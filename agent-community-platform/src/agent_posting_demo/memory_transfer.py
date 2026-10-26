"""Bounded, credential-safe cloud memory export and replication.

This module does not read environment credentials. All methods are synchronous;
the service runs them in its job thread. Inject ``transport.request`` for offline
tests. A mutation is sent once: a lost acknowledgement is returned as ``unknown``
and must be reconciled with ``verify`` before anyone considers another import.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import socket
import time
import uuid
import zipfile
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from http.client import IncompleteRead
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_EXPORT_BYTES = 100 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
API_BASES = {
    "mem0_platform_v3": "https://api.mem0.ai",
    "hindsight_cloud": "https://api.hindsight.vectorize.io",
    "memos_cloud": "https://memos.memtensor.cn/api/openmem/v1",
}
MEMOS_CATEGORIES = (
    "memory_detail_list", "preference_detail_list", "profile_detail_list",
    "event_detail_list", "tool_memory_detail_list", "skill_detail_list",
)
MEM0_PROJECT_FIELDS = (
    "custom_instructions", "agent_custom_instructions", "custom_categories",
    "multilingual", "decay", "enable_graph", "memory_depth", "usecase_setting",
)


class CloudTransferError(RuntimeError):
    """Public messages contain no request credentials or unsanitized URLs."""

    def __init__(self, message: str, *, category: str = "provider_error",
                 status_code: int | None = None, retry_after: float | None = None):
        self.category = category
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(message)


class CloudTransferCancelled(CloudTransferError):
    def __init__(self):
        super().__init__("Cloud memory transfer cancelled", category="cancelled")


class _UnknownMutation(CloudTransferError):
    pass


@dataclass(frozen=True)
class CloudTransferResponse:
    status_code: int
    body: Any = b""
    headers: Mapping[str, str] = field(default_factory=dict)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib normally forwards Authorization across redirects. No transfer
        # request needs redirects; reject them instead of leaking the key.
        return None


class _StdlibTransport:
    def __init__(self, max_bytes: int, cancel_event=None):
        self.max_bytes = max_bytes
        self.cancel_event = cancel_event
        self.opener = build_opener(_NoRedirects())

    def request(self, method, url, *, headers, body, timeout):
        request = Request(url, data=body, headers=dict(headers), method=method)
        started = time.monotonic()
        try:
            response = self.opener.open(request, timeout=timeout)
        except HTTPError as exc:
            response = exc
        try:
            size = response.headers.get("Content-Length")
            if size and int(size) > self.max_bytes:
                raise CloudTransferError("Cloud response exceeds the export size limit",
                                         category="capacity")
            output = bytearray()
            reader = getattr(response, "read1", response.read)
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise CloudTransferCancelled()
                if time.monotonic() - started >= timeout:
                    raise TimeoutError("Cloud response exceeded its request deadline")
                block = reader(min(65536, self.max_bytes + 1 - len(output)))
                if not block:
                    break
                output.extend(block)
                if len(output) > self.max_bytes:
                    raise CloudTransferError("Cloud response exceeds the export size limit",
                                             category="capacity")
            return CloudTransferResponse(int(response.code), bytes(output), dict(response.headers))
        finally:
            response.close()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def classify_transfer_error(provider: str, status: int, body: Any,
                            headers: Mapping[str, str] | None = None) -> str:
    """Quota is a positive diagnosis, never inferred from a generic 429/413."""
    if provider == "memos_cloud":
        from agent_posting_core.memory_read_router import classify_cloud_failure
        failure = classify_cloud_failure(status, body, headers, provider=provider)
        return {"invalid_credentials": "authentication", "scope_missing": "not_found",
                "forbidden": "permission", "transient_error": "temporary",
                "transport_error": "network"}.get(failure["category"], failure["category"])
    if provider == "hindsight_cloud" and status == 402:
        return "quota_exhausted"
    data = body if isinstance(body, dict) else {}
    codes = []

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"code", "error_code", "type", "error"} and isinstance(child, str):
                    codes.append(child.lower())
                elif isinstance(child, dict):
                    visit(child)

    visit(data)
    quota_codes = {
        "quota_exceeded", "insufficient_quota", "monthly_quota_exceeded",
        "memory_quota_exceeded", "retrieval_quota_exceeded", "credits_exhausted",
        "insufficient_credits", "credit_balance_exhausted", "monthly_limit_exceeded",
    }
    if any(code in quota_codes for code in codes):
        return "quota_exhausted"
    # Some Mem0 responses expose a detail string rather than a structured code.
    message = str(data.get("detail", data.get("message", ""))).lower()
    if (provider == "mem0_platform_v3" and status in {402, 403, 429}
            and re.search(r"(?:monthly|retrieval|memory|request)\s+(?:usage\s+)?quota\s+"
                          r"(?:has been\s+)?(?:exceeded|exhausted)|"
                          r"(?:insufficient|exhausted)\s+(?:monthly\s+)?credits", message)):
        return "quota_exhausted"
    if status == 429:
        return "rate_limited"
    if status == 413:
        return "request_too_large"
    if status == 401:
        return "authentication"
    if status == 403:
        return "permission"
    if status == 404:
        return "not_found"
    if status in {400, 409, 422}:
        return "validation"
    if status >= 500 or status == 408:
        return "temporary"
    return "provider_error"


def _origin(url: str):
    parsed = urlsplit(url)
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(),
            parsed.port or (443 if parsed.scheme == "https" else 80))


def _scope(value: str) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or value in {".", ".."} or len(value) > 512
            or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise CloudTransferError("A nonempty, single namespace or bank ID is required",
                                 category="validation")
    return value


def _semantic_item(provider: str, item: dict) -> dict:
    """Stable public content fields; remote IDs and embedding scores are excluded."""
    if provider == "mem0_platform_v3":
        return {
            "text": item.get("memory", item.get("text", "")),
            "metadata": item.get("metadata") or {},
            "expiration_date": item.get("expiration_date") or None,
        }
    if provider == "memos_cloud":
        # Preserve native categories and source fields in export hashes. MemOS
        # has no verified import contract, so this is an inventory fingerprint.
        return {key: value for key, value in item.items()
                if key not in {"id", "native_id", "relativity"}}
    result = {
        "text": item.get("text", ""), "fact_type": item.get("fact_type"),
        "context": item.get("context") or "", "metadata": item.get("metadata") or {},
        "tags": sorted(item.get("tags") or []), "state": item.get("state") or "valid",
    }
    for name in ("mentioned_at", "occurred_start", "occurred_end"):
        if item.get(name):
            # Providers can emit +00:00 or Z for the same stored instant.
            try:
                result[name] = datetime.fromisoformat(item[name].replace("Z", "+00:00")).isoformat()
            except (ValueError, AttributeError):
                result[name] = item[name]
    return result


def memory_content_hash(provider: str, items: list[dict]) -> str:
    return _digest(sorted(_digest(_semantic_item(provider, item)) for item in items))


def memos_snapshot_package(snapshot: dict, scope: str) -> dict:
    """Expose all six native memory categories without re-extracting content."""
    data = snapshot.get("data")
    if snapshot.get("user_id") is not None and snapshot["user_id"] != scope:
        raise CloudTransferError("MemOS snapshot belongs to a different user_id", category="validation")
    if not isinstance(data, dict) or not all(key in data for key in MEMOS_CATEGORIES):
        raise CloudTransferError("MemOS snapshot has no native memory categories", category="validation")
    items = []
    for category in MEMOS_CATEGORIES:
        rows = data.get(category, [])
        if not isinstance(rows, list):
            raise CloudTransferError("MemOS snapshot contains a malformed memory category", category="validation")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise CloudTransferError("MemOS snapshot contains a malformed memory record", category="validation")
            text = next((row.get(key) for key in ("memory_value", "preference", "event_value", "tool_value", "memory")
                         if isinstance(row.get(key), str)), None)
            if text is None:
                text = json.dumps(row.get("properties", row.get("skill_value", {})), ensure_ascii=False, sort_keys=True)
            items.append({**deepcopy(row), "native_id": row["id"], "id": category + ":" + row["id"],
                          "native_category": category, "text": text})
    return {"schema_version": 1, "provider": "memos_cloud", "source_scope": scope,
            "items": items, "data": deepcopy(data), "kind": "memos_native_memory",
            "complete": snapshot.get("complete") is True, "pages": deepcopy(snapshot.get("pages")),
            "counts": {key: len(data.get(key, [])) for key in MEMOS_CATEGORIES},
            "exported_at": snapshot.get("exported_at"), "fidelity": "native_readback",
            "content_hash": memory_content_hash("memos_cloud", items),
            "replication_supported": False,
            "limitations": ["MemOS Cloud does not expose a verified native import API; this export cannot be used as a complete account replica"]}


def _selected(package: dict, selected_ids) -> list[dict]:
    rows = package.get("items")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise CloudTransferError("Transfer package has no valid memory list", category="validation")
    if selected_ids is None:
        return deepcopy(rows)
    if (not isinstance(selected_ids, (list, tuple, set)) or not selected_ids
            or not all(isinstance(value, str) and value for value in selected_ids)):
        raise CloudTransferError("Select at least one valid memory ID", category="validation")
    selected = set(selected_ids)
    by_id = {row.get("id"): row for row in rows}
    if not selected.issubset(by_id):
        raise CloudTransferError("Selected memory IDs are missing from the cached export",
                                 category="validation")
    return deepcopy([row for row in rows if row.get("id") in selected])


class CloudMemoryTransfer:
    def __init__(self, account: dict, transport=None, *, cancel_event=None,
                 timeout_seconds: float = 30, operation_timeout_seconds: float = 300,
                 poll_interval_seconds: float = 1, max_bytes: int = MAX_EXPORT_BYTES,
                 max_pages: int = 10000, sleep: Callable = time.sleep,
                 monotonic: Callable = time.monotonic):
        self.provider = account.get("provider")
        if self.provider not in API_BASES:
            raise CloudTransferError("Unsupported cloud memory provider", category="validation")
        self._key = account.get("api_key")
        if not isinstance(self._key, str) or not self._key.strip():
            raise CloudTransferError("Cloud memory API key is empty", category="authentication")
        self.api_base = (account.get("api_base") or API_BASES[self.provider]).rstrip("/")
        parsed = urlsplit(self.api_base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise CloudTransferError("Invalid cloud API base URL", category="validation")
        if self._key in self.api_base:
            raise CloudTransferError("The API base URL must not contain credentials", category="validation")
        if (not 0 < max_bytes <= MAX_EXPORT_BYTES or max_pages < 1
                or timeout_seconds <= 0 or operation_timeout_seconds <= 0
                or poll_interval_seconds < 0):
            raise CloudTransferError("Invalid cloud transfer resource limits", category="validation")
        self.max_bytes = int(max_bytes)
        self.max_pages = int(max_pages)
        self.timeout = min(float(timeout_seconds), 60)
        self.operation_timeout = float(operation_timeout_seconds)
        self.poll_interval = min(float(poll_interval_seconds), 10)
        self.cancel_event = cancel_event
        self._sleep = sleep
        self._monotonic = monotonic
        self.transport = transport or _StdlibTransport(self.max_bytes, cancel_event)
        self._account = {
            key: deepcopy(account.get(key)) for key in
            ("account_id", "org_id", "organization_id", "project_id", "quota_group",
             "identity_verified") if account.get(key) is not None
        }
        self._identity = None

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self._key, "[REDACTED]")
        if isinstance(value, dict):
            # Memory metadata can legitimately contain an experiment's api_key
            # field. Mask this account's actual credential, not unrelated data.
            return {key: self._redact(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self._redact(child) for child in value]
        return value

    def _check_cancel(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise CloudTransferCancelled()

    def _wait(self):
        self._check_cancel()
        if self.cancel_event is not None:
            if self.cancel_event.wait(self.poll_interval):
                raise CloudTransferCancelled()
        else:
            self._sleep(self.poll_interval)

    def _url(self, path):
        if path.startswith("/") and not path.startswith("//"):
            url = self.api_base + path
        else:
            url = urljoin(self.api_base + "/", path)
        if (_origin(url) != _origin(self.api_base) or urlsplit(url).username
                or urlsplit(url).password or urlsplit(url).fragment):
            raise CloudTransferError("Refusing to send cloud credentials to a different origin",
                                     category="validation")
        return url

    def _signed_archive_url(self, path):
        # Hindsight Cloud returns GCS object URLs signed by the provider. The
        # signature authorizes this download; our source account key must not
        # be forwarded to the object store. This exception is deliberately
        # limited to the observed provider contract, never general API calls.
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if (parsed.scheme != "https" or parsed.hostname != "storage.googleapis.com"
                or parsed.port not in {None, 443} or parsed.username or parsed.password
                or parsed.fragment or not parsed.path or "\\" in path or self._key in path
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or query.get("X-Goog-Algorithm") not in (["GOOG4-RSA-SHA256"], ["GOOG4-HMAC-SHA256"])
                or not all(len(query.get(key, [])) == 1 and query[key][0] for key in
                           ("X-Goog-Credential", "X-Goog-Date", "X-Goog-Expires", "X-Goog-Signature", "X-Goog-SignedHeaders"))):
            raise CloudTransferError("Refusing archive download from a different origin without a supported signed URL",
                                     category="validation")
        return path

    def _download_archive(self, path):
        if _origin(urljoin(self.api_base + "/", path)) == _origin(self.api_base):
            return self._request("GET", path, binary=True)
        return self._request("GET", path, binary=True, credential_free=True)

    def _request(self, method, path, payload=None, *, binary=False, body=None,
                 content_type=None, mutation=False, allow_404=False, credential_free=False):
        self._check_cancel()
        if credential_free:
            if self.provider != "hindsight_cloud" or method != "GET" or not binary or payload is not None or body is not None:
                raise CloudTransferError("Credential-free requests are limited to archive downloads", category="validation")
            url = self._signed_archive_url(path)
            headers = {"Accept": "application/zip"}
        else:
            url = self._url(path)
            headers = {"Authorization": ("Token " if self.provider in {"mem0_platform_v3", "memos_cloud"}
                                         else "Bearer ") + self._key,
                       "Accept": "application/zip" if binary else "application/json"}
        if payload is not None:
            body = _json_bytes(payload)
            content_type = "application/json"
        if body is not None and len(body) > self.max_bytes + 65536:
            raise CloudTransferError("Import exceeds the export size limit", category="capacity")
        if content_type:
            headers["Content-Type"] = content_type
        try:
            call = getattr(self.transport, "request", self.transport)
            response = call(method, url, headers=headers, body=body, timeout=self.timeout)
        except AssertionError:
            raise
        except CloudTransferError as exc:
            if mutation:
                raise _UnknownMutation("Cloud mutation response could not be read safely",
                                       category="unknown_commit") from None
            raise exc
        except (URLError, TimeoutError, socket.timeout, OSError, IncompleteRead) as exc:
            cls = _UnknownMutation if mutation else CloudTransferError
            raise cls("Cloud memory request lost its acknowledgement (" + type(exc).__name__ + ")",
                      category="unknown_commit" if mutation else "network") from None
        except Exception as exc:
            cls = _UnknownMutation if mutation else CloudTransferError
            raise cls("Cloud memory transport failed (" + type(exc).__name__ + ")",
                      category="unknown_commit" if mutation else "network") from None
        if isinstance(response, dict):
            response = CloudTransferResponse(response.get("status_code", response.get("status", 200)),
                                             response.get("body", b""), response.get("headers", {}))
        status = getattr(response, "status_code", None)
        raw = getattr(response, "body", None)
        if type(status) is not int or not 100 <= status <= 599:
            cls = _UnknownMutation if mutation else CloudTransferError
            raise cls("Cloud memory returned an invalid HTTP acknowledgement", category="unknown_commit"
                      if mutation else "provider_error")
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if isinstance(raw, (bytes, bytearray)) and len(raw) > (self.max_bytes if binary else MAX_JSON_BYTES):
            cls = _UnknownMutation if mutation else CloudTransferError
            raise cls("Cloud response exceeds its size limit", category="unknown_commit"
                      if mutation else "capacity")
        data = raw
        if not binary or status >= 300:
            try:
                data = json.loads(raw) if isinstance(raw, (bytes, bytearray)) else raw
            except (ValueError, UnicodeDecodeError):
                data = {"detail": bytes(raw or b"").decode("utf-8", errors="replace")[:512]}
        if status == 404 and allow_404:
            return None
        if not 200 <= status < 300:
            if credential_free:
                # Object-store errors can echo the signed URL. Persist only
                # the status, never its credential/signature or response body.
                raise CloudTransferError(f"Cloud archive download failed (HTTP {status})",
                                         category="network" if status >= 500 else "provider_error", status_code=status)
            if mutation and (status == 408 or status >= 500):
                raise _UnknownMutation(f"Cloud import acknowledgement is ambiguous (HTTP {status})",
                                       category="unknown_commit", status_code=status)
            category = classify_transfer_error(self.provider, status, data, response.headers)
            retry = None
            try:
                retry = float(next((v for k, v in response.headers.items()
                                    if k.lower() == "retry-after"), ""))
            except (ValueError, TypeError):
                pass
            safe = str(self._redact(data))[:512]
            error = CloudTransferError(f"Cloud memory HTTP {status}: {safe}", category=category,
                                       status_code=status, retry_after=retry)
            if self.provider == "memos_cloud":
                from agent_posting_core.memory_read_router import classify_cloud_failure
                error.cloud_failure = classify_cloud_failure(
                    status, data, response.headers, provider=self.provider, redactions=(self._key,))
            raise error
        if binary:
            if not isinstance(raw, (bytes, bytearray)):
                raise CloudTransferError("Cloud archive is not binary data", category="provider_error")
            return bytes(raw)
        if self.provider == "memos_cloud" and isinstance(data, dict) and data.get("code") != 0:
            from agent_posting_core.memory_read_router import classify_cloud_failure
            failure = classify_cloud_failure(status, data, response.headers, provider=self.provider, redactions=(self._key,))
            category = {"invalid_credentials": "authentication", "scope_missing": "not_found",
                        "forbidden": "permission", "transient_error": "temporary"}.get(
                            failure["category"], failure["category"])
            error = CloudTransferError("MemOS rejected the request: " + str(self._redact(data))[:512],
                                       category=category, status_code=status)
            error.cloud_failure = failure
            raise error
        if not isinstance(data, dict):
            cls = _UnknownMutation if mutation else CloudTransferError
            raise cls("Cloud memory returned a malformed JSON acknowledgement",
                      category="unknown_commit" if mutation else "provider_error")
        return self._redact(data)

    def identify(self) -> dict:
        if self.provider == "memos_cloud":
            # There is no documented authenticated account inventory endpoint.
            # Probe an isolated read-only scope; never ADD as a connection test.
            try:
                data = self._request("POST", "/get/memory", {"user_id": "platform_connection_check", "page": 1, "size": 1})
                native = data.get("data")
                if (not isinstance(native, dict)
                        or not all(isinstance(native.get(category), list) for category in MEMOS_CATEGORIES)
                        or any(type(native.get(key)) is not int or native[key] < 0
                               for key in ("current", "pages", "size", "total"))):
                    raise CloudTransferError("Invalid MemOS memory read acknowledgement", category="provider_error")
            except CloudTransferError as exc:
                if (exc.category != "not_found"
                        or getattr(exc, "cloud_failure", {}).get("provider_error_code") != "40103"):
                    raise
            identity = {"provider": self.provider, "api_base": self.api_base,
                        "identity_verified": False, "connected": True,
                        "identity_source": "authenticated_read"}
        elif self.provider == "mem0_platform_v3":
            data = self._request("GET", "/v1/ping/")
            org_id, project_id = data.get("org_id"), data.get("project_id")
            known = isinstance(org_id, str) and bool(org_id) and isinstance(project_id, str) and bool(project_id)
            identity = {"provider": self.provider, "api_base": self.api_base,
                        "org_id": org_id, "project_id": project_id,
                        "identity_verified": known, "connected": True,
                        "identity_source": "provider_ping"}
            if known:
                identity["quota_domain"] = f"mem0:{self.api_base}:{org_id}"
                identity["storage_domain"] = f"mem0:{self.api_base}:{org_id}:{project_id}"
        else:
            # /version alone is unauthenticated. A successful bank inventory
            # request checks the key; organization ownership is user-supplied.
            data = self._request("GET", "/v1/default/banks?limit=1&offset=0")
            if not isinstance(data.get("banks"), list):
                raise CloudTransferError("Invalid Hindsight bank inventory", category="provider_error")
            org_id = self._account.get("org_id") or self._account.get("organization_id")
            identity = {"provider": self.provider, "api_base": self.api_base,
                        "org_id": org_id, "identity_verified": False, "connected": True,
                        "identity_source": "user_config"}
            if org_id:
                identity["quota_domain"] = f"hindsight:{self.api_base}:{org_id}"
                identity["storage_domain"] = identity["quota_domain"]
        if self._account.get("quota_group"):
            identity["configured_quota_group"] = self._account["quota_group"]
        self._identity = self._redact(identity)
        return deepcopy(self._identity)

    def list_memories(self, scope: str) -> dict:
        _scope(scope)
        if self.provider == "memos_cloud":
            return self._memos_list(scope)
        rows, ids, total_bytes = [], set(), 0
        total = None
        for page in range(1, self.max_pages + 1):
            if self.provider == "mem0_platform_v3":
                data = self._request("POST", f"/v3/memories/?page={page}&page_size=100",
                                     {"filters": {"user_id": scope}, "show_expired": True})
                current, count = data.get("results"), data.get("count")
            else:
                data = self._request("GET", self._bank(scope) +
                                     f"/memories/list?limit=100&offset={len(rows)}")
                current, count = data.get("items"), data.get("total")
                if data.get("offset", len(rows)) != len(rows):
                    raise CloudTransferError("Hindsight pagination returned a wrong offset", category="pagination")
            if (not isinstance(current, list) or type(count) is not int or count < 0
                    or not all(isinstance(row, dict) and isinstance(row.get("id"), str)
                               and row["id"] for row in current)):
                raise CloudTransferError("Cloud memory pagination returned malformed records", category="pagination")
            if total is not None and total != count:
                return {"items": rows, "complete": False, "pages": page,
                        "reason": "Cloud memories changed during pagination"}
            total = count
            batch_ids = [row["id"] for row in current]
            if len(set(batch_ids)) != len(batch_ids) or ids.intersection(batch_ids):
                raise CloudTransferError("Cloud memory pagination repeated records", category="pagination")
            ids.update(batch_ids)
            total_bytes += len(_json_bytes(current))
            if total_bytes > self.max_bytes:
                raise CloudTransferError("Memory list exceeds the export size limit", category="capacity")
            rows.extend(current)
            if len(rows) > total:
                raise CloudTransferError("Cloud memory pagination count is inconsistent", category="pagination")
            if len(rows) == total:
                if self.provider == "mem0_platform_v3" and data.get("next"):
                    raise CloudTransferError("Mem0 count and next-page marker disagree", category="pagination")
                return {"items": rows, "complete": True, "pages": page, "count": total}
            if not current or (self.provider == "mem0_platform_v3" and not data.get("next")):
                return {"items": rows, "complete": False, "pages": page, "count": total,
                        "reason": "Cloud memory pagination ended before all records were returned"}
            if self.provider == "mem0_platform_v3":
                # Never follow next URLs; pagination markers can point off-site.
                self._url(data["next"])
        return {"items": rows, "complete": False, "pages": self.max_pages,
                "reason": "Cloud memory pagination reached the configured page limit"}

    def _memos_list(self, scope):
        combined = {key: [] for key in MEMOS_CATEGORIES}
        seen, expected, page_count, total_bytes = set(), None, None, 0
        for page in range(1, self.max_pages + 1):
            response = self._request("POST", "/get/memory", {"user_id": scope, "page": page, "size": 50})
            data = response.get("data")
            if not isinstance(data, dict) or not all(key in data for key in MEMOS_CATEGORIES):
                raise CloudTransferError("MemOS returned malformed memory categories", category="pagination")
            current, pages, total, size = (data.get(key) for key in ("current", "pages", "total", "size"))
            if (any(type(value) is not int or value < 0 for value in (current, pages, total, size))
                    or current != page and not (page == 1 and total == 0 and current == 0)
                    or size > 50 or total > 0 and (size < 1 or pages < 1)
                    or total > 0 and pages != (total + size - 1) // size):
                raise CloudTransferError("MemOS returned inconsistent pagination metadata", category="pagination")
            if expected is not None and (total != expected or pages != page_count):
                raise CloudTransferError("MemOS memory changed during pagination", category="pagination")
            expected, page_count = total, pages
            for category in MEMOS_CATEGORIES:
                rows = data.get(category, [])
                if not isinstance(rows, list) or len(rows) > size:
                    raise CloudTransferError("MemOS returned a malformed memory category", category="pagination")
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                        raise CloudTransferError("MemOS returned a malformed memory record", category="pagination")
                    identifier = (category, row["id"])
                    if identifier in seen:
                        raise CloudTransferError("MemOS pagination repeated memory records", category="pagination")
                    seen.add(identifier)
                combined[category].extend(rows)
            total_bytes += len(_json_bytes(data))
            if total_bytes > self.max_bytes:
                raise CloudTransferError("MemOS memory list exceeds its size limit", category="capacity")
            if page >= max(pages, 1):
                if max(map(len, combined.values()), default=0) != total:
                    raise CloudTransferError("MemOS category counts do not match pagination total", category="pagination")
                return memos_snapshot_package({"data": combined, "complete": True, "pages": page,
                    "exported_at": datetime.now().astimezone().isoformat()}, scope)
        raise CloudTransferError("MemOS pagination reached the configured page limit", category="capacity")

    @staticmethod
    def _bank(scope):
        return "/v1/default/banks/" + quote(_scope(scope), safe="")

    def _project_config(self):
        identity = self._identity or self.identify()
        if not identity.get("org_id") or not identity.get("project_id"):
            return None
        path = ("/api/v1/orgs/organizations/" + quote(identity["org_id"], safe="") +
                "/projects/" + quote(identity["project_id"], safe="") + "/")
        data = self._request("GET", path)
        return {key: deepcopy(data[key]) for key in MEM0_PROJECT_FIELDS if key in data}

    def _poll(self, scope, operation_id, *, mem0=False):
        if not isinstance(operation_id, str) or not operation_id:
            raise CloudTransferError("Cloud operation has no usable operation ID", category="provider_error")
        started = self._monotonic()
        while True:
            self._check_cancel()
            path = ("/v1/event/" + quote(operation_id, safe="") + "/" if mem0 else
                    self._bank(scope) + "/operations/" + quote(operation_id, safe=""))
            # An accepted job can briefly be invisible on a read replica. This
            # retries only the read, never the already submitted mutation.
            data = self._request("GET", path, allow_404=True)
            if data is None:
                if self._monotonic() - started >= self.operation_timeout:
                    raise CloudTransferError("Cloud operation receipt is not yet visible", category="poll_timeout")
                self._wait()
                continue
            status = str(data.get("status", "")).lower()
            if status in {"succeeded", "completed"}:
                return data
            if status in {"failed", "cancelled", "canceled"}:
                raise CloudTransferError("Cloud operation failed: " + str(self._redact(data))[:512],
                                         category="operation_failed")
            if status not in {"pending", "running", "processing", "queued", "in_progress"}:
                raise CloudTransferError("Cloud operation returned an unknown status", category="provider_error")
            if self._monotonic() - started >= self.operation_timeout:
                raise CloudTransferError("Cloud operation has not reached a terminal state", category="poll_timeout")
            self._wait()

    def _archive_files(self, archive):
        if not archive or len(archive) > self.max_bytes:
            raise CloudTransferError("Invalid or oversized transfer archive", category="capacity")
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                entries = zipped.infolist()
                if len(entries) > 100000 or sum(entry.file_size for entry in entries) > self.max_bytes:
                    raise CloudTransferError("Uncompressed transfer archive exceeds the size limit", category="capacity")
                names = [entry.filename for entry in entries]
                if len(names) != len(set(names)):
                    raise CloudTransferError("Transfer archive contains duplicate entries", category="validation")
                result = {}
                for entry in entries:
                    name = entry.filename
                    if (name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/")
                            or len(name) > 4096 or "\0" in name or entry.flag_bits & 1):
                        raise CloudTransferError("Transfer archive contains an unsafe entry", category="validation")
                    if not entry.is_dir():
                        result[name] = zipped.read(entry)
                if "manifest.json" not in result or not isinstance(json.loads(result["manifest.json"]), dict):
                    raise CloudTransferError("Transfer archive has no valid manifest", category="validation")
                return result
        except CloudTransferError:
            raise
        except (zipfile.BadZipFile, ValueError, UnicodeDecodeError, RuntimeError):
            raise CloudTransferError("Cloud export is not a valid transfer ZIP", category="validation") from None

    def export(self, scope: str, *, document_ids=None) -> dict:
        if self.provider == "memos_cloud":
            if document_ids is not None:
                raise CloudTransferError("MemOS exports include all native memory categories", category="validation")
            before, after = self.list_memories(scope), self.list_memories(scope)
            if before["content_hash"] != after["content_hash"]:
                raise CloudTransferError("MemOS memories changed during export", category="pagination")
            return before
        _scope(scope)
        if document_ids is not None and (not isinstance(document_ids, (list, tuple))
                                         or not document_ids or not all(isinstance(v, str) and v for v in document_ids)):
            raise CloudTransferError("Select at least one document ID", category="validation")
        before = self.list_memories(scope)
        package = {
            "schema_version": 1, "provider": self.provider, "source_scope": scope,
            "source_api_base": self.api_base, "items": before["items"],
            "complete": before["complete"], "pages": before["pages"],
            "exported_at": datetime.now().astimezone().isoformat(),
        }
        if not before["complete"]:
            package["reason"] = before.get("reason")
            package["content_hash"] = memory_content_hash(self.provider, package["items"])
            return package
        if self.provider == "mem0_platform_v3":
            if document_ids is not None:
                raise CloudTransferError("Mem0 exports use memory IDs, not document IDs", category="validation")
            package["identity"] = self._identity or self.identify()
            package["project_config"] = self._project_config()
            package["fidelity"] = "text_metadata"
            package["limitations"] = [
                "Remote IDs, internal links, categories and change history are not restored",
                "Identical text in the same scope may be deduplicated by Mem0",
                "Embeddings and retrieval ordering are not guaranteed to be identical",
            ]
        else:
            query = [("include_data", "true"), ("include_bank_config", "false" if document_ids else "true"),
                     ("include_history", "false")]
            if document_ids:
                query.extend(("document_id", value) for value in document_ids)
                package["items"] = [item for item in before["items"] if item.get("document_id") in document_ids]
            else:
                package["bank_config"] = self._request("GET", self._bank(scope) + "/config")
                package["bank_template"] = self._request("GET", self._bank(scope) + "/export")
            receipt = self._request("POST", self._bank(scope) + "/transfer/export?" + urlencode(query))
            done = self._poll(scope, receipt.get("operation_id"))
            metadata = done.get("result_metadata")
            download = metadata.get("download_url") if isinstance(metadata, dict) else None
            if not isinstance(download, str) or not download:
                raise CloudTransferError("Hindsight export has no archive download URL", category="provider_error")
            archive = self._download_archive(download)
            self._archive_files(archive)
            package.update(native_archive=base64.b64encode(archive).decode("ascii"),
                           archive_sha256=hashlib.sha256(archive).hexdigest(), archive_bytes=len(archive),
                           document_ids=list(document_ids) if document_ids else None,
                           fidelity="document_subset" if document_ids else "bank_transfer")
            package["limitations"] = ["Embeddings, remote IDs and entity/link resolution are rebuilt",
                                      "Retrieval ordering may differ after import"]
            if document_ids:
                package["limitations"].append("Document subsets omit bank observations, mental models and configuration")
        # A paginated list is not a transaction. Do not certify a package if the
        # source changed while an asynchronous bank archive was being produced.
        after = self.list_memories(scope)
        if (not after["complete"] or memory_content_hash(self.provider, before["items"]) !=
                memory_content_hash(self.provider, after["items"])):
            package["complete"] = False
            package["reason"] = "Cloud memories changed during export; pull a fresh snapshot"
        package["content_hash"] = memory_content_hash(self.provider, package["items"])
        return package

    def _validate_package(self, package):
        if (not isinstance(package, dict) or package.get("schema_version") != 1
                or package.get("provider") != self.provider or package.get("complete") is not True):
            raise CloudTransferError("A complete export for the same provider is required", category="validation")
        rows = _selected(package, None)
        if package.get("content_hash") != memory_content_hash(self.provider, rows):
            raise CloudTransferError("Cached memory content hash does not match its export", category="validation")
        return rows

    def _decode_archive(self, package):
        encoded = package.get("native_archive")
        if not isinstance(encoded, str) or len(encoded) > (self.max_bytes * 4 // 3 + 8):
            raise CloudTransferError("Cached native archive is missing or oversized", category="validation")
        try:
            archive = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise CloudTransferError("Cached native archive is not base64", category="validation") from None
        if hashlib.sha256(archive).hexdigest() != package.get("archive_sha256"):
            raise CloudTransferError("Cached native archive failed its SHA-256 check", category="validation")
        self._archive_files(archive)
        return archive

    def _bank_exists(self, scope):
        ids, total = set(), None
        for _ in range(self.max_pages):
            data = self._request("GET", f"/v1/default/banks?limit=100&offset={len(ids)}")
            rows, count = data.get("banks"), data.get("total")
            if (not isinstance(rows, list) or type(count) is not int or count < 0
                    or not all(isinstance(row, dict) and isinstance(row.get("bank_id"), str) for row in rows)):
                raise CloudTransferError("Cannot confirm target bank absence from inventory", category="pagination")
            if total is not None and total != count:
                raise CloudTransferError("Target bank inventory changed during pagination", category="pagination")
            total = count
            current = [row["bank_id"] for row in rows]
            if scope in current:
                return True
            if len(set(current)) != len(current) or ids.intersection(current):
                raise CloudTransferError("Target bank inventory repeated a page", category="pagination")
            ids.update(current)
            if len(ids) >= total:
                return False
            if not rows:
                break
        raise CloudTransferError("Target bank inventory is incomplete", category="pagination")

    def _fact_subset_archive(self, package, selected):
        files = self._archive_files(self._decode_archive(package))
        documents = {}
        for name, content in files.items():
            if name.startswith("documents/") and name.endswith(".json"):
                data = json.loads(content)
                if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
                    raise CloudTransferError("Native document archive has an unsupported schema", category="validation")
                documents[name] = data
        picks = defaultdict(set)
        for item in selected:
            if item.get("fact_type") not in {"world", "experience"} or item.get("state", "valid") != "valid":
                raise CloudTransferError("Individual Hindsight imports support valid world/experience facts; "
                                         "use a whole bank for observations or invalidated memories", category="validation")
            matches = []
            for name, doc in documents.items():
                if item.get("document_id") and doc.get("id") != item["document_id"]:
                    continue
                for index, fact in enumerate(doc["facts"]):
                    if (_semantic_item(self.provider, fact) == _semantic_item(self.provider, item)
                            and index not in picks[name]):
                        matches.append((name, index))
            if len(matches) != 1:
                raise CloudTransferError("Selected fact cannot be matched uniquely to its native document; "
                                         "export its document or whole bank instead", category="validation")
            name, index = matches[0]
            picks[name].add(index)
        output = io.BytesIO()
        dropped_links = 0
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("manifest.json", _json_bytes({"schema_version": 1, "source_bank_id": package["source_scope"]}))
            for name, indices in sorted(picks.items()):
                source = documents[name]
                doc = {key: deepcopy(source[key]) for key in ("id", "original_text", "tags", "chunks") if key in source}
                mapping = {old: new for new, old in enumerate(sorted(indices))}
                facts = []
                for index in sorted(indices):
                    fact = deepcopy(source["facts"][index])
                    relations = []
                    for relation in fact.get("causal_relations", []):
                        target = relation.get("target_fact_index")
                        if target in mapping:
                            relation["target_fact_index"] = mapping[target]
                            relations.append(relation)
                        else:
                            dropped_links += 1
                    if "causal_relations" in fact:
                        fact["causal_relations"] = relations
                    facts.append(fact)
                doc["facts"] = facts
                zipped.writestr(name, _json_bytes(doc))
                # Carry inline file attachments referenced by the chosen chunks.
                for attachment, content in files.items():
                    if not attachment.endswith(".json") and attachment != "manifest.json":
                        if attachment not in zipped.namelist():
                            zipped.writestr(attachment, content)
        archive = output.getvalue()
        self._archive_files(archive)
        return archive, dropped_links

    @staticmethod
    def _progress(callback, phase, **values):
        if callback:
            callback({"phase": phase, **values})

    def import_package(self, package: dict, target_scope: str, *, selected_ids=None,
                       progress=None, resume_state=None) -> dict:
        if self.provider == "memos_cloud":
            raise CloudTransferError("MemOS has no verified native memory import API; connect an existing scope instead",
                                     category="validation")
        _scope(target_scope)
        self._validate_package(package)
        selected = _selected(package, selected_ids)
        result = {"provider": self.provider, "target_scope": target_scope, "items": [],
                  "id_mapping": {}, "import_state": "complete", "retry_safe": False,
                  "partial": selected_ids is not None or bool(package.get("document_ids")),
                  "source_content_hash": memory_content_hash(self.provider, selected),
                  "limitations": deepcopy(package.get("limitations", []))}
        if resume_state is not None:
            if (not isinstance(resume_state, dict)
                    or resume_state.get("provider") != self.provider
                    or resume_state.get("target_scope") != target_scope
                    or resume_state.get("source_content_hash") != result["source_content_hash"]):
                raise CloudTransferError("Resume receipt belongs to a different export or target scope",
                                         category="validation")
        if self.provider == "mem0_platform_v3":
            # Validate every source row before the first write. Mem0 deduplicates
            # identical text, so conflicting metadata cannot be copied safely.
            payloads, signatures, text_signatures = {}, {}, defaultdict(set)
            for item in selected:
                source_id = item.get("id")
                if not isinstance(source_id, str) or not source_id or source_id in payloads:
                    raise CloudTransferError("Export memory IDs are missing or repeated", category="validation")
                payload = {"messages": [{"role": "assistant", "content": item.get("memory", item.get("text", ""))}],
                           "user_id": target_scope, "infer": False}
                if not isinstance(payload["messages"][0]["content"], str) or not payload["messages"][0]["content"]:
                    raise CloudTransferError("Mem0 memory text is empty", category="validation")
                if item.get("metadata") is not None:
                    payload["metadata"] = deepcopy(item["metadata"])
                if item.get("expiration_date"):
                    payload["expiration_date"] = item["expiration_date"]
                if item.get("created_at"):
                    try:
                        created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                        if created.tzinfo is None:
                            raise ValueError("Missing timezone")
                        payload["timestamp"] = int(created.timestamp())
                    except (ValueError, AttributeError, OverflowError):
                        raise CloudTransferError("Source Mem0 memory has an invalid creation timestamp", category="validation") from None
                _json_bytes(payload)
                payloads[source_id] = payload
                signatures[source_id] = _digest(_semantic_item(self.provider, item))
                text_signatures[payload["messages"][0]["content"]].add(signatures[source_id])
            if any(len(values) > 1 for values in text_signatures.values()):
                raise CloudTransferError("Mem0 exact-text deduplication cannot preserve conflicting metadata for identical text",
                                         category="validation")
            existing = self.list_memories(target_scope)
            if not existing["complete"]:
                raise CloudTransferError("Cannot confirm target Mem0 scope contents", category="pagination")
            if existing["items"] and resume_state is None:
                raise CloudTransferError("Target Mem0 scope already contains memories; a matching resume receipt is required",
                                         category="target_exists")
            expected = Counter({signature: 1 for signature in signatures.values()})
            actual = Counter(_digest(_semantic_item(self.provider, item)) for item in existing["items"])
            if actual - expected:
                raise CloudTransferError("Target Mem0 scope contains extra or conflicting memories; it cannot resume this export",
                                         category="target_exists")
            existing_ids = defaultdict(list)
            for item in existing["items"]:
                existing_ids[_digest(_semantic_item(self.provider, item))].append(item["id"])
            reconciled = 0
            for item in selected:
                ids = existing_ids.get(signatures[item["id"]])
                if ids:
                    reconciled += 1
                    result["id_mapping"][item["id"]] = list(ids)
                    result["items"].append({"source_id": item["id"], "target_ids": list(ids),
                                             "status": "reconciled"})
            unresolved = []
            operation_id = resume_state.get("operation_id") if resume_state else None
            if resume_state:
                prior_unresolved = resume_state.get("unresolved_source_ids", [])
                if (not isinstance(prior_unresolved, list)
                        or not all(isinstance(value, str) and value in signatures for value in prior_unresolved)):
                    raise CloudTransferError("Resume receipt has invalid unresolved memory IDs", category="validation")
                unresolved.extend(prior_unresolved)
                if resume_state.get("import_state") == "unknown":
                    pending_id = resume_state.get("pending_source_id")
                    if pending_id is not None:
                        if pending_id not in signatures:
                            raise CloudTransferError("Unknown import receipt has an invalid pending memory ID", category="validation")
                        unresolved.append(pending_id)
                for row in resume_state.get("items", []):
                    if not isinstance(row, dict) or row.get("status") not in {"succeeded", "reconciled", "deduplicated"}:
                        continue
                    source_id = row.get("source_id")
                    if source_id not in signatures:
                        raise CloudTransferError("Resume receipt contains a different source memory ID", category="validation")
                    if not existing_ids.get(signatures[source_id]):
                        # Even a complete paginated list can lag behind a
                        # confirmed write. Absence does not authorize repeating
                        # rows that previously returned SUCCEEDED.
                        unresolved.append(source_id)
                        operation_id = operation_id or row.get("event_id")
            # Absence on a read replica cannot prove that a timed-out ADD did
            # not commit. Resolve its exact pending row by content before
            # submitting anything else; a later retry may see it appear.
            missing_unresolved = [source_id for source_id in dict.fromkeys(unresolved)
                                  if not existing_ids.get(signatures[source_id])]
            unknown_without_id = bool(resume_state and resume_state.get("import_state") == "unknown"
                                      and not unresolved and reconciled != len(selected))
            if missing_unresolved or unknown_without_id:
                result.update(import_state="unknown", operation_id=operation_id,
                              unresolved_source_ids=missing_unresolved,
                              error="Previous import acknowledgement remains unresolved; its memory is not yet visible")
                if missing_unresolved:
                    result["pending_source_id"] = missing_unresolved[0]
                result["counts"] = {"selected": len(selected), "reconciled": reconciled,
                                    "submitted_successes": 0}
                return result
            if reconciled:
                self._progress(progress, "import_reconciled", completed=reconciled,
                               total=len(selected), target_scope=target_scope)
            operation_id = None
            submitted = 0
            completed = reconciled
            try:
                for item in selected:
                    if item["id"] in result["id_mapping"]:
                        continue
                    # An identical source row may have been added earlier in
                    # this attempt. Preserve its mapping without another ADD.
                    ids = existing_ids.get(signatures[item["id"]])
                    if ids:
                        result["id_mapping"][item["id"]] = list(ids)
                        result["items"].append({"source_id": item["id"], "target_ids": list(ids),
                                                 "status": "deduplicated"})
                        completed += 1
                        continue
                    operation_id = None
                    result["pending_source_id"] = item["id"]
                    self._progress(progress, "import_submitting", completed=completed, total=len(selected),
                                   source_id=item["id"], target_scope=target_scope)
                    receipt = self._request("POST", "/v3/memories/add/", payloads[item["id"]], mutation=True)
                    operation_id = receipt.get("event_id")
                    self._progress(progress, "operation_accepted", completed=completed, total=len(selected),
                                   operation_id=operation_id, source_id=item["id"], target_scope=target_scope)
                    status = str(receipt.get("status", "")).lower()
                    if status in {"pending", "running", "processing"}:
                        receipt = self._poll(target_scope, operation_id, mem0=True)
                    elif status != "succeeded":
                        raise _UnknownMutation("Mem0 add has no confirmed successful event", category="unknown_commit")
                    new_ids = self._mem0_ids(receipt)
                    result["id_mapping"][item["id"]] = new_ids
                    result["items"].append({"source_id": item["id"], "target_ids": new_ids,
                                             "event_id": operation_id, "status": "succeeded"})
                    existing_ids[signatures[item["id"]]] = new_ids
                    submitted += 1
                    completed += 1
                    self._progress(progress, "import_completed", completed=completed, total=len(selected),
                                   operation_id=operation_id, target_scope=target_scope)
                    result.pop("pending_source_id", None)
            except (_UnknownMutation, CloudTransferCancelled) as exc:
                result.update(import_state="unknown", operation_id=operation_id, error=str(exc))
            except CloudTransferError as exc:
                if operation_id and exc.category != "operation_failed":
                    result.update(import_state="unknown", operation_id=operation_id, error=str(exc))
                else:
                    # A definite ADD rejection can be retried after addressing
                    # quota/authentication. Successful prior rows are re-read
                    # and skipped on that retry. No unknown write is retried.
                    result.update(import_state="partial", retry_safe=True, operation_id=operation_id,
                                  error=str(exc), rejected_source_id=result.get("pending_source_id"))
                    result["counts"] = {"selected": len(selected), "reconciled": reconciled,
                                        "submitted_successes": submitted}
                    exc.partial_result = deepcopy(result)
                    raise
            result["counts"] = {"selected": len(selected), "reconciled": reconciled,
                                "submitted_successes": submitted}
            result["deduplication"] = "Mem0 may collapse exact repeated text; verify unique text/metadata after import"
            return result
        if (resume_state is not None and
                (resume_state.get("import_state") in {"unknown", "complete"}
                 or resume_state.get("carrier_creation_started")
                 or resume_state.get("import_submission_started"))):
            # A missing target is not proof that an asynchronous restore was
            # never submitted. Resume reads only; never recreate its carrier
            # or upload the archive a second time after an ambiguous response.
            for key in ("carrier_scope", "operation_scope", "carrier_creation_started",
                        "import_submission_started", "operation_id"):
                if key in resume_state:
                    result[key] = deepcopy(resume_state[key])
            operation_scope = result.get("operation_scope")
            operation_id = result.get("operation_id")
            result.update(import_state="unknown", error="Previous Hindsight submission needs reconciliation")
            if operation_id and operation_scope:
                _scope(operation_scope)
                valid_carrier = (result.get("carrier_scope") == operation_scope
                                 and operation_scope.startswith("transfer_")
                                 and operation_scope != target_scope)
                valid_merge = selected_ids is not None and operation_scope == target_scope
                if not (valid_carrier or valid_merge):
                    raise CloudTransferError("Resume operation belongs to an invalid carrier bank",
                                             category="validation")
                try:
                    done = self._poll(operation_scope, operation_id)
                    result.update(import_state="complete", counts=done.get("result_metadata", {}))
                    result.pop("error", None)
                except (CloudTransferError, CloudTransferCancelled) as exc:
                    result["error"] = str(exc)
            return result
        if self._bank_exists(target_scope):
            raise CloudTransferError("Target Hindsight bank already exists; verify or choose a fresh bank",
                                     category="target_exists")
        archive = self._decode_archive(package)
        mode = "restore"
        if selected_ids is not None:
            archive, dropped_links = self._fact_subset_archive(package, selected)
            result["dropped_causal_links"] = dropped_links
            result["limitations"].append("Selected facts omit bank-level observations, mental models and directives")
            mode = "merge"
        operation_id = None
        operation_scope = target_scope
        try:
            if mode == "merge":
                # The service reserves a new UUID scope. Check inventory before
                # using Hindsight's documented create-or-update endpoint; never
                # use it on an existing bank and never use replace conflicts.
                self._request("PUT", self._bank(target_scope), {}, mutation=True)
            else:
                # restore creates target_bank_id in the background. Its URL
                # bank must already exist, and owns the operation receipt.
                # Reserve an isolated empty carrier; leave the new target
                # absent and never PUT an existing user memory bank.
                operation_scope = "transfer_" + uuid.uuid4().hex
                result.update(carrier_scope=operation_scope, operation_scope=operation_scope)
                if self._bank_exists(operation_scope):
                    raise CloudTransferError("Transfer carrier bank already exists", category="target_exists")
                result["carrier_creation_started"] = True
                self._progress(progress, "carrier_create_submitting", target_scope=target_scope,
                               carrier_scope=operation_scope, operation_scope=operation_scope,
                               carrier_creation_started=True)
                self._request("PUT", self._bank(operation_scope),
                              {"name": "Memory transfer operation carrier"}, mutation=True)
                carrier = self.list_memories(operation_scope)
                if not carrier["complete"] or carrier["items"]:
                    raise _UnknownMutation("Transfer carrier could not be confirmed empty", category="unknown_commit")
                self._progress(progress, "carrier_ready", target_scope=target_scope,
                               carrier_scope=operation_scope, operation_scope=operation_scope,
                               carrier_creation_started=True)
            boundary = "memory-transfer-" + uuid.uuid4().hex
            body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"transfer.zip\"\r\n"
                    "Content-Type: application/zip\r\n\r\n").encode("ascii") + archive + f"\r\n--{boundary}--\r\n".encode("ascii")
            query = {"mode": mode, "target_bank_id": target_scope,
                     "include_data": "true", "include_bank_config": "true" if mode == "restore" else "false",
                     "document_conflict": "skip", "include_history": "false"}
            result.update(operation_scope=operation_scope, import_submission_started=True)
            self._progress(progress, "import_submitting", completed=0, total=len(selected), target_scope=target_scope,
                           operation_scope=operation_scope, carrier_scope=result.get("carrier_scope"),
                           import_submission_started=True)
            receipt = self._request("POST", self._bank(operation_scope) + "/transfer/import?" + urlencode(query),
                                    body=body, content_type="multipart/form-data; boundary=" + boundary, mutation=True)
            operation_id = receipt.get("operation_id")
            self._progress(progress, "operation_accepted", completed=0, total=len(selected),
                           operation_id=operation_id, target_scope=target_scope, operation_scope=operation_scope,
                           carrier_scope=result.get("carrier_scope"), import_submission_started=True)
            if not operation_id:
                raise _UnknownMutation("Hindsight import has no usable operation receipt", category="unknown_commit")
            done = self._poll(operation_scope, operation_id)
            result.update(operation_id=operation_id, counts=done.get("result_metadata", {}))
            result["items"] = [{"source_id": item["id"], "status": "operation_completed"} for item in selected]
            self._progress(progress, "import_completed", completed=len(selected), total=len(selected),
                           operation_id=operation_id, target_scope=target_scope, operation_scope=operation_scope,
                           carrier_scope=result.get("carrier_scope"), import_submission_started=True)
        except (_UnknownMutation, CloudTransferCancelled) as exc:
            result.update(import_state="unknown", operation_id=operation_id, error=str(exc))
        except CloudTransferError as exc:
            if operation_id and exc.category != "operation_failed":
                result.update(import_state="unknown", operation_id=operation_id, error=str(exc))
            else:
                raise
        return result

    @staticmethod
    def _mem0_ids(receipt):
        rows = receipt.get("results", [])
        return [row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]

    @staticmethod
    def _template_semantics(value, section=None):
        # IDs/grounding references are remapped by native transfer. Preserve
        # substantive config, text, query, trigger, directive and model fields.
        if isinstance(value, dict):
            return {key: CloudMemoryTransfer._template_semantics(child, key) for key, child in value.items()
                    if key not in {"id", "bank_id", "created_at", "updated_at", "based_on",
                                   "last_refreshed_at", "next_refresh_at", "source_memory_ids"}}
        if isinstance(value, list):
            children = [CloudMemoryTransfer._template_semantics(child) for child in value]
            return (sorted(children, key=lambda child: _json_bytes(child)) if section in
                    {"mental_models", "directives", "webhooks"} else children)
        return value

    def verify(self, package: dict, target_scope: str, selected_ids=None) -> dict:
        _scope(target_scope)
        self._validate_package(package)
        selected = _selected(package, selected_ids)
        actual = self.list_memories(target_scope)
        expected_values = Counter(_digest(_semantic_item(self.provider, item)) for item in selected)
        actual_values = Counter(_digest(_semantic_item(self.provider, item)) for item in actual["items"])
        deduplicated = False
        if self.provider == "mem0_platform_v3":
            # Mem0's exact-text hash deduplication cannot preserve two rows with
            # the same text and conflicting metadata. Detect that explicitly.
            by_text = defaultdict(set)
            for item in selected:
                semantic = _semantic_item(self.provider, item)
                by_text[semantic["text"]].add(_digest(semantic))
            conflicting = any(len(signatures) > 1 for signatures in by_text.values())
            deduplicated = any(count > 1 for count in expected_values.values())
            if deduplicated:
                expected_values = Counter({key: 1 for key in expected_values})
        else:
            conflicting = False
        missing = sum((expected_values - actual_values).values())
        extra = sum((actual_values - expected_values).values())
        config_match = None
        reason = None
        if self.provider == "mem0_platform_v3" and package.get("project_config") is not None:
            config_match = package["project_config"] == self._project_config()
        elif self.provider == "memos_cloud":
            # No mutation or reconstruction: verify every exposed native field
            # across all six categories against the frozen full source readback.
            config_match = (package.get("fidelity") == "native_readback" and selected_ids is None
                            and isinstance(package.get("data"), dict)
                            and all(key in package["data"] for key in MEMOS_CATEGORIES))
        elif (self.provider == "hindsight_cloud" and selected_ids is None
              and not package.get("document_ids")):
            config = self._request("GET", self._bank(target_scope) + "/config")
            template = self._request("GET", self._bank(target_scope) + "/export")
            config_match = (
                self._template_semantics(package.get("bank_config")) == self._template_semantics(config)
                and self._template_semantics(package.get("bank_template")) == self._template_semantics(template)
            )
        if conflicting:
            reason = "Mem0 exact-text deduplication cannot preserve conflicting metadata for identical text"
        elif not actual["complete"]:
            reason = actual.get("reason", "Target pagination is incomplete")
        elif missing or extra:
            reason = "Target memory contents do not match the exported selection"
        elif config_match is False:
            reason = "Target provider configuration does not match the source export"
        verified = reason is None
        mapping = defaultdict(list)
        for item in actual["items"]:
            mapping[_digest(_semantic_item(self.provider, item))].append(item["id"])
        id_mapping = {item["id"]: list(mapping.get(_digest(_semantic_item(self.provider, item)), [])) for item in selected}
        return {"verified": verified, "reason": reason, "provider": self.provider,
                "target_scope": target_scope, "complete": actual["complete"],
                "config_match": config_match, "deduplicated": deduplicated,
                "counts": {"source": len(selected), "expected_unique": sum(expected_values.values()),
                           "target": len(actual["items"]), "missing": missing, "extra": extra},
                "content_hash": memory_content_hash(self.provider, actual["items"]),
                "source_content_hash": memory_content_hash(self.provider, selected),
                "id_mapping": id_mapping, "partial": selected_ids is not None or bool(package.get("document_ids")),
                "limitations": deepcopy(package.get("limitations", []))}


__all__ = ["CloudMemoryTransfer", "CloudTransferError", "CloudTransferCancelled",
           "CloudTransferResponse", "MAX_EXPORT_BYTES", "memory_content_hash", "classify_transfer_error"]
