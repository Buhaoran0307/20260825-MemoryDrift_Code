"""Read-only account routing with verified replica and quota boundaries.

This module does not own credentials or a database. The caller supplies an
account registry; only runtime requests receive secrets or physical scopes.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


PROVIDERS = {"hindsight_cloud": "hindsight", "mem0_platform_v3": "mem0", "memos_cloud": "memos"}
_QUOTA_CODES = frozenset({
    "quota_exceeded", "quota_exhausted", "insufficient_quota",
    "monthly_quota_exceeded", "monthly_limit_exceeded", "usage_limit_exceeded",
    "credits_exhausted", "credit_balance_exhausted", "insufficient_credits",
    "credit_limit_exceeded", "billing_hard_limit_reached",
})
_QUOTA_MESSAGE = re.compile(
    r"(?:monthly|account|plan|billing)\s+(?:usage\s+)?(?:quota|credits?|limit)"
    r".{0,60}(?:exhausted|exceeded|reached|depleted)|"
    r"(?:usage\s+)?quota\s+(?:exhausted|exceeded|reached)"
    r".{0,40}(?:billing\s+period|monthly|account|plan)|"
    r"(?:insufficient|no remaining|out of)\s+(?:account\s+)?credits?|"
    r"(?:quota|credits?)\s+(?:has been |is )?(?:exhausted|depleted)", re.I,
)
_BLOCKED_STATES = {"disabled", "quota_exhausted", "exhausted", "invalid_credentials", "forbidden"}


def is_default_account(account):
    """Default credentials are reserved for exporting first-stage memory."""
    return bool(account.get("is_default") or account.get("export_only")
                or account.get("environment_account")
                or str(account.get("name", "")).strip().lower() == "test0"
                or str(account.get("account_id", "")).startswith("env-"))


def classify_cloud_failure(status_code=None, body=None, headers=None, *, provider=None, redactions=()):
    """Classify bounded provider evidence; HTTP 429 alone is only a rate limit."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (ValueError, TypeError):
            body = {"message": body[:2048]}
    codes, messages = [], []

    def collect(value, depth=0):
        if depth > 4 or not isinstance(value, dict):
            return
        for name in ("code", "error_code", "type"):
            item = value.get(name)
            if isinstance(item, (str, int)) and not isinstance(item, bool):
                item = str(item)
                for secret in redactions:
                    if secret:
                        item = item.replace(secret, "[REDACTED]")
                codes.append(re.sub(r"[\s-]+", "_", item.strip().lower())[:128])
        for name in ("message", "detail", "error", "reason"):
            item = value.get(name)
            if isinstance(item, str):
                messages.append(item[:2048])
            elif isinstance(item, dict):
                collect(item, depth + 1)
        for name in ("errors", "data"):
            item = value.get(name)
            if isinstance(item, dict):
                collect(item, depth + 1)
            elif isinstance(item, list):
                for row in item[:5]:
                    collect(row, depth + 1)

    collect(body)
    code = next((item for item in codes if item in _QUOTA_CODES), codes[0] if codes else None)
    if provider in {"memos", "memos_cloud"} and code in {"40100", "40130", "40132"}:
        category = "invalid_credentials"
    elif provider in {"memos", "memos_cloud"} and code == "40304":
        category = "quota_exhausted"
    elif provider in {"memos", "memos_cloud"} and code == "40300":
        category = "rate_limited"
    elif provider in {"memos", "memos_cloud"} and code == "40103":
        category = "scope_missing"
    elif provider in {"memos", "memos_cloud"} and code in {"50000", "50002", "50004", "50005"}:
        category = "transient_error"
    elif provider in {"memos", "memos_cloud"} and code not in {None, "0"}:
        category = "provider_error"
    elif isinstance(status_code, int) and 200 <= status_code < 300:
        category = "completed"
    elif status_code == 401:
        category = "invalid_credentials"
    elif status_code == 402 and provider in {"hindsight", "hindsight_cloud"}:
        # Hindsight billing documents exhausted credits as HTTP 402.
        # https://docs.hindsight.vectorize.io/billing/
        category = "quota_exhausted"
    elif status_code in {400, 402, 403, 429} and (
        code in _QUOTA_CODES or any(_QUOTA_MESSAGE.search(item) for item in messages)
    ):
        category = "quota_exhausted"
    elif status_code == 403:
        category = "forbidden"
    elif status_code == 404:
        category = "scope_missing"
    elif status_code == 429:
        category = "rate_limited"
    elif status_code is None:
        category = "transport_error"
    elif status_code in {408, 500, 502, 503, 504}:
        category = "transient_error"
    else:
        category = "provider_error"
    result = {"category": category, "http_status": status_code,
              "provider_error_code": code, "provider": provider}
    for name, value in (headers or {}).items():
        value = str(value)
        for secret in redactions:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        if str(name).lower() == "retry-after":
            result["retry_after"] = str(value)[:128]
        elif str(name).lower() in {"x-request-id", "request-id", "x-correlation-id"}:
            result["request_id"] = str(value)[:128]
    return result


def cloud_failure_from_exception(exc, *, provider=None):
    existing = getattr(exc, "cloud_failure", None)
    if isinstance(existing, dict):
        return {**existing, "provider": existing.get("provider") or provider}
    diagnostics = getattr(exc, "diagnostics", {}) or {}
    return classify_cloud_failure(
        getattr(exc, "status_code", diagnostics.get("status_code")),
        diagnostics.get("response_body"), diagnostics.get("response_headers"),
        provider=provider,
    )


class MemoryAccountsUnavailable(RuntimeError):
    """No eligible same-version replica can serve this pending memory read."""
    def __init__(self, message, *, audit):
        super().__init__(message)
        self.memory_read_audit = deepcopy(audit)


def _physical_scope(provider, value):
    if isinstance(value, dict):
        value = value.get("bank_id" if provider == "hindsight" else "user_id") or value.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("verified replica has no physical memory scope")
    value = value.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or (
        provider == "hindsight" and (value in {".", ".."} or "/" in value or "\\" in value)
    ):
        raise ValueError("verified replica physical memory scope is invalid")
    return value


def _cooling(value):
    if not value:
        return False
    try:
        if isinstance(value, (int, float)):
            return value > datetime.now(timezone.utc).timestamp()
        expiry = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry > datetime.now(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return True


class MemoryReadRouter:
    def __init__(self, *, store, source, pool, on_attempt=None):
        self.store, self.source, self.pool = store, deepcopy(source), deepcopy(pool)
        if self.pool.get("shared") is False and not self.pool.get("account_ids"):
            raise ValueError("non-shared memory account pools require at least one account ID")
        self.strategy = source.get("memory_strategy")
        self.provider = PROVIDERS.get(source.get("memory_strategy"))
        if self.provider is None:
            raise ValueError("account pools support Hindsight, Mem0 and MemOS only")
        self.on_attempt = on_attempt
        self.preferred_account_id = None
        self.memory_set_id = pool.get("memory_set_id")
        self.revision_hash = pool.get("revision_hash")

    def freeze_pool(self):
        """Bind a legacy automatic pool before persisting a second-stage run."""
        rows = self._replicas()
        if not self.memory_set_id or not self.revision_hash:
            raise ValueError("Import the selected experiment memory from the default account into experiment accounts before starting stage two.")
        shared = self.pool.get("shared", True)
        account_ids = (list(self.pool["account_ids"]) if shared is False
                       else sorted({row["account_id"] for row in rows}))
        return {**self.pool, "enabled": True, "shared": shared,
                "account_ids": account_ids,
                "memory_set_id": self.memory_set_id, "revision_hash": self.revision_hash}

    def _replicas(self):
        replicas = self.store.list_replicas(
            source_run_id=self.source["run_id"], agent_id=self.source["agent_id"],
            snapshot_sha256=self.source["snapshot_sha256"],
        )
        allowed = set(self.pool.get("account_ids", []))
        if self.pool.get("shared") or (not allowed and self.pool.get("shared") is not False):
            allowed.update(row.get("account_id") for row in replicas)
        rows = [row for row in replicas if row.get("account_id") in allowed
                and row.get("provider") in {self.provider, self.source["memory_strategy"]}
                and row.get("verified") is True and row.get("complete") is True
                and row.get("status") in {"ready", "verified", "completed"}
                and row.get("memory_set_id") and row.get("revision_hash")]
        experiment_rows, accounts = [], {}
        for row in rows:
            try:
                account = self.store.resolve_account(row["account_id"], expected_provider=self.strategy)
            except (KeyError, ValueError):
                continue
            if not is_default_account(account):
                experiment_rows.append(row)
                accounts[row["account_id"]] = account
        rows = experiment_rows
        selection_rows = rows
        if self.pool.get("shared") and not self.memory_set_id and not self.revision_hash:
            available = [row for row in rows
                if accounts[row["account_id"]].get("enabled", True)
                and accounts[row["account_id"]].get("status") not in _BLOCKED_STATES
                and not _cooling(accounts[row["account_id"]].get("cooldown_until"))
                and not (accounts[row["account_id"]].get("status") == "cooldown"
                         and not accounts[row["account_id"]].get("cooldown_until"))
                and (accounts[row["account_id"]].get("credential_revision") is None
                     or row.get("credential_revision") == accounts[row["account_id"]]["credential_revision"])]
            selection_rows = available or rows
        versions = {(row["memory_set_id"], row["revision_hash"]) for row in selection_rows}
        if not self.memory_set_id and not self.revision_hash:
            if len(versions) == 1:
                self.memory_set_id, self.revision_hash = next(iter(versions))
            elif versions and self.pool.get("shared"):
                selected = max(selection_rows, key=lambda row: (row.get("updated_at", ""), row.get("replica_id", "")))
                self.memory_set_id, self.revision_hash = selected["memory_set_id"], selected["revision_hash"]
            elif versions:
                raise ValueError("account pool must select one verified memory set version")
        return [row for row in rows if (row["memory_set_id"], row["revision_hash"])
                == (self.memory_set_id, self.revision_hash)]

    async def render(self, render_callback, **kwargs):
        if ("agent_id" in kwargs and kwargs["agent_id"] != self.source["agent_id"]) or (
            "memory_snapshot" in kwargs and kwargs["memory_snapshot"] != self.source["snapshot"]
        ):
            raise ValueError("memory read router source does not match the frozen request")
        audit = {"provider": self.provider, "source_run_id": self.source["run_id"],
                 "snapshot_sha256": self.source["snapshot_sha256"], "attempts": []}

        def record(item):
            audit["attempts"].append(item)
            if self.on_attempt:
                self.on_attempt(deepcopy(audit))

        def update_record():
            if self.on_attempt:
                self.on_attempt(deepcopy(audit))

        try:
            rows = self._replicas()
        except ValueError as exc:
            audit.update(status="pending_memory", reason="memory_version_not_selected")
            raise MemoryAccountsUnavailable(str(exc), audit=audit) from None
        audit.update(memory_set_id=self.memory_set_id, revision_hash=self.revision_hash)
        order = {account_id: index for index, account_id in enumerate(self.pool.get("account_ids", []))}
        priorities = {row["account_id"]: row.get("priority", 100)
                      for row in self.store.list_accounts(provider=self.strategy)} if hasattr(self.store, "list_accounts") else {}
        rows.sort(key=lambda row: (row["account_id"] != self.preferred_account_id,
                                  priorities.get(row["account_id"], 100), order.get(row["account_id"], len(order)),
                                  row.get("replica_id", "")))
        seen, depleted_domains = set(), set()
        for replica in rows:
            account_id = replica["account_id"]
            if account_id in seen:
                continue
            seen.add(account_id)
            try:
                account = self.store.resolve_account(account_id, expected_provider=self.strategy)
            except (KeyError, ValueError):
                record({"account_id": account_id, "status": "skipped", "reason": "account_unavailable"})
                continue
            if is_default_account(account):
                record({"account_id": account_id, "status": "skipped", "reason": "default_account_export_only"})
                continue
            domain = account.get("quota_domain") or account_id
            credential_revision = account.get("credential_revision")
            if credential_revision is not None and replica.get("credential_revision") != credential_revision:
                record({"account_id": account_id, "status": "skipped", "reason": "credential_revision_mismatch",
                        "credential_revision": credential_revision,
                        "replica_credential_revision": replica.get("credential_revision")})
                continue
            if (not account.get("enabled", True) or account.get("status") in _BLOCKED_STATES
                    or _cooling(account.get("cooldown_until")) or domain in depleted_domains
                    or (account.get("status") == "cooldown" and not account.get("cooldown_until"))):
                record({"account_id": account_id, "status": "skipped", "reason": "account_not_ready"})
                continue
            scope = _physical_scope(self.provider, replica.get("physical_scope"))
            runtime = {"api_key": account["api_key"],
                       "run_id": self.source.get("snapshot", {}).get("options", {}).get("scope", {}).get("run_id")
                       or self.source["run_id"], "read_only": True,
                       "physical_scope": scope, "account_id": account_id}
            for name in ("api_base",):
                if account.get(name):
                    runtime[name] = account[name]
            attempt = {"account_id": account_id, "replica_id": replica.get("replica_id"),
                       "physical_scope": scope, "started_at": datetime.now(timezone.utc).isoformat(),
                       "status": "started"}
            if credential_revision is not None:
                attempt["credential_revision"] = credential_revision
            for name in ("org_id", "project_id", "quota_domain"):
                if account.get(name):
                    attempt[name] = account[name]
            if account.get("api_base"):
                attempt["api_base"] = account["api_base"]
            record(attempt)
            try:
                result = await render_callback(**{**kwargs, "memory_runtime": runtime})
            except Exception as exc:
                if credential_revision is not None:
                    try:
                        current = self.store.resolve_account(account_id, expected_provider=self.strategy)
                    except (KeyError, ValueError):
                        current = {}
                    if current.get("credential_revision") != credential_revision:
                        attempt.update(status="discarded", reason="credential_changed_during_read")
                        update_record()
                        continue
                failure = cloud_failure_from_exception(exc, provider=self.provider)
                attempt.update(status="failed", failure=failure)
                update_record()
                if failure["category"] == "quota_exhausted":
                    marked = self.store.set_account_state(account_id, "quota_exhausted",
                        last_error=json.dumps(failure, ensure_ascii=False), expected_revision=credential_revision)
                    if marked is False or (credential_revision is not None and isinstance(marked, dict)
                            and marked.get("credential_revision") != credential_revision):
                        attempt.update(status="discarded", reason="credential_changed_during_read")
                        update_record()
                        continue
                    depleted_domains.add(domain)
                    # Keys from one account/project share its depletion state.
                    for other_id in {row["account_id"] for row in rows}:
                        if other_id == account_id:
                            continue
                        try:
                            other = self.store.resolve_account(other_id, expected_provider=self.strategy)
                        except (KeyError, ValueError):
                            continue
                        if (not is_default_account(other) and other.get("quota_domain")
                                and other["quota_domain"] == domain):
                            self.store.set_account_state(other_id, "quota_exhausted",
                                last_error=json.dumps(failure, ensure_ascii=False),
                                expected_revision=other.get("credential_revision"))
                    continue
                if failure["category"] in {"invalid_credentials", "forbidden"}:
                    marked = self.store.set_account_state(account_id, failure["category"],
                        last_error=json.dumps(failure, ensure_ascii=False), expected_revision=credential_revision)
                    if marked is False or (credential_revision is not None and isinstance(marked, dict)
                            and marked.get("credential_revision") != credential_revision):
                        attempt.update(status="discarded", reason="credential_changed_during_read")
                        update_record()
                        continue
                exc.memory_read_audit = deepcopy(audit)
                raise
            if credential_revision is not None:
                try:
                    current = self.store.resolve_account(account_id, expected_provider=self.strategy)
                except (KeyError, ValueError):
                    current = {}
                if current.get("credential_revision") != credential_revision:
                    attempt.update(status="discarded", reason="credential_changed_during_read")
                    update_record()
                    continue
            attempt.update(status="completed")
            update_record()
            self.preferred_account_id = account_id
            audit.update(status="completed", account_id=account_id,
                         replica_id=replica.get("replica_id"), physical_scope=scope)
            result["memory_read_audit"] = deepcopy(audit)
            return result
        audit.update(status="pending_memory", reason="no_verified_available_replica")
        raise MemoryAccountsUnavailable("No verified account can currently serve this memory set", audit=audit)
