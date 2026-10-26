"""Strict same-run maintenance for an interrupted cloud retrieval quota failure."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path

from agent_posting_core.memory_read_router import classify_cloud_failure
from .experiment_config import load_experiment_config
from .validation_runner import _resume_identity, _summary


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recover_interrupted_memory_quota_run(platform, run_id: str, *, expected_manifest_sha256: str):
    """Preserve interruption evidence and require unchanged frozen inputs and zero LLM calls.

    This does not retry any query. The existing resume endpoint skips every
    finalized selection, including the original failed retrieval selections.
    """
    from .platform_service import _append_json_line, _now, _read_json_if_exists, _write_json

    with platform._launch_lock:
        run = platform.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.get("runner_type") != "tool_selection" or run.get("status") != "interrupted":
            raise ValueError("quota maintenance requires an interrupted second-stage run")
        if run.get("screen_session") and platform.launcher.is_active(run["screen_session"]):
            raise ValueError("quota maintenance cannot change an active worker")
        directory = Path(run["output_dir"])
        manifest_path = directory / "manifest.json"
        if _hash(manifest_path) != expected_manifest_sha256:
            raise ValueError("quota maintenance manifest changed before recovery")
        manifest = _read_json_if_exists(manifest_path) or {}
        if (manifest.get("status") != "interrupted" or manifest.get("run_id") != run_id
                or manifest.get("interrupted_by") != "user"):
            raise ValueError("quota maintenance requires the original user interruption")
        config = load_experiment_config(Path(run["worker_config_path"]), project_root=platform.project_root)
        if config.run.run_id != run_id or config.validation is None:
            raise ValueError("quota maintenance run identity changed")
        resolved = _read_json_if_exists(directory / "config.resolved.json") or {}
        if _resume_identity(resolved) != _resume_identity(config.public_dict()):
            raise ValueError("quota maintenance frozen configuration changed")
        validation = config.validation
        pool = validation.get("memory_account_pool") or {}
        if (not pool.get("enabled") or not pool.get("memory_set_id") or not pool.get("revision_hash")
                or not pool.get("account_ids")):
            raise ValueError("quota maintenance requires an enabled frozen memory pool")
        source = validation["memory_source"]
        if (manifest.get("snapshot_sha256") != source["snapshot_sha256"]
                or manifest.get("source_run_id") != source["run_id"]):
            raise ValueError("quota maintenance frozen source identity changed")
        frozen = {"config.resolved.json": resolved,
            "source-memory.json": source,
            "input-queries.json": validation["queries"]["dataset"],
            "input-mcp.json": validation["mcp"]["dataset"]}
        for filename, expected in frozen.items():
            if _read_json_if_exists(directory / filename) != expected:
                raise ValueError("quota maintenance frozen input changed: " + filename)
        queries = validation["queries"]["dataset"]["queries"]
        records = []
        selection_paths = sorted((directory / "selections").glob("query-*.json"))
        for path in selection_paths:
            record = _read_json_if_exists(path) or {}
            index = record.get("query_index")
            if (not isinstance(index, int) or not 1 <= index <= len(queries)
                    or path.name != f"query-{index:04d}.json"
                    or record.get("query_id") != queries[index - 1]["query_id"]
                    or record.get("snapshot_sha256") != source["snapshot_sha256"]):
                raise ValueError("quota maintenance saved query identity changed")
            failure = record.get("cloud_failure") or {}
            error = record.get("error") or ""
            match = re.search(r"HTTP\s+(429|402|403|400):\s*(\{.*\})$", error)
            try:
                body = json.loads(match.group(2)) if match else None
            except (TypeError, ValueError):
                body = None
            classified = classify_cloud_failure(int(match.group(1)) if match else None, body,
                provider=failure.get("provider"))
            if (record.get("status") != "failed" or record.get("behavior_llm_calls") != 0
                    or record.get("raw_response") or record.get("tool_calls")
                    or failure.get("http_status") not in {400, 402, 403, 429}
                    or classified["category"] != "quota_exhausted"):
                raise ValueError("quota maintenance requires finalized retrieval-only billing-quota evidence")
            records.append(record)
        if not records or [r["query_index"] for r in records] != list(range(1, len(records) + 1)):
            raise ValueError("quota maintenance requires a contiguous saved query checkpoint")
        selections = _read_json_if_exists(directory / "selections.json") or {}
        if (manifest.get("completed_queries") != len(records) or selections.get("total") != len(records)
                or selections.get("items") != [_summary(r) for r in records]):
            raise ValueError("quota maintenance saved summaries changed")
        # Recheck the caller's checkpoint after all validation and before mutation.
        if _hash(manifest_path) != expected_manifest_sha256:
            raise ValueError("quota maintenance manifest changed during recovery")
        audit = directory / "maintenance" / ("billing-quota-routing-" + secrets.token_hex(8))
        audit.mkdir(parents=True, exist_ok=False)
        hashes = {"manifest.json": expected_manifest_sha256}
        for name in ("events.jsonl", "config.resolved.json", "source-memory.json", "input-queries.json",
                     "input-mcp.json", "selections.json"):
            path = directory / name
            if path.exists():
                hashes[name] = _hash(path)
        for path in selection_paths:
            hashes[str(path.relative_to(directory))] = _hash(path)
        # Exclusive files ensure original interruption/control records cannot be overwritten.
        for name in ("manifest.json", "events.jsonl"):
            path = directory / name
            if path.exists():
                with (audit / ("original-" + name)).open("xb") as destination:
                    destination.write(path.read_bytes())
        action = {"at": _now(), "reason": "billing_quota_routing_maintenance",
            "original_status": "interrupted", "run_id": run_id,
            "original_manifest_sha256": expected_manifest_sha256, "original_file_hashes": hashes,
            "audit_dir": str(audit), "preserved_finalized_queries": len(records),
            "next_query_index": len(records) + 1, "resume_does_not_repeat_finalized_queries": True}
        with (audit / "audit.json").open("x", encoding="utf-8") as destination:
            json.dump(action, destination, ensure_ascii=False, indent=2)
        manifest.setdefault("maintenance_actions", []).append(action)
        manifest.update(status="paused", pause_reason="memory_accounts_unavailable", paused_at=_now(),
            maintenance_pause_reason="billing_quota_routing_maintenance", can_resume_memory_accounts=True)
        _write_json(manifest_path, manifest)
        _append_json_line(directory / "events.jsonl", {"at": _now(), "event": "memory_quota_maintenance_paused", **action})
        platform.store.update_run(run_id, status="paused", completed_at=None,
            error="Billing-quota routing maintenance; original failed queries and interruption audit preserved")
    return platform.get_run(run_id)
