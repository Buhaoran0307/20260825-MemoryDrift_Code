"""Runtime-only credentials/checkpoint paths; never serialize credentials."""
import hashlib
import os
from pathlib import Path

from agent_posting_core.cloud_memory import CLOUD_STRATEGIES, CloudMemoryError
from .config import PROJECT_ROOT, read_env_file


def memory_scope_run_id(source):
    """An imported run has a new registry ID but retains its provider namespace."""
    if source.get("memory_strategy") in {*CLOUD_STRATEGIES, "mem0_platform_v3", "langmem_official"}:
        scope = source.get("snapshot", {}).get("options", {}).get("scope", {})
        if isinstance(scope.get("run_id"), str) and scope["run_id"]:
            return scope["run_id"]
    return source["run_id"]


def cloud_runtime(strategy, run_id, *, api_keys=None, run_dir=None, agent_id=None,
                  read_only=False, project_root=PROJECT_ROOT, registry_db_path=None, store=None):
    if strategy not in CLOUD_STRATEGIES:
        raise ValueError("unsupported cloud strategy")
    name = strategy.removesuffix("_cloud").upper() + "_API_KEY"
    account = None
    if strategy == "memos_cloud" and not read_only and (store is not None or registry_db_path):
        from agent_posting_core.memory_read_router import is_default_account
        if store is None:
            from .memory_account_store import MemoryAccountStore
            store = MemoryAccountStore(Path(registry_db_path))
        store.ensure_environment_account(strategy, env_name=name, project_root=project_root)
        account = store.resolve_account("env-" + strategy, expected_provider=strategy)
        if not is_default_account(account) or not account.get("enabled", True):
            raise CloudMemoryError("MemOS first-stage ingestion requires the enabled default export account")
        key = account.get("api_key")
    else:
        key = (api_keys or {}).get(strategy) if api_keys is not None else (
            os.environ.get(name) or read_env_file(Path(project_root) / ".env").get(name)
        )
    if not key:
        raise CloudMemoryError(f"API key is empty; set {name}")
    result = {"api_key": key, "run_id": run_id, "read_only": read_only}
    if account is not None:
        result.update(account_id=account["account_id"], api_base=account["api_base"])
        if account.get("credential_revision") is not None:
            result["credential_revision"] = account["credential_revision"]
    if run_dir is not None and agent_id is not None and not read_only:
        token = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()
        result["checkpoint_path"] = str(Path(run_dir) / "memory" / "cloud-checkpoints" / f"{strategy}-{token}.json")
    return result


def build_memory_read_router(source, pool, *, registry_db_path=None, store=None, on_attempt=None):
    """Resolve a server-provided registry without serializing any credential."""
    from agent_posting_core.memory_read_router import MemoryReadRouter, PROVIDERS
    if source.get("memory_strategy") not in PROVIDERS:
        if isinstance(pool, dict) and pool.get("enabled"):
            raise ValueError("account pools support Hindsight, Mem0 and MemOS only")
        return None
    if pool is not None and not isinstance(pool, dict):
        raise ValueError("memory account pool must be an object")
    pool = dict(pool or {})
    if "shared" in pool and type(pool["shared"]) is not bool:
        raise ValueError("memory_account_pool.shared must be boolean")
    if any(str(account_id).startswith("env-") for account_id in pool.get("account_ids", [])):
        raise ValueError("The default account is export-only. Select an experiment account for stage two.")
    if store is None:
        from .memory_account_store import MemoryAccountStore
        database = registry_db_path or pool.get("registry_db_path")
        if not database:
            raise ValueError("Stage-two cloud memory requires platform experiment accounts. Import memory from the default account into experiment accounts first.")
        store = MemoryAccountStore(Path(database))
    if not pool.get("enabled"):
        pool = {**pool, "enabled": True,
                "account_ids": pool.get("account_ids", []) if pool.get("shared") is False else []}
    pool.setdefault("shared", True)
    return MemoryReadRouter(store=store, source=source, pool=pool, on_attempt=on_attempt)
