"""Read memory artifacts and run durable, user-triggered cloud transfers."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from .memory_account_store import MemoryAccountStore, PROVIDERS, is_default_account, now, public_error
from .memory_results import inventory
from .validation_sources import _snapshot_hash, read_memory_source


def _read(path):
    path = Path(path)
    if not path.is_file():
        return None
    if path.stat().st_size > 160 * 1024 * 1024:
        raise ValueError("memory artifact exceeds the size limit")
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _receipt(value, *secrets):
    """Mask actual account keys without treating memory IDs as secret fields."""
    if isinstance(value, dict):
        return {key: _receipt(item, *secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_receipt(item, *secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


class MemoryWorkspace:
    def __init__(self, platform, *, transfer_factory=None):
        self.platform = platform
        self.store = MemoryAccountStore(platform.store.db_path)
        self.root = self.store.workspace_dir
        self._threads = {}
        self._cancel = {}
        self._lock = threading.RLock()
        self._owner = uuid.uuid4().hex
        self.transfer_factory = transfer_factory
        for provider, name in (("mem0_platform_v3", "MEM0_API_KEY"), ("hindsight_cloud", "HINDSIGHT_API_KEY"),
                               ("memos_cloud", "MEMOS_API_KEY")):
            self.store.ensure_environment_account(provider, env_name=name, project_root=platform.project_root)

    def client(self, account, cancel_event=None):
        if self.transfer_factory:
            return self.transfer_factory(account)
        from .memory_transfer import CloudMemoryTransfer
        return CloudMemoryTransfer(account, cancel_event=cancel_event)

    def source(self, run_id, agent_id=None, round_index=None):
        run = self.platform.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.get("runner_type") == "tool_selection":
            raise ValueError("memory viewing requires a first-stage run")
        if round_index is not None and (type(round_index) is not int or round_index < 1):
            raise ValueError("round_index must be a positive integer")
        folder = Path(run["output_dir"]).resolve() / "memory"
        paths = sorted(folder.glob("memory-round-*.json"))
        if round_index is not None:
            paths = [folder / f"memory-round-{round_index:03d}.json"]
        if not paths or not paths[-1].is_file():
            raise ValueError("no saved memory snapshot is available")
        path = paths[-1]
        if path.is_symlink():
            raise PermissionError("memory artifact must not be a symbolic link")
        payload = _read(path)
        candidates = {key: value for key, value in payload.items()
                      if isinstance(value, dict) and isinstance(value.get("snapshot"), dict)}
        if agent_id is None:
            if len(candidates) != 1:
                raise ValueError("choose one source Agent")
            agent_id = next(iter(candidates))
        if agent_id not in candidates:
            raise ValueError("source Agent is absent from the selected snapshot")
        snapshot = candidates[agent_id]["snapshot"]
        selected_round = int(path.stem.removeprefix("memory-round-"))
        snapshot_hash = _snapshot_hash(snapshot)
        # Match the exact restorable copy used by stage two, including legacy repair.
        if run.get("status") in {"completed", "completed_with_errors"}:
            try:
                frozen = read_memory_source(run, agent_id=agent_id, round_index=selected_round)
                snapshot_hash = frozen["snapshot_sha256"]
            except (ValueError, KeyError, OSError):
                pass
        strategy = snapshot.get("strategy")
        options = snapshot.get("options") or {}
        scope = options.get("scope") or {}
        physical_scope = ((options.get("target_bank") or options.get("remote_scope_id") or scope.get("id"))
                          if strategy == "hindsight_cloud" else
                          (options.get("remote_scope_id") or scope.get("id") or scope.get("user_id"))
                          if strategy == "memos_cloud" else scope.get("user_id"))
        key = hashlib.sha256(json.dumps([run_id, agent_id, selected_round, snapshot_hash]).encode()).hexdigest()
        return {"run": run, "snapshot": snapshot, "run_id": run_id,
                "agent_id": agent_id, "round_index": selected_round, "strategy": strategy,
                "snapshot_sha256": snapshot_hash, "scope": physical_scope,
                "cache_key": key, "snapshot_file": path.name}

    def record(self, source):
        run = source["run"]
        cache = self.store.get_cache(source["cache_key"]) or {}
        strategy = source["strategy"]
        legacy = self.legacy_export(source)
        return {key: source[key] for key in ("run_id", "agent_id", "round_index", "strategy", "snapshot_sha256", "scope")} | {
            "experiment_id": run.get("experiment_id"), "experiment_name": run.get("experiment_name"),
            "group_id": run.get("group_id"), "run_status": run.get("status"),
            "account_id": cache.get("account_id"), "pull_status": cache.get("pull_status",
                "complete" if legacy else "not_fetched" if strategy in PROVIDERS else "local"),
            "fetched_at": cache.get("fetched_at", (legacy or {}).get("exported_at")),
            "total": cache.get("item_count", len(legacy.get("items", [])) if legacy else
                               None if strategy in PROVIDERS else len(inventory(source["snapshot"]))),
            "write_count": len(source["snapshot"].get("events") or []),
            "error": cache.get("error"), "snapshot": source["snapshot_file"],
            "available_rounds": [int(p.stem.removeprefix("memory-round-")) for p in
                                 sorted((Path(run["output_dir"]) / "memory").glob("memory-round-*.json"))],
        }

    def legacy_export(self, source):
        path = Path(source["run"]["output_dir"]) / "memory" / f"remote-export-round-{source['round_index']:03d}.json"
        exported = _read(path) or {}
        item = exported.get(source["agent_id"])
        if isinstance(item, dict) and isinstance(item.get("items"), list):
            return item
        if source["strategy"] == "memos_cloud":
            provider_snapshot = (source["snapshot"].get("options") or {}).get("provider_snapshot")
            if isinstance(provider_snapshot, dict) and provider_snapshot.get("complete") is True:
                from .memory_transfer import memos_snapshot_package
                return memos_snapshot_package(provider_snapshot, source["scope"])
        return None

    def cached_export(self, source):
        cache = self.store.get_cache(source["cache_key"])
        if cache and cache.get("package_file"):
            return _read(self._artifact(cache["package_file"]))
        return self.legacy_export(source)

    def _artifact(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path.is_symlink():
            raise PermissionError("memory cache path escapes its workspace")
        return path

    def index(self, *, strategy=None, group_id=None, run_id=None, agent_id=None,
              round_index=None, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("invalid memory index pagination")
        # Page lightweight registry rows first. Snapshot bodies are read only for this page.
        where = ["e.runner_type <> 'tool_selection'"]
        params = []
        for sql, value in (("r.run_id=?", run_id), ("e.group_id=?", group_id),
                           ("e.display_memory_strategy=?", strategy)):
            if value:
                where.append(sql)
                params.append(value)
        with closing(self.platform.store._connect()) as connection:
            query = " FROM experiment_runs r JOIN experiments e USING(experiment_id) WHERE " + " AND ".join(where)
            total = connection.execute("SELECT COUNT(*)" + query, params).fetchone()[0]
            ids = [row[0] for row in connection.execute("SELECT r.run_id" + query +
                   " ORDER BY r.created_at DESC,r.run_id LIMIT ? OFFSET ?", [*params, limit, offset])]
        rows = []
        for identifier in ids:
            run = self.platform.store.get_run(identifier)
            folder = Path(run["output_dir"]) / "memory"
            paths = sorted(folder.glob("memory-round-*.json"))
            if round_index:
                paths = [folder / f"memory-round-{round_index:03d}.json"]
            if not paths or not paths[-1].is_file():
                continue
            try:
                payload = _read(paths[-1]) or {}
                agents = [agent_id] if agent_id else list(payload)
                for selected in agents:
                    source = self.source(identifier, selected, round_index)
                    if not strategy or source["strategy"] == strategy:
                        rows.append(self.record(source))
            except (ValueError, OSError, KeyError):
                continue
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "next_offset": offset + len(ids), "has_more": offset + len(ids) < total}

    def check_account(self, account_id):
        account = self.store.resolve_account(account_id)
        try:
            identity = self.client(account).identify()
            identity = {**identity, "verified": bool(identity.get("identity_verified", identity.get("verified", False)))}
            return self.store.record_identity(account_id, identity, expected_revision=account.get("credential_revision", 0))
        except Exception as exc:
            category = getattr(exc, "category", "check_failed")
            status = {"quota_exhausted": "quota_exhausted", "authentication": "invalid_credentials",
                      "invalid_credentials": "invalid_credentials", "forbidden": "forbidden"}.get(category, "unchecked")
            value = self.store.set_account_state(account_id, status,
                last_error=public_error({"category": category, "message": str(exc)}, account["api_key"]),
                expected_revision=account.get("credential_revision", 0))
            return value

    def _provider_sources(self, provider):
        """Enumerate local first-stage snapshots across all experiment groups."""
        with closing(self.platform.store._connect()) as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT r.run_id FROM experiment_runs r JOIN experiments e USING(experiment_id) "
                "WHERE e.runner_type <> 'tool_selection' AND e.display_memory_strategy=? "
                "ORDER BY r.created_at DESC,r.run_id", (provider,))]
        for run_id in ids:
            run = self.platform.store.get_run(run_id)
            paths = sorted((Path(run["output_dir"]) / "memory").glob("memory-round-*.json"))
            if not paths:
                continue
            try:
                payload = _read(paths[-1]) or {}
                agents = [agent_id for agent_id, value in payload.items()
                    if isinstance(value, dict) and isinstance(value.get("snapshot"), dict)
                    and value["snapshot"].get("strategy") == provider]
                for agent_id in agents:
                    source = self.source(run_id, agent_id)
                    if source["strategy"] == provider:
                        yield source
            except (ValueError, OSError, KeyError):
                continue

    def _source_owner(self, source, replicas):
        """Use recorded local ownership, then the legacy environment convention."""
        options = source["snapshot"].get("options") or {}
        if options.get("account_id"):
            return options["account_id"], "snapshot"
        origins = [row for row in replicas if row.get("origin")]
        if origins:
            return origins[0]["account_id"], "origin_replica"
        cache = self.store.get_cache(source["cache_key"]) or {}
        if cache.get("account_id"):
            return cache["account_id"], "pull_cache"
        return "env-" + source["strategy"], "legacy_default"

    @staticmethod
    def _replica_available(replica, account):
        return bool(not is_default_account(account) and account.get("enabled")
            and account.get("status") not in {"disabled", "quota_exhausted", "exhausted", "invalid_credentials", "forbidden", "cooldown"}
            and replica.get("verified") and replica.get("complete") and replica.get("physical_scope")
            and replica.get("status") in {"ready", "verified", "completed"}
            and replica.get("credential_revision", 0) == account.get("credential_revision", 0))

    def account_inventory(self, account_id):
        """Show local source records or account replicas without touching cloud APIs."""
        account = self.store.get_account(account_id)
        default = is_default_account(account)
        provider = account["provider"]
        items = []
        replicas = self.store.list_replicas()
        if default:
            for source in self._provider_sources(provider):
                matches = [row for row in replicas if row.get("source_run_id") == source["run_id"]
                    and row.get("agent_id") == source["agent_id"]
                    and row.get("snapshot_sha256") == source["snapshot_sha256"]]
                owner, binding = self._source_owner(source, matches)
                if owner != account_id:
                    continue
                record = self.record(source)
                importable = bool(source["scope"] and source["run"].get("status") in {
                    "completed", "completed_with_errors"})
                items.append({**record, "account_id": account_id, "source_account_id": account_id,
                    "source_account_binding": binding, "replicas": matches, "importable": importable,
                    "available_for_stage_two": False, "status": record["pull_status"],
                    "run_name": record["experiment_name"], "record_count": record["total"]})
        else:
            seen = set()
            for replica in replicas:
                if replica.get("account_id") != account_id or replica.get("provider") != provider:
                    continue
                key = (replica["source_run_id"], replica["agent_id"], replica["snapshot_sha256"],
                       replica["memory_set_id"], replica["revision_hash"])
                if key in seen:
                    continue
                seen.add(key)
                try:
                    source = self.source(replica["source_run_id"], replica["agent_id"], replica.get("round_index"))
                    record = self.record(source)
                    source_matches = (source["snapshot_sha256"] == replica["snapshot_sha256"]
                        and source["strategy"] == provider and source["run"].get("status") in {
                            "completed", "completed_with_errors"})
                except (ValueError, OSError, KeyError):
                    source_matches = False
                    record = {"run_id": replica["source_run_id"], "agent_id": replica["agent_id"],
                        "round_index": replica.get("round_index"), "strategy": provider,
                        "experiment_name": None, "total": None}
                available = source_matches and self._replica_available(replica, account)
                count = (replica.get("verification") or {}).get("counts")
                if isinstance(count, dict):
                    count = count.get("target")
                if type(count) is not int:
                    count = replica.get("item_count", record["total"])
                items.append({**record, "account_id": account_id, "source_account_id": account_id,
                    "snapshot_sha256": replica["snapshot_sha256"], "scope": replica["physical_scope"],
                    "memory_set_id": replica["memory_set_id"], "memory_revision": replica["revision_hash"],
                    "replicas": [replica], "importable": False, "available_for_stage_two": available,
                    "status": replica.get("status"), "run_name": record["experiment_name"],
                    "record_count": count, "total": count,
                    "pool_exclusion_reason": replica.get("pool_exclusion_reason")})
        return {"account": account, "provider": provider, "is_default": default, "export_only": default,
                "items": items, "total": len(items), "next_account_name": self.store.next_account_name(provider)}

    def start_account_import(self, account_id, payload, *, background=True):
        """Queue independently auditable complete-source copies selected by the user."""
        if not isinstance(payload, dict) or set(payload) - {"sources", "target_account_ids"}:
            raise ValueError("invalid account memory import request; select complete experiment memories")
        account = self.store.get_account(account_id)
        if not is_default_account(account):
            raise ValueError("select experiment memories from the default export account")
        sources = payload.get("sources")
        if not isinstance(sources, list) or not sources or len(sources) > 100:
            raise ValueError("choose 1 to 100 experiment memory sources")
        with self.platform._launch_lock:
            inventory = self.account_inventory(account_id)
            available = {(row["run_id"], row["agent_id"], row["round_index"]): row
                         for row in inventory["items"] if row["importable"]}
            requests, seen = [], set()
            for selected in sources:
                if not isinstance(selected, dict) or set(selected) - {"run_id", "agent_id", "round_index"}:
                    raise ValueError("each import source must identify a complete experiment memory")
                source = self.source(selected.get("run_id"), selected.get("agent_id"), selected.get("round_index"))
                key = (source["run_id"], source["agent_id"], source["round_index"])
                if key not in available:
                    raise ValueError("selected memory does not belong to this default account or is not ready for export")
                if key in seen:
                    continue
                seen.add(key)
                requests.append(self._transfer_request("sync", {**selected, "source_account_id": account_id,
                    "target_account_ids": payload.get("target_account_ids")}))
            # Validate the whole selection before persisting or launching any copy.
            jobs = [self.store.create_job("sync", request) for request in requests]
        if background:
            self._launch_import_batch([job["job_id"] for job in jobs])
        else:
            self._execute_import_batch([job["job_id"] for job in jobs])
        return {"jobs": [self.store.get_job(job["job_id"]) for job in jobs]}

    def _launch_import_batch(self, job_ids):
        with self._lock:
            for job_id in job_ids:
                self._cancel[job_id] = threading.Event()
            thread = threading.Thread(target=self._execute_import_batch, args=(job_ids,), daemon=True)
            for job_id in job_ids:
                self._threads[job_id] = thread
            thread.start()

    def _execute_import_batch(self, job_ids):
        """Freeze provider identities once before jobs sharing accounts execute."""
        claimed = []
        keys = []
        done = threading.Event()
        try:
            for job_id in job_ids:
                if self.store.claim_job(job_id, self._owner):
                    claimed.append(job_id)
                    self.store.update_job(job_id, status="running", phase="checking_accounts")
            if not claimed:
                return
            def heartbeat():
                while not done.wait(30):
                    for job_id in claimed:
                        self.store.claim_job(job_id, self._owner)
            threading.Thread(target=heartbeat, daemon=True).start()
            jobs = [self.store.get_job(job_id) for job_id in claimed]
            revisions = {}
            for job in jobs:
                revisions.update(job["request"]["account_revisions"])
            for account_id, revision in revisions.items():
                if all(self.store.get_job(job_id).get("cancel_requested") for job_id in claimed):
                    return
                account = self.store.resolve_account(account_id)
                keys.append(account["api_key"])
                if account.get("credential_revision", 0) != revision:
                    raise ValueError("account context changed before batch import; select the memories again")
                identity = self.client(account).identify()
                identity = {**identity, "verified": bool(identity.get("identity_verified", identity.get("verified", False)))}
                checked = self.store.record_identity(account_id, identity, expected_revision=revision)
                for job in jobs:
                    if account_id in job["request"]["account_revisions"]:
                        job["request"]["account_revisions"][account_id] = checked.get("credential_revision", 0)
                        self.store.update_job(job["job_id"], request=job["request"])
            for job_id in claimed:
                job = self.store.get_job(job_id)
                if job.get("cancel_requested"):
                    self.store.update_job(job_id, status="cancelled", phase="cancelled", completed_at=now())
                    continue
                self.execute(job_id, cancel_event=self._cancel.get(job_id))
        except Exception as exc:
            for job_id in claimed:
                job = self.store.get_job(job_id)
                if job["status"] not in {"completed", "cancelled"}:
                    self.store.update_job(job_id, status="cancelled" if job.get("cancel_requested") else "failed",
                        phase="checking_accounts", error=public_error(str(exc), *keys), completed_at=now())
        finally:
            done.set()
            for job_id in claimed:
                job = self.store.get_job(job_id)
                if job.get("cancel_requested") and job["status"] != "completed":
                    self.store.update_job(job_id, status="cancelled", phase="cancelled", completed_at=now())
                self.store.release_job(job_id, self._owner)

    def start(self, kind, payload, *, background=True):
        with self.platform._launch_lock:
            job = self._create_transfer_job(kind, payload)
        if background:
            self._launch(job["job_id"])
        else:
            self.execute(job["job_id"])
        return self.store.get_job(job["job_id"])

    def _create_transfer_job(self, kind, payload):
        return self.store.create_job(kind, self._transfer_request(kind, payload))

    def _transfer_source_state(self, source, kind):
        run = source["run"]
        status = run.get("status")
        complete = status in {"completed", "completed_with_errors"}
        if complete:
            return status, True
        if kind == "pull" and status in {"failed", "interrupted", "paused", "cancelled"}:
            session = run.get("screen_session")
            active = bool(run.get("screen_active") or
                          session and self.platform.launcher.is_active(str(session)))
            if not active:
                return status, False
        raise ValueError("finish the first-stage run before pulling or synchronizing cloud memory")

    def _transfer_request(self, kind, payload):
        allowed = {"run_id", "agent_id", "round_index", "account_id", "source_account_id",
                   "target_account_ids", "selected_ids", "document_ids", "physical_scope"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("invalid memory transfer request")
        source = self.source(payload.get("run_id"), payload.get("agent_id"), payload.get("round_index"))
        if source["strategy"] not in PROVIDERS:
            raise ValueError("only hosted Mem0/Hindsight/MemOS memories require transfer")
        source_status, source_complete = self._transfer_source_state(source, kind)
        if not source["scope"]:
            raise ValueError("source has no cloud memory scope")
        if kind == "connect-existing":
            if source["strategy"] != "memos_cloud":
                raise ValueError("existing-scope connections currently support MemOS only")
            if set(payload) - {"run_id", "agent_id", "round_index", "account_id", "physical_scope"}:
                raise ValueError("existing-scope connection accepts one target account and one user_id")
            from .memory_transfer import _scope
            scope = _scope(payload.get("physical_scope"))
            account = self.store.resolve_account(payload.get("account_id"), source["strategy"])
            if not account.get("enabled") or is_default_account(account):
                raise ValueError("choose an enabled non-default MemOS experiment account")
            package = self.legacy_export(source)
            if not package or package.get("complete") is not True:
                raise ValueError("a complete frozen MemOS source export is required before connecting an existing scope")
            return {**{key: source[key] for key in ("run_id", "agent_id", "round_index", "snapshot_sha256", "strategy", "scope", "cache_key")},
                    "account_id": account["account_id"], "physical_scope": scope,
                    "account_revisions": {account["account_id"]: account.get("credential_revision", 0)}}
        cache = self.store.get_cache(source["cache_key"]) or {}
        account_id = payload.get("source_account_id") or payload.get("account_id") or cache.get("account_id")
        if not account_id:
            raise ValueError("choose the source account containing this memory")
        account = self.store.resolve_account(account_id, source["strategy"])
        if not account.get("enabled"):
            raise ValueError("source account is disabled")
        request = {key: source[key] for key in ("run_id", "agent_id", "round_index", "snapshot_sha256", "strategy", "scope", "cache_key")}
        if kind == "pull":
            request.update(source_run_status=source_status, source_ingestion_complete=source_complete)
        request["source_account_id"] = account_id
        request["account_revisions"] = {account_id: account.get("credential_revision", 0)}
        if kind == "sync":
            if source["strategy"] == "memos_cloud":
                raise ValueError("MemOS Cloud does not expose a verified native memory import API; "
                                 "export and read-only search are supported, but account replication is unavailable")
            targets = payload.get("target_account_ids")
            if not isinstance(targets, list) or not targets or len(targets) > 50 or any(not isinstance(t, str) for t in targets):
                raise ValueError("choose 1 to 50 target accounts")
            targets = list(dict.fromkeys(targets))
            if account_id in targets:
                raise ValueError("source account cannot also be a sync target")
            for target in targets:
                resolved = self.store.resolve_account(target, source["strategy"])
                if not resolved.get("enabled"):
                    raise ValueError("target account is disabled")
                if is_default_account(resolved):
                    raise ValueError("default accounts are export-only and cannot be sync targets")
                request["account_revisions"][target] = resolved.get("credential_revision", 0)
            request["target_account_ids"] = targets
            for key in ("selected_ids", "document_ids"):
                selected = payload.get(key)
                if selected is not None:
                    if not isinstance(selected, list) or not selected or any(not isinstance(v, str) or not v for v in selected):
                        raise ValueError(f"{key} must be a nonempty ID list; omit it to copy the entire memory set")
                    request[key] = list(dict.fromkeys(selected))
        elif kind != "pull":
            raise ValueError("unsupported memory job")
        return request

    def _launch(self, job_id):
        with self._lock:
            thread = self._threads.get(job_id)
            if thread and thread.is_alive():
                raise ValueError("memory job is already running")
            event = threading.Event()
            thread = threading.Thread(target=self.execute, args=(job_id,), kwargs={"cancel_event": event}, daemon=True)
            self._cancel[job_id] = event
            self._threads[job_id] = thread
            thread.start()

    def cancel(self, job_id):
        job = self.store.get_job(job_id)
        if job["status"] == "completed":
            raise ValueError("completed transfers cannot be cancelled")
        if job_id in self._cancel:
            self._cancel[job_id].set()
        return self.store.update_job(job_id, cancel_requested=True,
            status="cancelled" if job["status"] not in {"running", "verifying"} else job["status"])

    def retry(self, job_id):
        with self.platform._launch_lock:
            with self._lock:
                thread = self._threads.get(job_id)
                if thread and thread.is_alive():
                    raise ValueError("memory job is already running")
            job = self.store.get_job(job_id)
            if job.get("history_only") or job.get("copied_from_job_id"):
                raise PermissionError(
                    "copied memory transfer history cannot be retried; create a new transfer"
                )
            if job["status"] in {"queued", "running", "verifying"}:
                # Check the persisted lease, not only threads owned by this server.
                if not self.store.claim_job(job_id, self._owner):
                    raise ValueError("memory job is still owned by an active worker")
            if job["status"] == "completed":
                return job
            self.store.update_job(job_id, status="queued", cancel_requested=False, error=None)
        self._launch(job_id)
        return self.store.get_job(job_id)

    def _binding(self, request, package, account_id, scope, *, complete, verified, **extra):
        content_hash = package.get("content_hash") or _snapshot_hash({"items": package.get("items", [])})
        revision = _snapshot_hash({"content_hash": content_hash,
            "project_config": package.get("project_config"), "bank_config": package.get("bank_config"),
            "bank_template": package.get("bank_template")})
        selected = request.get("selected_ids") or request.get("document_ids")
        item_count = len([row for row in package.get("items", [])
                          if not request.get("selected_ids") or row.get("id") in request["selected_ids"]])
        memory_set_id = hashlib.sha256(json.dumps([request["run_id"], request["agent_id"],
            request["snapshot_sha256"], sorted(selected) if selected else None], sort_keys=True).encode()).hexdigest()
        return self.store.save_replica({
            "source_run_id": request["run_id"], "agent_id": request["agent_id"],
            "snapshot_sha256": request["snapshot_sha256"], "account_id": account_id,
            "memory_set_id": memory_set_id, "revision_hash": revision, "provider": request["strategy"],
            "physical_scope": scope, "verified": bool(verified), "complete": bool(complete),
            "item_count": item_count,
            "credential_revision": request.get("account_revisions", {}).get(account_id),
            "status": "ready" if verified else "unverified", "verified_at": now() if verified else None,
            "ranking_identical": False, "round_index": request["round_index"], **extra})

    @staticmethod
    def _has_configuration(provider, package):
        if provider == "mem0_platform_v3":
            return package.get("project_config") is not None
        return package.get("bank_config") is not None and package.get("bank_template") is not None

    @staticmethod
    def _resume_receipt(entry, package, scope, selected_ids):
        from .memory_transfer import memory_content_hash
        receipt = entry.get("import_result")
        rows = [row for row in package.get("items", [])
                if selected_ids is None or row.get("id") in selected_ids]
        content_hash = memory_content_hash(package.get("provider"), rows)
        if (isinstance(receipt, dict) and receipt.get("provider") == package.get("provider")
                and receipt.get("target_scope") == scope
                and receipt.get("source_content_hash") == content_hash
                and receipt.get("import_state") in {"partial", "unknown", "complete"}):
            return deepcopy(receipt)
        return None

    def _identify_account(self, account, client, request, job_id):
        identity = client.identify()
        # Hindsight can authenticate a key without exposing its organization;
        # a configured organization is not a provider-verified identity.
        identity = {**identity, "verified": bool(identity.get("identity_verified", identity.get("verified", False)))}
        checked = self.store.record_identity(account["account_id"], identity,
            expected_revision=account.get("credential_revision", 0))
        account.update(checked)
        request["account_revisions"][account["account_id"]] = checked.get("credential_revision", 0)
        self.store.update_job(job_id, request=request)
        return account

    def _connect_existing(self, job_id, source, request, cancel_event, keys):
        account = self.store.resolve_account(request["account_id"], request["strategy"])
        if is_default_account(account) or not account.get("enabled"):
            raise ValueError("existing-scope connections require an enabled non-default account")
        if account.get("credential_revision", 0) != request["account_revisions"].get(account["account_id"]):
            raise ValueError("account context changed; start a new existing-scope connection")
        keys.append(account["api_key"])
        package = self.legacy_export(source)
        if not package or package.get("complete") is not True:
            raise ValueError("the frozen full MemOS source export is unavailable")
        client = self.client(account, cancel_event)
        self.store.update_job(job_id, status="verifying", phase="verifying_existing_scope", started_at=now())
        def invalidate_binding():
            for replica in self.store.list_replicas(source["run_id"], source["agent_id"], source["snapshot_sha256"]):
                if replica.get("account_id") == account["account_id"] and replica.get("physical_scope") == request["physical_scope"]:
                    self.store.save_replica({**replica, "verified": False, "status": "verification_failed",
                        "pool_exclusion_reason": "Existing MemOS scope could not be verified against the frozen source"})
        try:
            check = client.verify(package, request["physical_scope"])
        except Exception:
            invalidate_binding()
            raise
        if cancel_event.is_set():
            raise InterruptedError("existing-scope verification cancelled")
        verified = check.get("verified") is True and check.get("complete") is True and check.get("config_match") is True
        entry = {"account_id": account["account_id"], "scope": request["physical_scope"],
                 "verification": _receipt(check, *keys), "verified": verified, "copied": False,
                 "binding_mode": "shared_scope" if request["physical_scope"] == source["scope"] else "verified_existing_scope",
                 "status": "completed" if verified else "failed"}
        if verified:
            replica = self._binding(request, package, account["account_id"], request["physical_scope"],
                complete=True, verified=True, verification=_receipt(check, *keys),
                binding_mode=entry["binding_mode"], copied=False,
                verification_basis="all_six_native_memory_categories", ranking_identical=False)
            entry["replica_id"] = replica["replica_id"]
            self.store.set_account_state(account["account_id"], "active", expected_revision=account.get("credential_revision", 0))
        else:
            invalidate_binding()
            entry["error"] = check.get("reason") or "Existing scope does not match the frozen complete MemOS source"
        self.store.update_job(job_id, status=entry["status"], phase="completed", targets=[entry],
                              completed_count=int(verified), total_count=1, completed_at=now())

    def execute(self, job_id, *, cancel_event=None):
        cancel_event = cancel_event or threading.Event()
        if not self.store.claim_job(job_id, self._owner):
            return
        heartbeat_done = threading.Event()
        def heartbeat():
            while not heartbeat_done.wait(30):
                self.store.claim_job(job_id, self._owner)
                if self.store.get_job(job_id).get("cancel_requested"):
                    cancel_event.set()
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        keys = []
        account = None
        try:
            job = self.store.get_job(job_id)
            request = job["request"]
            source = self.source(request["run_id"], request["agent_id"], request["round_index"])
            if source["snapshot_sha256"] != request["snapshot_sha256"]:
                raise ValueError("source snapshot changed after the transfer was queued")
            source_status, source_complete = self._transfer_source_state(source, job["kind"])
            if job["kind"] == "pull":
                request["source_run_status"] = source_status
                request["source_ingestion_complete"] = bool(
                    request.get("source_ingestion_complete", True) and source_complete)
            if job["kind"] == "connect-existing":
                self._connect_existing(job_id, source, request, cancel_event, keys)
                return
            account = self.store.resolve_account(request["source_account_id"], request["strategy"])
            if account.get("credential_revision", 0) != request.get("account_revisions", {}).get(account["account_id"]):
                raise ValueError("source account context changed; start a new synchronization")
            keys.append(account["api_key"])
            client = self.client(account, cancel_event)
            account = self._identify_account(account, client, request, job_id)
            self.store.update_job(job_id, status="running", phase="exporting", started_at=job.get("started_at", now()))
            package_file = job.get("package_file") or f"packages/{job_id}.json"
            package = _read(self._artifact(package_file))
            if package is None:
                if job["kind"] == "sync":
                    package = client.export(request["scope"], document_ids=request.get("document_ids"))
                else:
                    package = client.list_memories(request["scope"])
                package.setdefault("kind", "remote_memory")
                package.setdefault("exported_at", now())
                if job["kind"] == "pull":
                    package.update(source_run_status=request["source_run_status"],
                                   source_ingestion_complete=request["source_ingestion_complete"])
                _write(self._artifact(package_file), package)
                self.store.update_job(job_id, package_file=package_file)
            if cancel_event.is_set():
                raise InterruptedError("memory transfer cancelled")
            complete = bool(package.get("complete", False))
            if not complete:
                raise ValueError("cloud memory changed during export or the provider returned an incomplete list")
            # A document subset must not replace the original source's full cache.
            if not request.get("document_ids"):
                self.store.save_cache(request["cache_key"], {
                    "package_file": package_file, "account_id": account["account_id"], "fetched_at": now(),
                    "credential_revision": account.get("credential_revision", 0),
                    "pull_status": "complete", "item_count": len(package.get("items", []))})
            if (request.get("source_ingestion_complete", True)
                    and not request.get("selected_ids") and not request.get("document_ids")):
                has_configuration = self._has_configuration(request["strategy"], package)
                self._binding(request, package, account["account_id"], request["scope"],
                    complete=has_configuration, verified=True, origin=True,
                    pool_exclusion_reason=None if has_configuration else
                        "Full provider configuration was not included in this export")
            if job["kind"] == "pull":
                self.store.update_job(job_id, status="completed", phase="completed", completed_at=now(),
                    item_count=len(package.get("items", [])))
                return
            targets = list(job.get("targets") or [])
            for target_id in request["target_account_ids"]:
                if cancel_event.is_set():
                    raise InterruptedError("memory transfer cancelled")
                prior = next((item for item in targets if item["account_id"] == target_id), None)
                if prior and prior.get("status") == "completed":
                    continue
                target = self.store.resolve_account(target_id, request["strategy"])
                if is_default_account(target):
                    raise ValueError("default accounts are export-only and cannot be sync targets")
                if target.get("credential_revision", 0) != request.get("account_revisions", {}).get(target_id):
                    raise ValueError("target account context changed; start a new synchronization")
                keys.append(target["api_key"])
                scope = prior.get("scope") if prior else "replica_" + hashlib.sha256((job_id + target_id).encode()).hexdigest()[:40]
                entry = prior or {"account_id": target_id, "scope": scope, "status": "prepared"}
                if not prior:
                    targets.append(entry)
                self.store.update_job(job_id, targets=targets)
                target_client = self.client(target, cancel_event)
                try:
                    target = self._identify_account(target, target_client, request, job_id)
                    resume_state = self._resume_receipt(entry, package, scope, request.get("selected_ids"))
                    needs_import = True
                    prior_receipt = entry.get("import_result")
                    has_ambiguous_receipt = (isinstance(prior_receipt, dict)
                                             and prior_receipt.get("import_state") in {"unknown", "complete"})
                    # Lost acknowledgements are reconciled before any resubmission.
                    if (entry.get("status") in {"importing", "processing", "verifying", "unknown"}
                            or has_ambiguous_receipt):
                        if (request["strategy"] == "hindsight_cloud" and resume_state
                                and resume_state.get("operation_id") and resume_state.get("operation_scope")):
                            # Poll the existing carrier operation before reading
                            # a target that may not have been created yet. This
                            # resume branch only reads; it cannot upload again.
                            resumed = target_client.import_package(package, scope,
                                selected_ids=request.get("selected_ids"), resume_state=resume_state)
                            entry["import_result"] = _receipt(resumed, *keys)
                            if resumed.get("import_state") != "complete":
                                entry.update(status="unknown", error=public_error(
                                    resumed.get("error", "Previous import remains unconfirmed"), *keys))
                                self.store.update_job(job_id, targets=targets)
                                continue
                        check = target_client.verify(package, scope, selected_ids=request.get("selected_ids"))
                        entry["verification"] = _receipt(check, *keys)
                        if check.get("verified"):
                            needs_import = False
                        elif request["strategy"] != "mem0_platform_v3" or resume_state is None:
                            entry.update(status="unknown", error="Previous import remains unconfirmed; inspect the target before resubmitting")
                            self.store.update_job(job_id, targets=targets)
                            continue
                        elif resume_state.get("import_state") == "complete":
                            # A succeeded ADD followed by a lagging read is
                            # still a committed write, never a reason to ADD
                            # the supposedly missing rows a second time.
                            resume_state["import_state"] = "unknown"
                    if needs_import:
                        from .memory_transfer import memory_content_hash
                        selected = [row for row in package.get("items", [])
                                    if request.get("selected_ids") is None or row.get("id") in request["selected_ids"]]
                        intent = {"provider": request["strategy"], "target_scope": scope,
                                  "source_content_hash": memory_content_hash(request["strategy"], selected),
                                  "import_state": "unknown", "retry_safe": False, "items": [], "id_mapping": {}}
                        entry.update(status="importing", error=None)
                        entry["import_result"] = _receipt(resume_state or intent, *keys)
                        self.store.update_job(job_id, targets=targets)
                        def progress(value):
                            entry["progress"] = public_error(value, *keys)
                            state = deepcopy(entry.get("import_result") or intent)
                            state.update(import_state="unknown", retry_safe=False)
                            if value.get("phase") == "import_submitting":
                                state["operation_id"] = None
                                entry.pop("operation_id", None)
                                if value.get("source_id"):
                                    state["pending_source_id"] = value["source_id"]
                            if value.get("operation_id") is not None:
                                state["operation_id"] = value["operation_id"]
                                entry["operation_id"] = value["operation_id"]
                            for key in ("carrier_scope", "operation_scope", "carrier_creation_started",
                                        "import_submission_started"):
                                if value.get(key) is not None:
                                    state[key] = value[key]
                            entry["import_result"] = _receipt(state, *keys)
                            self.store.update_job(job_id, targets=targets)
                        result = target_client.import_package(package, scope,
                            selected_ids=request.get("selected_ids"), progress=progress, resume_state=resume_state)
                        entry["import_result"] = _receipt(result, *keys)
                        entry["id_mapping"] = _receipt(result.get("id_mapping", {}), *keys)
                        if result.get("import_state") != "complete":
                            entry.update(status="unknown", error=public_error(result.get("error", "Import is unconfirmed"), *keys))
                            self.store.update_job(job_id, targets=targets)
                            continue
                        entry["status"] = "verifying"
                        self.store.update_job(job_id, targets=targets)
                        check = target_client.verify(package, scope, selected_ids=request.get("selected_ids"))
                    verified = bool(check.get("verified"))
                    eligible = not bool(request.get("selected_ids") or request.get("document_ids") or check.get("deduplicated"))
                    reason = None
                    if request.get("selected_ids") or request.get("document_ids"):
                        reason = "Only a selected subset was copied"
                    elif check.get("deduplicated"):
                        reason = "Provider deduplication changed the number of memory records"
                    elif check.get("config_match") is not True:
                        reason = "Provider configuration could not be verified; this copy is unavailable for automatic account switching"
                    eligible = eligible and check.get("config_match") is True
                    replica = self._binding(request, package, target_id, scope,
                        complete=eligible, verified=verified, verification=_receipt(check, *keys),
                        pool_exclusion_reason=reason,
                        id_mapping=_receipt(check.get("id_mapping") or entry.get("id_mapping", {}), *keys))
                    entry.update(status="completed" if verified else "failed", verified=verified,
                                 verification=_receipt(check, *keys), pool_exclusion_reason=reason,
                                 id_mapping=_receipt(check.get("id_mapping") or entry.get("id_mapping", {}), *keys),
                                 replica_id=replica["replica_id"], error=None if verified else check.get("reason", "Verification failed"))
                except Exception as exc:
                    category = getattr(exc, "category", "transfer_failed")
                    if isinstance(getattr(exc, "partial_result", None), dict):
                        entry["import_result"] = _receipt(exc.partial_result, *keys)
                        entry["id_mapping"] = _receipt(exc.partial_result.get("id_mapping", {}), *keys)
                    # A known rejection may safely be retried; ambiguous writes may not.
                    entry.update(status="unknown" if entry["status"] in {"importing", "processing", "verifying"}
                                 and category not in {"quota_exhausted", "invalid_credentials", "authentication", "forbidden", "validation", "not_found", "rate_limited"}
                                 else "failed", error=public_error(str(exc), *keys), category=category)
                    if category == "quota_exhausted":
                        self.store.set_account_state(target_id, "quota_exhausted", last_error={"category": category},
                            expected_revision=target.get("credential_revision", 0))
                self.store.update_job(job_id, targets=targets, completed_count=sum(t.get("status") == "completed" for t in targets),
                                      total_count=len(request["target_account_ids"]))
            successful = sum(t.get("status") == "completed" for t in targets)
            self.store.update_job(job_id, status="completed" if successful == len(targets) else "partial" if successful else "failed",
                                  phase="completed", completed_at=now())
        except Exception as exc:
            if account is not None and getattr(exc, "category", None) == "quota_exhausted":
                self.store.set_account_state(account["account_id"], "quota_exhausted",
                    last_error={"category": "quota_exhausted"}, expected_revision=account.get("credential_revision", 0))
            cancelled = cancel_event.is_set() or isinstance(exc, InterruptedError)
            self.store.update_job(job_id, status="cancelled" if cancelled else "failed", error=public_error(str(exc), *keys), completed_at=now())
            job = self.store.get_job(job_id)
            if job["kind"] == "pull":
                previous = self.store.get_cache(job["request"]["cache_key"]) or {}
                if not previous.get("package_file"):
                    self.store.save_cache(job["request"]["cache_key"], {"pull_status": "failed", "error": public_error(str(exc), *keys)})
        finally:
            heartbeat_done.set()
            self.store.release_job(job_id, self._owner)
