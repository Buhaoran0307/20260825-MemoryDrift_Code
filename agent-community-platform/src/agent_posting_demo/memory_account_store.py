"""Hosted-memory accounts, replica bindings and durable transfer jobs.

Only public account metadata is stored as JSON. Credentials are authenticated
encrypted, with a local key outside experiment artifacts and bundle exports.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from agent_posting_core.memory_read_router import is_default_account


PROVIDERS = {"mem0_platform_v3", "hindsight_cloud", "memos_cloud"}
DEFAULT_BASES = {"mem0_platform_v3": "https://api.mem0.ai",
                 "hindsight_cloud": "https://api.hindsight.vectorize.io",
                 "memos_cloud": "https://memos.memtensor.cn/api/openmem/v1"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def provider_name(value: str) -> str:
    value = {"mem0": "mem0_platform_v3", "hindsight": "hindsight_cloud",
             "memos": "memos_cloud"}.get(value, value)
    if value not in PROVIDERS:
        raise ValueError("only Mem0, Hindsight and MemOS accounts are supported")
    return value


def public_error(value, *secrets):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in {
            "api_key", "authorization", "token", "secret", "credential", "native_archive"
        } else public_error(item, *secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [public_error(item, *secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value[:4000]
    return value


def public_account(value):
    """Derive roles for legacy rows as well as newly created accounts."""
    value = dict(value)
    default = is_default_account(value)
    value.update(is_default=default, export_only=default)
    value.setdefault("credential_source", "environment" if default else "stored")
    return value


class MemoryAccountStore:
    def __init__(self, db_path: Path, workspace_dir: Path | None = None):
        self.db_path = Path(db_path).resolve()
        self.workspace_dir = Path(workspace_dir or self.db_path.parent / "memory_workspace").resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS memory_accounts (
                    account_id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    metadata_json TEXT NOT NULL, credential_cipher TEXT,
                    credential_ref_json TEXT
                );
                CREATE TABLE IF NOT EXISTS memory_replicas (
                    replica_id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
                    account_id TEXT NOT NULL, memory_set_id TEXT NOT NULL,
                    revision_hash TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    UNIQUE(source_run_id,agent_id,snapshot_sha256,account_id,memory_set_id,revision_hash)
                );
                CREATE INDEX IF NOT EXISTS memory_replicas_source
                    ON memory_replicas(source_run_id,agent_id,snapshot_sha256);
                CREATE TABLE IF NOT EXISTS memory_jobs (
                    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_run_id TEXT,
                    status TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    lease_owner TEXT, lease_until REAL
                );
                CREATE TABLE IF NOT EXISTS memory_pull_cache (
                    cache_key TEXT PRIMARY KEY, metadata_json TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.db_path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _cipher(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise RuntimeError("Install requirements.txt in the platform Python environment") from exc
        path = self.workspace_dir / ".credential-key"
        if path.is_symlink():
            raise PermissionError("credential key must not be a symbolic link")
        if not path.exists():
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    # Publish only the complete key; concurrent creators never
                    # observe an empty file or replace another creator's key.
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        if path.is_symlink():
            raise PermissionError("credential key must not be a symbolic link")
        return Fernet(path.read_bytes())

    @staticmethod
    def _invalidate_replicas(connection, account_id):
        rows = connection.execute("SELECT replica_id,metadata_json FROM memory_replicas WHERE account_id=?", (account_id,)).fetchall()
        for row in rows:
            value = json.loads(row[1])
            value.update(verified=False, status="credential_changed", updated_at=now())
            connection.execute("UPDATE memory_replicas SET metadata_json=? WHERE replica_id=?", (json.dumps(value), row[0]))

    @staticmethod
    def _public_row(row):
        value = public_account(json.loads(row["metadata_json"]))
        available = bool(row["credential_cipher"])
        if row["credential_ref_json"]:
            from .config import read_env_file
            reference = json.loads(row["credential_ref_json"])
            available = bool(os.environ.get(reference["env_name"])
                             or read_env_file(Path(reference["env_file"])).get(reference["env_name"]))
            value["credential_source"] = "environment"
        elif row["credential_cipher"]:
            value["credential_source"] = "stored"
        value["credential_available"] = available
        value.pop("has_api_key", None)
        return value

    def list_accounts(self, provider=None):
        provider = provider_name(provider) if provider else None
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM memory_accounts" +
                                      (" WHERE provider=?" if provider else ""),
                                      (provider,) if provider else ()).fetchall()
        return sorted((self._public_row(row) for row in rows),
                      key=lambda item: (not item["is_default"], item.get("priority", 100), item["name"], item["account_id"]))

    def get_account(self, account_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM memory_accounts WHERE account_id=?",
                                     (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        return self._public_row(row)

    def display_keys(self):
        """Read credentials for the authorized local account page only.

        Keep these values separate from public metadata and do not change
        account revisions or state merely because the page is viewed.
        """
        from .config import read_env_file
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM memory_accounts").fetchall()
        accounts, failures = [], []
        for row in rows:
            key = None
            try:
                if row["credential_ref_json"]:
                    ref = json.loads(row["credential_ref_json"])
                    key = (os.environ.get(ref["env_name"])
                           or read_env_file(Path(ref["env_file"])).get(ref["env_name"]) or None)
                elif row["credential_cipher"]:
                    key = self._cipher().decrypt(row["credential_cipher"].encode()).decode()
            except Exception:
                # Do not reflect credential contents or decryption errors.
                failures.append({"account_id": row["account_id"], "error": "credential_unavailable"})
            accounts.append({"account_id": row["account_id"], "api_key": key})
        return {"accounts": accounts, "failures": failures}

    @staticmethod
    def _next_account_name(connection, provider):
        sequence = 0
        for row in connection.execute("SELECT metadata_json FROM memory_accounts WHERE provider=?", (provider,)):
            account = json.loads(row[0])
            if is_default_account(account):
                continue
            match = re.fullmatch(r"(?:Experiment |\u5b9e\u9a8c)([0-9]+)", account.get("name", ""))
            if match:
                sequence = max(sequence, int(match.group(1)))
        return f"Experiment {sequence + 1:02d}"

    def next_account_name(self, provider):
        """Return a suggestion; creation reserves the next name in its transaction."""
        with self.connection() as connection:
            return self._next_account_name(connection, provider_name(provider))

    def save_account(self, payload, account_id=None):
        if not isinstance(payload, dict):
            raise ValueError("account must be an object")
        allowed = {"provider", "name", "api_key", "api_base", "org_id", "project_id",
                   "quota_domain", "priority", "enabled", "status"}
        if set(payload) - allowed:
            raise ValueError("unknown account fields: " + ", ".join(sorted(set(payload) - allowed)))
        previous = self.get_account(account_id) if account_id else {}
        account_id = account_id or "ma_" + uuid.uuid4().hex
        provider = provider_name(payload.get("provider", previous.get("provider")))
        if previous and provider != previous["provider"]:
            raise ValueError("an account's provider cannot be changed")
        value = {**previous, "account_id": account_id, "provider": provider,
                 "created_at": previous.get("created_at", now()), "updated_at": now()}
        for key in ("name", "org_id", "project_id", "quota_domain"):
            candidate = payload.get(key, previous.get(key, ""))
            if not isinstance(candidate, str) or len(candidate) > 300 or any(ord(c) < 32 for c in candidate):
                raise ValueError(f"invalid account {key}")
            value[key] = candidate.strip()
        automatic_name = not previous and not value["name"]
        if not value["name"] and not automatic_name:
            raise ValueError("account name is required")
        value = public_account(value)
        api_base = payload.get("api_base", previous.get("api_base", DEFAULT_BASES[provider]))
        if not isinstance(api_base, str):
            raise ValueError("api_base must be a string")
        api_base = api_base.strip().rstrip("/") or DEFAULT_BASES[provider]
        parsed = urlsplit(api_base)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("api_base must be HTTPS, or HTTP on localhost")
        value["api_base"] = api_base
        priority = payload.get("priority", previous.get("priority", 100))
        if type(priority) is not int or not 0 <= priority <= 10000:
            raise ValueError("priority must be an integer between 0 and 10000")
        value["priority"] = priority
        enabled = payload.get("enabled", previous.get("enabled", True))
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        value["enabled"] = enabled
        status = payload.get("status", previous.get("status", "unchecked"))
        if status not in {"unchecked", "active", "quota_exhausted", "invalid_credentials",
                          "forbidden", "disabled", "cooldown"}:
            raise ValueError("invalid account status")
        if "api_key" in payload and payload["api_key"]:
            status = "unchecked"
        if enabled and status == "disabled":
            status = "unchecked"
        value["status"] = status if enabled else "disabled"
        if payload.get("status") == "active":
            value.update(cooldown_until=None, last_error=None)
        # Keep derived billing domains aligned when an organization is edited.
        derived_domains = {provider + ":account:" + account_id, provider + ":environment"}
        if previous.get("org_id"):
            derived_domains.add(provider + ":org:" + previous["org_id"])
        if (not value["quota_domain"] or "quota_domain" not in payload
                and previous.get("quota_domain") in derived_domains):
            value["quota_domain"] = (provider + ":org:" + value["org_id"]
                                     if value["org_id"] else provider + ":account:" + account_id)
        context_changed = bool(previous) and any(value.get(field) != previous.get(field) for field in
            ("api_base", "org_id", "project_id", "quota_domain"))
        credential_changed = bool(payload.get("api_key")) or context_changed
        value["credential_revision"] = previous.get("credential_revision", 0) + (1 if credential_changed or not previous else 0)
        cipher = None
        if "api_key" in payload and payload["api_key"]:
            key = payload["api_key"]
            if not isinstance(key, str) or not 1 <= len(key) <= 4096 or any(c.isspace() for c in key):
                raise ValueError("invalid API key")
            cipher = self._cipher().encrypt(key.encode()).decode()
            value["masked_key"] = "••••" + (key[-4:] if len(key) > 8 else "")
            value["credential_source"] = "stored"
        elif not previous:
            raise ValueError("API key is required")
        with self.connection() as connection:
            if previous:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute("SELECT metadata_json FROM memory_accounts WHERE account_id=?", (account_id,)).fetchone()
                if json.loads(current[0]).get("credential_revision", 0) != previous.get("credential_revision", 0):
                    raise ValueError("account context changed while saving; reload the account")
                connection.execute("UPDATE memory_accounts SET metadata_json=?, "
                                   "credential_cipher=COALESCE(?,credential_cipher), "
                                   "credential_ref_json=CASE WHEN ? IS NULL THEN credential_ref_json ELSE NULL END "
                                   "WHERE account_id=?",
                                   (json.dumps(value), cipher, cipher, account_id))
                if credential_changed:
                    self._invalidate_replicas(connection, account_id)
            else:
                connection.execute("BEGIN IMMEDIATE")
                if automatic_name:
                    value["name"] = self._next_account_name(connection, provider)
                duplicate = connection.execute("SELECT metadata_json FROM memory_accounts WHERE provider=?", (provider,)).fetchall()
                if any(json.loads(row[0]).get("name") == value["name"] for row in duplicate):
                    raise ValueError("account name already exists for this memory mechanism")
                connection.execute("INSERT INTO memory_accounts VALUES (?,?,?,?,NULL)",
                                   (account_id, provider, json.dumps(value), cipher))
        return self.get_account(account_id)

    def ensure_environment_account(self, provider, *, env_name, project_root):
        provider = provider_name(provider)
        account_id = "env-" + provider
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM memory_accounts WHERE account_id=?", (account_id,)).fetchone():
                return
            value = {"account_id": account_id, "provider": provider, "name": "Default account",
                     "api_base": DEFAULT_BASES[provider], "enabled": True, "priority": 100,
                     "status": "unchecked", "org_id": "", "project_id": "",
                     "quota_domain": provider + ":environment", "masked_key": "••••",
                     "created_at": now(), "updated_at": now(), "environment_account": True,
                     "is_default": True, "export_only": True, "credential_source": "environment"}
            value["credential_revision"] = 1
            from .config import read_env_file
            key = os.environ.get(env_name) or read_env_file(Path(project_root) / ".env").get(env_name) or ""
            connection.execute("INSERT INTO memory_accounts VALUES (?,?,?,NULL,?)",
                               (account_id, provider, json.dumps(value), json.dumps({
                                   "env_name": env_name, "env_file": str(Path(project_root) / ".env"),
                                   "fingerprint": hashlib.sha256(key.encode()).hexdigest()})))

    def resolve_account(self, account_id, expected_provider=None):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM memory_accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        value = json.loads(row["metadata_json"])
        if expected_provider and provider_name(expected_provider) != value["provider"]:
            raise ValueError("account provider does not match the memory source")
        if row["credential_ref_json"]:
            from .config import read_env_file
            ref = json.loads(row["credential_ref_json"])
            key = os.environ.get(ref["env_name"]) or read_env_file(Path(ref["env_file"])).get(ref["env_name"])
            fingerprint = hashlib.sha256((key or "").encode()).hexdigest()
            if ref.get("fingerprint") != fingerprint:
                with self.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute("SELECT metadata_json,credential_ref_json FROM memory_accounts WHERE account_id=?", (account_id,)).fetchone()
                    value, ref = json.loads(current[0]), json.loads(current[1])
                    if ref.get("fingerprint") != fingerprint:
                        ref["fingerprint"] = fingerprint
                        value.update(credential_revision=value.get("credential_revision", 0) + 1,
                                     status="unchecked", updated_at=now())
                        connection.execute("UPDATE memory_accounts SET metadata_json=?,credential_ref_json=? WHERE account_id=?",
                                           (json.dumps(value), json.dumps(ref), account_id))
                        self._invalidate_replicas(connection, account_id)
        else:
            key = self._cipher().decrypt(row["credential_cipher"].encode()).decode()
        if not key:
            raise ValueError("account credential is unavailable")
        return {**public_account(value), "credential_available": True, "api_key": key}

    def set_account_state(self, account_id, status, *, last_error=None, cooldown_until=None,
                          expected_revision=None):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM memory_accounts WHERE account_id=?", (account_id,)).fetchone()
            if row is None:
                raise KeyError(account_id)
            value = self._public_row(row)
            if expected_revision is not None and value.get("credential_revision", 0) != expected_revision:
                return value
            value.update(status=status, last_error=public_error(last_error),
                         cooldown_until=cooldown_until, updated_at=now())
            connection.execute("UPDATE memory_accounts SET metadata_json=? WHERE account_id=?", (json.dumps(value), account_id))
            if status == "quota_exhausted":
                rows = connection.execute("SELECT account_id,metadata_json FROM memory_accounts WHERE provider=? AND account_id<>?", (value["provider"], account_id)).fetchall()
                for other_id, raw in rows:
                    other = json.loads(raw)
                    if other.get("quota_domain") == value.get("quota_domain"):
                        other.update(status=status, last_error=public_error(last_error), updated_at=now())
                        connection.execute("UPDATE memory_accounts SET metadata_json=? WHERE account_id=?", (json.dumps(other), other_id))
        return value

    def record_identity(self, account_id, identity, expected_revision=None):
        value = self.get_account(account_id)
        org = str(identity.get("org_id") or value.get("org_id") or "")
        project = str(identity.get("project_id") or value.get("project_id") or "")
        # Conservative organization grouping also covers projects sharing billing.
        value.update(org_id=org, project_id=project, identity_verified=bool(identity.get("verified", org)),
                     quota_domain=value["provider"] + ":org:" + org if org else value["quota_domain"],
                     status="active", last_error=None, cooldown_until=None, checked_at=now(), updated_at=now())
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM memory_accounts WHERE account_id=?", (account_id,)).fetchone()
            previous = self._public_row(row)
            expected_revision = value.get("credential_revision", 0) if expected_revision is None else expected_revision
            if previous.get("credential_revision", 0) != expected_revision:
                raise ValueError("account context changed during connection check; check the current credentials again")
            # A connection check must not overwrite concurrent priority/enabled edits.
            value = {**previous, **{key: value[key] for key in (
                "org_id", "project_id", "identity_verified", "quota_domain", "last_error",
                "cooldown_until", "checked_at", "updated_at")},
                "status": "active" if previous.get("enabled", True) else "disabled"}
            if any(previous.get(field) != value.get(field) for field in ("org_id", "project_id", "quota_domain")):
                value["credential_revision"] = previous.get("credential_revision", 0) + 1
                self._invalidate_replicas(connection, account_id)
            connection.execute("UPDATE memory_accounts SET metadata_json=? WHERE account_id=?", (json.dumps(value), account_id))
        return value

    def save_replica(self, value):
        value = dict(value)
        required = {"source_run_id", "agent_id", "snapshot_sha256", "account_id",
                    "memory_set_id", "revision_hash", "provider", "physical_scope"}
        if not required <= value.keys():
            raise ValueError("incomplete replica binding")
        self.get_account(value["account_id"])
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute("SELECT metadata_json FROM memory_accounts WHERE account_id=?", (value["account_id"],)).fetchone()
            current_revision = json.loads(account[0]).get("credential_revision", 0)
            if value.get("credential_revision", current_revision) != current_revision:
                raise ValueError("account credentials or endpoint changed during synchronization; verify using the new account context")
            value["credential_revision"] = current_revision
            row = connection.execute("SELECT replica_id FROM memory_replicas WHERE source_run_id=? "
                "AND agent_id=? AND snapshot_sha256=? AND account_id=? AND memory_set_id=? AND revision_hash=?",
                tuple(value[key] for key in ("source_run_id", "agent_id", "snapshot_sha256", "account_id", "memory_set_id", "revision_hash"))).fetchone()
            value["replica_id"] = row[0] if row else value.get("replica_id", "mr_" + uuid.uuid4().hex)
            value.update(updated_at=now())
            connection.execute("INSERT OR REPLACE INTO memory_replicas VALUES (?,?,?,?,?,?,?,?)",
                tuple(value[key] for key in ("replica_id", "source_run_id", "agent_id", "snapshot_sha256", "account_id", "memory_set_id", "revision_hash")) + (json.dumps(value),))
        return value

    def list_replicas(self, source_run_id=None, agent_id=None, snapshot_sha256=None):
        filters = {key: val for key, val in locals().copy().items()
                   if key in {"source_run_id", "agent_id", "snapshot_sha256"} and val}
        with self.connection() as connection:
            rows = connection.execute("SELECT metadata_json FROM memory_replicas" +
                (" WHERE " + " AND ".join(key + "=?" for key in filters) if filters else "") + " ORDER BY rowid DESC", tuple(filters.values())).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_replica(self, replica_id):
        with self.connection() as connection:
            row = connection.execute("SELECT metadata_json FROM memory_replicas WHERE replica_id=?", (replica_id,)).fetchone()
        if row is None:
            raise KeyError(replica_id)
        return json.loads(row[0])

    def save_cache(self, key, value):
        with self.connection() as connection:
            connection.execute("INSERT OR REPLACE INTO memory_pull_cache VALUES (?,?)", (key, json.dumps(value)))

    def get_cache(self, key):
        with self.connection() as connection:
            row = connection.execute("SELECT metadata_json FROM memory_pull_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def create_job(self, kind, request):
        value = {"job_id": "mj_" + uuid.uuid4().hex, "kind": kind, "request": request,
                 "status": "queued", "targets": [], "created_at": now(), "updated_at": now()}
        with self.connection() as connection:
            connection.execute("INSERT INTO memory_jobs VALUES (?,?,?,?,?,NULL,NULL)",
                (value["job_id"], kind, request.get("run_id"), "queued", json.dumps(value)))
        return value

    def get_job(self, job_id):
        with self.connection() as connection:
            row = connection.execute("SELECT metadata_json FROM memory_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return json.loads(row[0])

    def list_jobs(self, run_id=None):
        with self.connection() as connection:
            rows = connection.execute("SELECT metadata_json FROM memory_jobs" +
                (" WHERE source_run_id=?" if run_id else "") + " ORDER BY rowid DESC LIMIT 100",
                (run_id,) if run_id else ()).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update_job(self, job_id, **changes):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT metadata_json FROM memory_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            value = json.loads(row[0])
            value.update(changes, updated_at=now())
            connection.execute("UPDATE memory_jobs SET status=?,metadata_json=? WHERE job_id=?",
                               (value["status"], json.dumps(value), job_id))
        return value

    def claim_job(self, job_id, owner, lease_seconds=120):
        import time
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT lease_owner,lease_until,status FROM memory_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row[0] and (row[1] or 0) > time.time() and row[0] != owner:
                return False
            if row[2] in {"completed", "cancelled"}:
                return False
            connection.execute("UPDATE memory_jobs SET lease_owner=?,lease_until=? WHERE job_id=?",
                               (owner, time.time() + lease_seconds, job_id))
        return True

    def release_job(self, job_id, owner):
        with self.connection() as connection:
            connection.execute("UPDATE memory_jobs SET lease_owner=NULL,lease_until=NULL WHERE job_id=? AND lease_owner=?", (job_id, owner))
