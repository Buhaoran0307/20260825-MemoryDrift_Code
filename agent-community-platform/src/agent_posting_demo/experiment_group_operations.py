"""Atomic regrouping and complete local copies of existing experiments.

Moves keep experiment/run IDs, frozen configurations and all memory namespaces.
Copies use the portable bundle's evidence-preserving rewrites, reusing registered
resources and existing provider namespaces in this same platform.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

from .experiment_bundle import _references, _rewrite, _validate_owned_path, export_bundle, import_bundle
from .platform_store import LEGACY_EXPERIMENT_GROUP_ID, TERMINAL_RUN_STATUSES, _now


ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _group_id(value, field):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError(field + " must be a valid experiment group ID")
    if value == LEGACY_EXPERIMENT_GROUP_ID:
        raise PermissionError("the legacy experiment group cannot participate in group operations")
    return value


def _experiment_ids(value):
    if not isinstance(value, list) or not value:
        raise ValueError("experiment_ids must be a non-empty array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError("experiment_ids must contain non-empty strings")
    return list(dict.fromkeys(value))


def _registry(connection):
    return {
        table: {row[key]: dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')}
        for table, key in (
            ("experiment_groups", "group_id"), ("experiments", "experiment_id"),
            ("experiment_runs", "run_id"),
        )
    }


def _dependency_graph(experiments, runs):
    dependencies, missing_runs = {}, {}
    for experiment_id, row in experiments.items():
        config = json.loads(row["config_json"])
        sources = _references(config, "source_experiment_id") | _references(config, "memory_source_experiment_id")
        sources.update(row[field] for field in ("source_experiment_id", "memory_source_experiment_id") if row.get(field))
        run_ids = _references(config, "source_run_id")
        frozen = (config.get("validation") or {}).get("memory_source") or {}
        if isinstance(frozen, dict):
            if frozen.get("experiment_id"):
                sources.add(frozen["experiment_id"])
            if frozen.get("run_id"):
                run_ids.add(frozen["run_id"])
        for run_id in run_ids:
            if run_id in runs:
                sources.add(runs[run_id]["experiment_id"])
            else:
                missing_runs.setdefault(experiment_id, set()).add(run_id)
        dependencies[experiment_id] = sources
    return dependencies, missing_runs


def _memory_records(connection, run_ids):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    records = {"memory_jobs": [], "memory_replicas": [], "memory_pull_cache": []}
    if "memory_jobs" in tables:
        for row in connection.execute("SELECT * FROM memory_jobs"):
            metadata = json.loads(row["metadata_json"])
            if row["source_run_id"] in run_ids or (metadata.get("request") or {}).get("run_id") in run_ids:
                records["memory_jobs"].append(dict(row))
    if "memory_replicas" in tables:
        records["memory_replicas"] = [dict(row) for row in connection.execute("SELECT * FROM memory_replicas")
                                      if row["source_run_id"] in run_ids]
    cache_keys = {(json.loads(row["metadata_json"]).get("request") or {}).get("cache_key")
                  for row in records["memory_jobs"]}
    # Completed exports normally have a durable job. Also recover cache bindings
    # from snapshot identity for older caches whose job history was removed.
    for row in records["memory_replicas"]:
        metadata = json.loads(row["metadata_json"])
        if metadata.get("round_index"):
            cache_keys.add(_cache_key(row["source_run_id"], row["agent_id"],
                                      metadata["round_index"], row["snapshot_sha256"]))
    if "memory_pull_cache" in tables:
        records["memory_pull_cache"] = [dict(row) for row in connection.execute("SELECT * FROM memory_pull_cache")
                                        if row["cache_key"] in cache_keys]
    return records


def _cache_key(run_id, agent_id, round_index, snapshot_sha256):
    return hashlib.sha256(json.dumps([run_id, agent_id, round_index, snapshot_sha256]).encode()).hexdigest()


def _assert_idle(platform, runs, memory):
    for row in runs:
        session = row.get("screen_session")
        if row["status"] not in TERMINAL_RUN_STATUSES or (session and platform.launcher.is_active(session)):
            raise PermissionError("experiments with active runs cannot be moved, copied or merged: " + row["experiment_id"])
    workspace = getattr(platform, "_memory_workspace", None)
    for row in memory["memory_jobs"]:
        thread = workspace._threads.get(row["job_id"]) if workspace is not None else None
        if (row["status"] in {"queued", "running", "verifying"}
                or row["lease_owner"] and (row["lease_until"] or 0) > time.time()
                or thread is not None and thread.is_alive()):
            raise PermissionError("experiments with active memory transfers cannot be moved, copied or merged: " + row["job_id"])


def _plan(platform, connection, *, source_group_ids, target_group_id, requested, mode, operation):
    registry = _registry(connection)
    groups, experiments, runs = (registry[key] for key in ("experiment_groups", "experiments", "experiment_runs"))
    for group_id in [*source_group_ids, target_group_id]:
        group = groups.get(group_id)
        if group is None:
            raise KeyError(group_id)
        if group["archived_at"] is not None:
            raise PermissionError("restore archived experiment groups before moving, copying or merging: " + group_id)
    if operation == "merge":
        requested = sorted(key for key, row in experiments.items() if row["group_id"] in source_group_ids)
    for experiment_id in requested:
        if experiment_id not in experiments:
            raise KeyError(experiment_id)
        if experiments[experiment_id]["group_id"] not in source_group_ids:
            raise ValueError("selected experiment does not belong to the source group: " + experiment_id)
    dependencies, missing_runs = _dependency_graph(experiments, runs)
    dependents = {key: set() for key in experiments}
    for dependent, sources in dependencies.items():
        for source in sources:
            dependents.setdefault(source, set()).add(dependent)
    selected, pending = set(requested), list(requested)
    while pending:
        experiment_id = pending.pop()
        if missing_runs.get(experiment_id):
            raise PermissionError("experiment references unavailable source runs: " + experiment_id)
        related = dependencies[experiment_id] | (dependents[experiment_id] if mode == "move" else set())
        for other_id in related:
            other = experiments.get(other_id)
            if other is None:
                raise PermissionError("experiment references an unavailable source experiment: " + other_id)
            if other["group_id"] not in source_group_ids:
                if mode == "move" and other["group_id"] == target_group_id:
                    if other["archived_at"] is not None:
                        raise PermissionError("restore archived related experiments before regrouping: " + other_id)
                    continue
                raise PermissionError(
                    "related experiments belong to another group; merge all related groups together: " + other_id
                )
            if other_id not in selected:
                selected.add(other_id)
                pending.append(other_id)
    selected_rows = [experiments[key] for key in sorted(selected)]
    for row in selected_rows:
        if row["archived_at"] is not None:
            raise PermissionError("restore archived experiments before moving, copying or merging: " + row["experiment_id"])
    selected_runs = [row for row in runs.values() if row["experiment_id"] in selected]
    memory = _memory_records(connection, {row["run_id"] for row in selected_runs})
    _assert_idle(platform, selected_runs, memory)
    result = {
        "operation": operation, "mode": mode, "group_id": target_group_id, "target_group_id": target_group_id,
        "source_group_ids": list(source_group_ids), "requested_experiment_ids": list(requested),
        "included_experiment_ids": sorted(selected), "experiment_ids": sorted(selected),
        "experiment_count": len(selected), "run_count": len(selected_runs),
        "dependency_count": len(selected) - len(requested), "target_defaults_preserved": True,
    }
    if len(source_group_ids) == 1:
        result["source_group_id"] = source_group_ids[0]
    if operation == "merge":
        result["removed_group_ids"] = list(source_group_ids)
    snapshot = {
        "groups": [groups[key] for key in sorted([*source_group_ids, target_group_id])],
        "experiments": selected_rows,
        "runs": sorted(selected_runs, key=lambda row: row["run_id"]), "memory": memory,
        "dependencies": {key: sorted(dependencies[key]) for key in sorted(selected)},
    }
    result["plan_token"] = hashlib.sha256(json.dumps(
        {"plan": result, "snapshot": snapshot}, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    return result, snapshot


def _check_plan_token(expected, actual):
    if expected is None:
        return
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("plan_token must be a valid preview token")
    if expected != actual:
        raise RuntimeError("experiment group operation plan changed; preview again before applying")


def _move(platform, *, source_group_ids, target_group_id, requested, operation, dry_run, plan_token):
    with platform._launch_lock, closing(platform.store._connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            result, _ = _plan(platform, connection, source_group_ids=source_group_ids,
                              target_group_id=target_group_id, requested=requested, mode="move", operation=operation)
            _check_plan_token(plan_token, result["plan_token"])
            result["dry_run"] = dry_run
            if dry_run:
                connection.rollback()
                return result
            now = _now()
            for table in ("experiments", "experiment_summaries"):
                connection.executemany(
                    f"UPDATE {table} SET group_id = ?, updated_at = ? WHERE experiment_id = ?",
                    [(target_group_id, now, key) for key in result["included_experiment_ids"]],
                )
            connection.executemany("UPDATE experiment_groups SET updated_at = ? WHERE group_id = ?",
                                   [(now, key) for key in [*source_group_ids, target_group_id]])
            if operation == "merge":
                for group_id in source_group_ids:
                    if connection.execute("SELECT 1 FROM experiments WHERE group_id = ? LIMIT 1", (group_id,)).fetchone():
                        raise RuntimeError("source group gained members during merge: " + group_id)
                    connection.execute("DELETE FROM experiment_groups WHERE group_id = ?", (group_id,))
            connection.commit()
            result["completed"] = True
            return result
        except Exception:
            connection.rollback()
            raise


def _validate_copy_paths(platform, snapshot):
    _validate_owned_path(platform, platform.runtime_dir)
    for experiment in snapshot["experiments"]:
        path, folder = platform._validated_experiment_definition(experiment)
        _validate_owned_path(platform, path)
        if path.is_symlink() or folder.is_symlink():
            raise PermissionError("symlink experiment definitions cannot be copied")
    for run in snapshot["runs"]:
        for path in platform._validated_run_artifacts(run):
            _validate_owned_path(platform, path)
            if path.is_symlink():
                raise PermissionError("symlink run artifacts cannot be copied")


def _clone_memory_records(connection, maps, memory):
    """Copy local bindings and transfer history, sharing immutable export files."""
    maps["replica_id"] = {row["replica_id"]: "mr_" + uuid.uuid4().hex for row in memory["memory_replicas"]}
    maps["job_id"] = {row["job_id"]: "mj_" + uuid.uuid4().hex for row in memory["memory_jobs"]}
    maps["cache_key"] = {}
    for row in memory["memory_jobs"]:
        request = json.loads(row["metadata_json"]).get("request") or {}
        if request.get("cache_key") and request.get("run_id") in maps["run_id"]:
            maps["cache_key"][request["cache_key"]] = _cache_key(
                maps["run_id"][request["run_id"]], request.get("agent_id"),
                request.get("round_index"), request.get("snapshot_sha256"),
            )
    for row in memory["memory_replicas"]:
        metadata = json.loads(row["metadata_json"])
        if metadata.get("round_index"):
            old_key = _cache_key(row["source_run_id"], row["agent_id"], metadata["round_index"], row["snapshot_sha256"])
            maps["cache_key"][old_key] = _cache_key(maps["run_id"][row["source_run_id"]], row["agent_id"],
                                                       metadata["round_index"], row["snapshot_sha256"])
    for table in ("memory_replicas", "memory_jobs", "memory_pull_cache"):
        for original in memory[table]:
            row = _rewrite(original, maps, {})
            row["metadata_json"] = json.dumps(_rewrite(json.loads(original["metadata_json"]), maps, {}), ensure_ascii=False)
            if table == "memory_jobs":
                row.update(lease_owner=None, lease_until=None)
                metadata = json.loads(row["metadata_json"])
                metadata.update(copied_from_job_id=original["job_id"], history_only=True)
                row["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
            columns = list(row)
            names = ",".join('"' + key + '"' for key in columns)
            placeholders = ",".join("?" for _ in columns)
            connection.execute(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', [row[key] for key in columns])


def transfer_experiments(platform, *, source_group_id, target_group_id, experiment_ids,
                         mode="move", dry_run=False, plan_token=None):
    source_group_id = _group_id(source_group_id, "source_group_id")
    target_group_id = _group_id(target_group_id, "target_group_id")
    requested = _experiment_ids(experiment_ids)
    if source_group_id == target_group_id:
        raise ValueError("choose a different target experiment group")
    if not isinstance(mode, str) or mode not in {"move", "copy"}:
        raise ValueError("mode must be 'move' or 'copy'")
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a boolean")
    if mode == "move":
        return _move(platform, source_group_ids=[source_group_id], target_group_id=target_group_id,
                     requested=requested, operation="transfer", dry_run=dry_run, plan_token=plan_token)
    options = dict(source_group_ids=[source_group_id], target_group_id=target_group_id,
                   requested=requested, mode="copy", operation="transfer")
    with platform._launch_lock:
        with closing(platform.store._connect()) as connection:
            connection.execute("BEGIN")
            result, snapshot = _plan(platform, connection, **options)
            connection.rollback()
        _check_plan_token(plan_token, result["plan_token"])
        _validate_copy_paths(platform, snapshot)

        def check(connection):
            current, current_snapshot = _plan(platform, connection, **options)
            if current != result or current_snapshot != snapshot:
                raise RuntimeError("source experiments or memory records changed during copy; preview again")

        with tempfile.TemporaryDirectory(dir=platform.runtime_dir, prefix="group-copy-") as scratch:
            archive = Path(scratch) / "experiments.zip"
            export_bundle(platform, archive, experiment_ids=result["included_experiment_ids"])
            imported = import_bundle(
                platform, archive, group_id=target_group_id, dry_run=dry_run,
                reuse_registered_resources=True, precommit_check=check,
                postinsert_hook=lambda connection, maps: _clone_memory_records(connection, maps, snapshot["memory"]),
            )
        dependency_count = result["dependency_count"]
        result.update(imported, dry_run=dry_run, dependency_count=dependency_count)
        if not dry_run:
            result["completed"] = True
        return result


def merge_experiment_groups(platform, *, group_ids, target_group_id, dry_run=False, plan_token=None):
    if not isinstance(group_ids, list) or not group_ids:
        raise ValueError("group_ids must be a non-empty array")
    groups = list(dict.fromkeys(_group_id(value, "group_ids") for value in group_ids))
    target_group_id = _group_id(target_group_id, "target_group_id")
    if target_group_id not in groups:
        raise ValueError("target_group_id must belong to the selected group_ids")
    source_group_ids = [key for key in groups if key != target_group_id]
    if not source_group_ids:
        raise ValueError("choose at least one source group to merge into the target group")
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a boolean")
    return _move(platform, source_group_ids=source_group_ids, target_group_id=target_group_id,
                 requested=[], operation="merge", dry_run=dry_run, plan_token=plan_token)
