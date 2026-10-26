"""Portable experiment archives: dependency closure, verified files and atomic import.

Only platform-owned records and artifacts are transported. Provider-side memory
namespaces remain provider references; their credentials are never bundled.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .platform_store import TERMINAL_RUN_STATUSES

FORMAT = "agent-community-experiments"
VERSION = 1
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_EXPANDED_BYTES = 8 * 1024**3
MAX_FILES = 100000
MAX_MANIFEST_BYTES = 64 * 1024**2
TABLE_KEYS = {
    "platform_resources": "resource_id",
    "platform_resource_revisions": "revision_id",
    "platform_target_mappings": "mapping_id",
    "experiment_groups": "group_id",
    "experiment_group_resource_refs": None,
    "experiments": "experiment_id",
    "experiment_summaries": None,
    "experiment_resource_refs": None,
    "experiment_runs": "run_id",
    "run_resource_refs": None,
    "run_metrics": None,
}
ID_FIELDS = {
    "source_experiment_id": "experiment_id",
    "memory_source_experiment_id": "experiment_id",
    "source_run_id": "run_id",
    "display_source_run_id": "run_id",
    "scope_id": "run_id",
    "resource_revision_id": "revision_id",
    "target_mapping_id": "mapping_id",
}
PATH_FIELDS = {"root_dir", "output_dir", "run_dir", "config_path", "worker_config_path", "log_path", "db_path"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _references(value: Any, field: str) -> set[str]:
    if isinstance(value, dict):
        found = {value[field]} if isinstance(value.get(field), str) and value[field] else set()
        for key, child in value.items():
            if key in {"snapshot", "dataset", "content", "arguments", "tool_result", "specified_memory"}:
                continue
            found.update(_references(child, field))
        return found
    if isinstance(value, list):
        return set().union(*(_references(child, field) for child in value)) if value else set()
    return set()


def _rows(connection, table):
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]


def export_bundle(platform, destination: Path, *, group_id=None, experiment_ids=None):
    """Write a ZIP; a selected stage-two experiment brings its source experiments."""
    if bool(group_id) == bool(experiment_ids):
        raise ValueError("choose group_id or a non-empty experiment_ids array")
    if experiment_ids is not None and (
        not isinstance(experiment_ids, list)
        or not all(isinstance(item, str) and item for item in experiment_ids)
    ):
        raise ValueError("experiment_ids must be an array of non-empty strings")
    with platform._launch_lock, closing(platform.store._connect()) as connection:
        connection.execute("BEGIN")
        rows = {table: _rows(connection, table) for table in TABLE_KEYS}
        connection.rollback()
        experiments = {row["experiment_id"]: row for row in rows["experiments"]}
        runs = {row["run_id"]: row for row in rows["experiment_runs"]}
        if group_id and not any(row["group_id"] == group_id for row in rows["experiment_groups"]):
            raise ValueError("unknown experiment group")
        selected = set(experiment_ids or [key for key, row in experiments.items() if row["group_id"] == group_id])
        if not selected or not selected <= experiments.keys():
            raise ValueError("no experiments selected or unknown experiment id")
        requested = sorted(selected)
        pending = list(selected)
        while pending:
            key = pending.pop()
            config = json.loads(experiments[key]["config_json"])
            dependencies = _references(config, "source_experiment_id")
            dependencies.update(_references(config, "memory_source_experiment_id"))
            for run_id in _references(config, "source_run_id"):
                if run_id not in runs:
                    raise ValueError(f"missing source run: {run_id}")
                dependencies.add(runs[run_id]["experiment_id"])
            for dependency in dependencies:
                if dependency not in experiments:
                    raise ValueError(f"missing source experiment: {dependency}")
                if dependency not in selected:
                    selected.add(dependency)
                    pending.append(dependency)
        selected_runs = {key for key, row in runs.items() if row["experiment_id"] in selected}
        if any(runs[key]["status"] not in TERMINAL_RUN_STATUSES for key in selected_runs):
            raise ValueError("selected experiments or their sources have active runs; export after they stop")
        groups = {experiments[key]["group_id"] for key in selected}
        rows["experiment_groups"] = [r for r in rows["experiment_groups"] if r["group_id"] in groups]
        for table in ("experiments", "experiment_summaries", "experiment_resource_refs"):
            rows[table] = [r for r in rows[table] if r["experiment_id"] in selected]
        for table in ("experiment_runs", "run_metrics", "run_resource_refs"):
            rows[table] = [r for r in rows[table] if r["run_id"] in selected_runs]
        rows["experiment_group_resource_refs"] = [r for r in rows["experiment_group_resource_refs"] if r["group_id"] in groups]
        revisions = {r["revision_id"] for table in ("experiment_group_resource_refs", "experiment_resource_refs", "run_resource_refs") for r in rows[table]}
        for key in selected:
            revisions.update(_references(json.loads(experiments[key]["config_json"]), "resource_revision_id"))
        rows["platform_resource_revisions"] = [r for r in rows["platform_resource_revisions"] if r["revision_id"] in revisions]
        if revisions != {r["revision_id"] for r in rows["platform_resource_revisions"]}:
            raise ValueError("missing resource revision")
        resources = {r["resource_id"] for r in rows["platform_resource_revisions"]}
        rows["platform_resources"] = [r for r in rows["platform_resources"] if r["resource_id"] in resources]
        mappings = {r.get("target_mapping_id") for r in rows["experiment_groups"]}
        for key in selected:
            mappings.update(_references(json.loads(experiments[key]["config_json"]), "target_mapping_id"))
        rows["platform_target_mappings"] = [r for r in rows["platform_target_mappings"] if r["mapping_id"] in mappings]
        files = []
        expanded_size = 0
        empty_outputs = []
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=platform.runtime_dir, prefix="bundle-export-") as scratch:
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                def add(path, name, run_id, role, relative=""):
                    nonlocal expanded_size
                    if path.is_symlink():
                        raise ValueError("symlink artifacts cannot be exported")
                    _safe_relative(name)
                    source = path
                    if path.suffix in {".sqlite", ".db"}:
                        source = Path(scratch) / f"{len(files)}.sqlite"
                        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                            with closing(sqlite3.connect(source)) as target:
                                db.backup(target)
                    expanded_size += source.stat().st_size
                    if expanded_size > MAX_EXPANDED_BYTES or len(files) >= MAX_FILES - 1:
                        raise ValueError("selected artifacts exceed bundle size/file limits; export fewer experiments")
                    archive.write(source, name)
                    files.append({"name": name, "run_id": run_id, "role": role, "relative": relative,
                                  "size": source.stat().st_size, "sha256": _hash(source)})
                for index, key in enumerate(sorted(selected_runs)):
                    row = runs[key]
                    output = Path(row["output_dir"])
                    if not output.is_dir() and row["status"] not in {"failed", "interrupted"}:
                        raise ValueError(f"run artifacts unavailable: {key}")
                    file_count = len(files)
                    for path in sorted(output.rglob("*")):
                        if path.is_symlink():
                            raise ValueError("symlink artifacts cannot be exported")
                        if path.is_file() and not path.name.endswith(("-wal", "-shm")):
                            relative = path.relative_to(output).as_posix()
                            add(path, f"runs/{index}/output/{relative}", key, "output", relative)
                    if len(files) == file_count:
                        empty_outputs.append(key)
                    worker = Path(row["worker_config_path"])
                    if not worker.is_file():
                        worker = output / "config.yaml"
                    if not worker.is_file():
                        raise ValueError(f"worker configuration unavailable: {key}")
                    add(worker, f"runs/{index}/worker.yaml", key, "worker")
                    log = Path(row["log_path"])
                    if log.is_file():
                        add(log, f"runs/{index}/worker.log", key, "log")
                manifest = {"format": FORMAT, "version": VERSION, "tables": rows, "files": files,
                            "requested_experiment_ids": requested, "project_root": str(platform.project_root),
                            "empty_output_run_ids": empty_outputs,
                            "default_group_id": group_id or experiments[requested[0]]["group_id"]}
                encoded = _json(manifest).encode("utf-8")
                if len(encoded) > MAX_MANIFEST_BYTES:
                    raise ValueError("bundle configuration data exceeds 64 MiB; export fewer experiments")
                archive.writestr("bundle.json", encoded)
        if destination.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("bundle exceeds 2 GiB; export fewer experiments")
        return {"experiment_count": len(selected), "run_count": len(selected_runs),
                "dependency_count": len(selected) - len(requested)}


def _safe_relative(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("invalid archive path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"..", "."} for part in name.split("/")):
        raise ValueError("archive path escapes its directory")
    if any(part.rstrip(" .") != part or part.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))
    } for part in path.parts):
        raise ValueError("unsafe archive filename")
    return path


def _read_bundle(archive):
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if len(names) > MAX_FILES or len(set(name.casefold() for name in names)) != len(names):
        raise ValueError("duplicate filenames or too many archive members")
    if sum(item.file_size for item in infos) > MAX_EXPANDED_BYTES:
        raise ValueError("archive exceeds expanded size limit")
    for item in infos:
        _safe_relative(item.filename)
        if item.is_dir() or (item.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError("directory and symlink entries are not supported")
    if "bundle.json" not in names or archive.getinfo("bundle.json").file_size > MAX_MANIFEST_BYTES:
        raise ValueError("missing or oversized bundle manifest")
    data = json.loads(archive.read("bundle.json"))
    if data.get("format") != FORMAT or data.get("version") != VERSION:
        raise ValueError("unsupported experiment bundle format/version")
    if set(data.get("tables", {})) != set(TABLE_KEYS):
        raise ValueError("incomplete bundle registry")
    entries = data.get("files", [])
    if {entry["name"] for entry in entries} != set(names) - {"bundle.json"} or len(entries) != len(names) - 1:
        raise ValueError("archive files do not match the manifest")
    for entry in entries:
        with archive.open(entry["name"]) as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != entry["sha256"] or entry["size"] != archive.getinfo(entry["name"]).file_size:
            raise ValueError(f"archive checksum mismatch: {entry['name']}")
    return data


def _rewrite(value, maps, paths, key="", parent=""):
    if isinstance(value, dict):
        # Memory snapshots, input datasets and model responses are evidence, not
        # platform identifiers. Never replace coincidentally matching user text.
        return {k: (v if k in {"snapshot", "dataset", "content", "arguments", "tool_result", "imported_from", "specified_memory"}
                    else _rewrite(v, maps, paths, k, key)) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, maps, paths, key, parent) for v in value]
    if not isinstance(value, str):
        return value
    kind = ID_FIELDS.get(key, key)
    if key == "id" and parent == "run":
        kind = "run_id"
    if kind in maps:
        return maps[kind].get(value, value)
    if key in PATH_FIELDS:
        normalized = value.replace("\\", "/").rstrip("/")
        for old, new in sorted(paths.items(), key=lambda item: -len(item[0])):
            if normalized == old or normalized.startswith(old + "/"):
                return new + normalized[len(old):]
    return value


def _rewrite_sqlite(path, maps):
    with closing(sqlite3.connect(path)) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({quoted})")}
            for column in ("run_id", "scope_id"):
                if column in columns:
                    for old, new in maps["run_id"].items():
                        db.execute(f'UPDATE {quoted} SET "{column}"=? WHERE "{column}"=?', (new, old))
            if table == "simulation_rounds" and {"run_id", "round_index", "record_json"} <= columns:
                for run_id, round_index, record in db.execute(
                    "SELECT run_id, round_index, record_json FROM simulation_rounds"
                ).fetchall():
                    value = _rewrite_envelope(json.loads(record), maps, {})
                    db.execute("UPDATE simulation_rounds SET record_json=? WHERE run_id=? AND round_index=?",
                               (_json(value), run_id, round_index))
        db.commit()


def _rewrite_envelope(value, maps, paths):
    """Update navigable record IDs without touching request/response evidence."""
    if isinstance(value, dict):
        for key in ("run_id", "source_run_id", "experiment_id", "source_experiment_id", "run_dir"):
            if key in value:
                value[key] = _rewrite(value[key], maps, paths, key)
    return value


def _register_embedded_defaults(rows, group):
    """Old embedded groups become usable by the current resource-based editor."""
    from .platform_service import _normalize_registered_resource
    refs = rows["experiment_group_resource_refs"]
    for role in ("queries", "mcp"):
        if any(row["group_id"] == group["group_id"] and row["role"] == role for row in refs):
            continue
        embedded = json.loads(group.get(role + "_json") or "null")
        if not embedded:
            for experiment in rows["experiments"]:
                candidate = json.loads(experiment["config_json"]).get("validation", {}).get(role)
                if isinstance(candidate, dict) and candidate.get("dataset"):
                    embedded = candidate
                    break
        if not embedded:
            continue
        normalized = _normalize_registered_resource(kind=role, filename=embedded["filename"],
            content=_json(embedded["dataset"]), environment_type=None)
        resource_id, revision_id = uuid.uuid4().hex, uuid.uuid4().hex
        created = group["created_at"]
        rows["platform_resources"].append({"resource_id": resource_id, "kind": role,
            "name": embedded["filename"], "description": "Imported group resource", "created_at": created,
            "updated_at": created, "archived_at": None})
        rows["platform_resource_revisions"].append({"revision_id": revision_id, "resource_id": resource_id,
            "revision_number": 1, "filename": normalized["filename"], "environment_type": None,
            "content_json": _json(normalized["file"]), "content_sha256": normalized["content_sha256"],
            "summary_json": _json(normalized["summary"]), "created_at": created})
        refs.append({"group_id": group["group_id"], "role": role, "revision_id": revision_id})


def _same_registered_record(table, original, existing):
    """Registry metadata may be renamed; immutable resource content must match."""
    fields = {
        "platform_resources": ("kind",),
        "platform_resource_revisions": (
            "resource_id", "revision_number", "filename", "environment_type",
            "content_json", "content_sha256", "summary_json",
        ),
        "platform_target_mappings": ("filename", "content_json", "content_sha256"),
    }[table]
    return all(original[field] == existing[field] for field in fields)


def _validate_owned_path(platform, path):
    """Do not stage or promote imports through symlinks outside their owner."""
    root = platform.project_root.absolute()
    path = Path(path).absolute()
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
        raise PermissionError("import artifact path is outside the project tree")
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise PermissionError("import artifact paths cannot pass through symbolic links")


def import_bundle(platform, source: Path, *, group_id=None, new_group=None, dry_run=False,
                  reuse_registered_resources=False, precommit_check=None, postinsert_hook=None):
    """Validate everything before committing records; roll back promoted files on error."""
    if bool(group_id) == bool(new_group):
        raise ValueError("choose an existing group_id or new_group")
    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds 2 GiB upload limit")
    for directory in (platform.runtime_dir, platform.exp_dir, platform.jobs_dir, platform.logs_dir,
                      platform.project_root / "output" / "imports"):
        _validate_owned_path(platform, directory)
    with platform._launch_lock, zipfile.ZipFile(source) as archive:
        data = _read_bundle(archive)
        rows = copy.deepcopy(data["tables"])
        if not rows["experiments"] or not rows["experiment_groups"]:
            raise ValueError("bundle has no experiments or source groups")
        rows["experiment_groups"].sort(key=lambda row: row["group_id"] != data.get("default_group_id"))
        if new_group:
            _register_embedded_defaults(rows, rows["experiment_groups"][0])
        maps = {}
        reused = {}
        from .platform_service import _resource_content_sha256
        for table in ("platform_resource_revisions", "platform_target_mappings"):
            for row in rows[table]:
                if _resource_content_sha256(json.loads(row["content_json"])) != row["content_sha256"]:
                    raise ValueError("resource content checksum mismatch")
        for table, key in TABLE_KEYS.items():
            if key:
                ids = [row[key] for row in rows[table]]
                if len(ids) != len(set(ids)) or not all(isinstance(item, str) and item for item in ids):
                    raise ValueError(f"duplicate or invalid IDs in {table}")
                maps[key] = {old: f"import-{uuid.uuid4().hex}" for old in ids}
        if reuse_registered_resources:
            with closing(platform.store._connect()) as connection:
                for table in ("platform_resources", "platform_resource_revisions", "platform_target_mappings"):
                    key = TABLE_KEYS[table]
                    reused[table] = set()
                    for row in rows[table]:
                        existing = connection.execute(
                            f'SELECT * FROM "{table}" WHERE "{key}" = ?', (row[key],),
                        ).fetchone()
                        if existing is None:
                            continue
                        if not _same_registered_record(table, row, existing):
                            raise ValueError("registered resource identity has different content: " + row[key])
                        maps[key][row[key]] = row[key]
                        reused[table].add(row[key])
        if group_id:
            group = platform.store.get_experiment_group(group_id)
            if not group or group.get("archived") or group_id == "legacy":
                raise ValueError("choose an existing active, non-legacy experiment group")
        else:
            group_id = new_group.get("group_id", "")
            if not isinstance(group_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", group_id) or group_id == "legacy":
                raise ValueError("new group ID must use 1-64 lowercase letters, digits, '_' or '-'")
            if not isinstance(new_group.get("name"), str) or not new_group["name"].strip():
                raise ValueError("new group name is required")
            if platform.store.get_experiment_group(group_id):
                raise FileExistsError("experiment group already exists")
        maps["group_id"] = {old: group_id for old in maps["group_id"]}
        paths = {}
        for row in rows["experiments"]:
            paths[row["config_path"].replace("\\", "/")] = str(platform.exp_dir / maps["experiment_id"][row["experiment_id"]] / "config.yaml")
        for row in rows["experiment_runs"]:
            if row["status"] not in TERMINAL_RUN_STATUSES:
                raise ValueError("bundle contains active runs")
            run_id = maps["run_id"][row["run_id"]]
            for field, target in {
                "output_dir": platform.project_root / "output" / "imports" / run_id,
                "worker_config_path": platform.jobs_dir / f"{run_id}.yaml",
                "log_path": platform.logs_dir / f"{run_id}.log",
            }.items():
                paths[row[field].replace("\\", "/").rstrip("/")] = str(target)
        def rewrite(value):
            return _rewrite(value, maps, paths)
        original_groups = rows["experiment_groups"]
        if new_group:
            group = rewrite(original_groups[0])
            group.update(group_id=group_id, name=new_group["name"].strip(), archived_at=None)
            rows["experiment_groups"] = [group]
            rows["experiment_group_resource_refs"] = [row for row in rows["experiment_group_resource_refs"] if row["group_id"] == original_groups[0]["group_id"]]
        else:
            rows["experiment_groups"] = []
            rows["experiment_group_resource_refs"] = []
        for table in TABLE_KEYS:
            for index, row in enumerate(rows[table]):
                # Resource content and hashes are preserved byte for byte.
                row = rewrite(row)
                for field in ("config_json",):
                    if field in row:
                        config = rewrite(json.loads(row[field]))
                        config.setdefault("output", {})["root_dir"] = str(platform.project_root / "output" / "imports")
                        row[field] = _json(config)
                if "archived_at" in row:
                    row["archived_at"] = None
                if table == "experiment_runs":
                    row["screen_session"] = None
                if table == "run_metrics" and row["metric_key"] == "validation.source_run_id":
                    old = json.loads(row["metric_value_json"])
                    row["metric_value_json"] = _json(maps["run_id"].get(old, old))
                rows[table][index] = row
        if new_group:
            for role, field in (("queries", "query_revision_id"), ("mcp", "mcp_revision_id")):
                if new_group.get(field):
                    platform._resource_revision(new_group[field], expected_kind=role, allow_archived=False)
                    rows["experiment_group_resource_refs"] = [r for r in rows["experiment_group_resource_refs"] if r["role"] != role]
                    rows["experiment_group_resource_refs"].append({"group_id": group_id, "role": role, "revision_id": new_group[field]})
        # Validate IDs inside configurations; SQLite FKs do not cover JSON links.
        for row in rows["experiments"]:
            config = json.loads(row["config_json"])
            for field, kind in (("source_experiment_id", "experiment_id"), ("source_run_id", "run_id"), ("resource_revision_id", "revision_id")):
                if not _references(config, field) <= set(maps[kind].values()):
                    raise ValueError(f"bundle has an unresolved {field}")
        if {r["experiment_id"] for r in rows["experiment_summaries"]} != set(maps["experiment_id"].values()):
            raise ValueError("bundle is missing experiment summaries")
        revisions = {row["revision_id"]: row for row in rows["platform_resource_revisions"]}
        for row in rows["run_resource_refs"]:
            if row["revision_id"] not in revisions or row["content_sha256"] != revisions[row["revision_id"]]["content_sha256"]:
                raise ValueError("run resource checksum mismatch")
        result = {"group_id": group_id, "experiment_count": len(rows["experiments"]),
                  "run_count": len(rows["experiment_runs"]), "id_map": maps,
                  "dependency_count": len(rows["experiments"]) - len(data.get("requested_experiment_ids", [])),
                  "dry_run": bool(dry_run)}
        if reuse_registered_resources:
            result.update(
                reused_resource_count=len(reused["platform_resources"]),
                reused_revision_count=len(reused["platform_resource_revisions"]),
                reused_target_mapping_count=len(reused["platform_target_mappings"]),
            )
        with tempfile.TemporaryDirectory(dir=platform.runtime_dir, prefix="bundle-import-") as scratch:
            staged = Path(scratch)
            promotions = []
            for row in rows["experiments"]:
                folder = staged / "exp" / row["experiment_id"]
                folder.mkdir(parents=True)
                (folder / "config.yaml").write_text(yaml.safe_dump(json.loads(row["config_json"]), allow_unicode=True), encoding="utf-8")
                promotions.append((folder, platform.exp_dir / row["experiment_id"]))
            runs = {row["run_id"]: row for row in rows["experiment_runs"]}
            for original in data.get("empty_output_run_ids", []):
                if original not in maps["run_id"]:
                    raise ValueError("unknown empty output run")
                (staged / "output" / maps["run_id"][original]).mkdir(parents=True, exist_ok=True)
            destinations = set()
            worker_ids = set()
            for entry in data["files"]:
                if entry["run_id"] not in maps["run_id"]:
                    raise ValueError("artifact references an unknown run")
                run_id = maps["run_id"][entry["run_id"]]
                role = entry["role"]
                if role == "output":
                    relative = _safe_relative(entry["relative"])
                    target = staged / "output" / run_id / relative
                elif role in {"worker", "log"}:
                    target = staged / role / (run_id + (".yaml" if role == "worker" else ".log"))
                    if role == "worker":
                        worker_ids.add(run_id)
                else:
                    raise ValueError("invalid artifact role")
                if str(target).casefold() in destinations:
                    raise ValueError("duplicate artifact destination")
                destinations.add(str(target).casefold())
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry["name"]) as source_file, target.open("wb") as dest:
                    shutil.copyfileobj(source_file, dest)
                # Rewrite metadata and configurations only; raw memory and query
                # evidence remain unchanged, including JSON and JSONL payloads.
                if role == "worker" or (role == "output" and entry["relative"] in {
                    "config.yaml", "config.resolved.json", "manifest.json", "source-memory.json",
                }):
                    is_yaml = target.suffix == ".yaml"
                    value = yaml.safe_load(target.read_text(encoding="utf-8")) if is_yaml else json.loads(target.read_text(encoding="utf-8"))
                    value = rewrite(value)
                    if target.name == "manifest.json":
                        value.setdefault("imported_from", {"run_id": entry["run_id"]})
                        value.setdefault("memory_scope_run_id", entry["run_id"])
                    if target.name in {"config.yaml", "config.resolved.json"} or role == "worker":
                        value.setdefault("output", {})["root_dir"] = str(platform.project_root / "output" / "imports")
                    target.write_text(yaml.safe_dump(value, allow_unicode=True) if is_yaml else _json(value), encoding="utf-8")
                elif role == "output" and target.name == "simulation.sqlite":
                    _rewrite_sqlite(target, maps)
                elif role == "output" and target.suffix == ".json" and entry["relative"].split("/")[0] in {"selections", "rounds"}:
                    value = json.loads(target.read_text(encoding="utf-8"))
                    target.write_text(_json(_rewrite_envelope(value, maps, paths)), encoding="utf-8")
            if worker_ids != runs.keys():
                raise ValueError("missing worker configuration")
            for run_id, row in runs.items():
                output = staged / "output" / run_id
                if not output.is_dir():
                    raise ValueError("missing run output artifacts")
                promotions.extend([(output, Path(row["output_dir"])),
                                   (staged / "worker" / f"{run_id}.yaml", Path(row["worker_config_path"]))])
                log = staged / "log" / f"{run_id}.log"
                if log.exists():
                    promotions.append((log, Path(row["log_path"])))
                # Validate persisted identities without requiring destination API
                # credentials or optional adapter installations just to import.
                config = yaml.safe_load((staged / "worker" / f"{run_id}.yaml").read_text(encoding="utf-8"))
                if not isinstance(config, dict) or config.get("run", {}).get("id") not in {None, run_id}:
                    raise ValueError("worker run identity mismatch")
                frozen = config.get("validation", {}).get("memory_source")
                if frozen:
                    specified = config.get("validation", {}).get("memory_source_mode") == "specified"
                    if specified:
                        from agent_posting_core.specified_memory import build_specified_memory_source
                        declared = config["validation"]
                        expected = build_specified_memory_source(
                            declared.get("specified_memory"), declared.get("specified_agent_id", "ROWAN"),
                        )
                        if frozen != expected:
                            raise ValueError("frozen specified memory does not match the declared input")
                    elif frozen.get("run_id") not in maps["run_id"].values():
                        raise ValueError("frozen memory references a missing run")
                    if _resource_content_sha256(frozen["snapshot"]) != frozen.get("snapshot_sha256"):
                        raise ValueError("frozen memory checksum mismatch")
            with closing(platform.store._connect()) as connection:
                moved = []
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    if precommit_check is not None:
                        precommit_check(connection)
                    if not new_group:
                        current_group = connection.execute(
                            "SELECT archived_at FROM experiment_groups WHERE group_id = ?", (group_id,),
                        ).fetchone()
                        if current_group is None or current_group["archived_at"] is not None:
                            raise PermissionError("the destination experiment group is no longer active")
                    for table in TABLE_KEYS:
                        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                        for row in rows[table]:
                            key = TABLE_KEYS[table]
                            if key and row[key] in reused.get(table, set()):
                                existing = connection.execute(
                                    f'SELECT * FROM "{table}" WHERE "{key}" = ?', (row[key],),
                                ).fetchone()
                                if existing is None or not _same_registered_record(table, row, existing):
                                    raise RuntimeError("registered resource changed during import: " + row[key])
                                continue
                            if not set(row) <= columns:
                                raise ValueError(f"unsupported columns in {table}")
                            fields = list(row)
                            names = ",".join(f'"{name}"' for name in fields)
                            placeholders = ",".join("?" for _ in fields)
                            connection.execute(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', [row[name] for name in fields])
                    if postinsert_hook is not None:
                        postinsert_hook(connection, maps)
                    if dry_run:
                        connection.rollback()
                    else:
                        for source_path, target in promotions:
                            _validate_owned_path(platform, target)
                            if target.exists() or target.is_symlink():
                                raise FileExistsError("import destination already exists")
                            target.parent.mkdir(parents=True, exist_ok=True)
                            moved.append(target)
                            shutil.move(str(source_path), str(target))
                        connection.commit()
                except Exception:
                    connection.rollback()
                    for path in reversed(moved):
                        if path.is_dir():
                            shutil.rmtree(path)
                        else:
                            path.unlink(missing_ok=True)
                    raise
        return result
