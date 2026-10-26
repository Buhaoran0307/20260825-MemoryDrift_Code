"""Read completed interaction memories without changing their source artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from agent_posting_core.memory import (
    DEFAULT_MEMORY_STRATEGY,
    MemorySnapshot,
    create_memory,
)
from agent_posting_core.cloud_memory import CLOUD_STRATEGIES
from agent_posting_core.memory_retrieval import get_retrieval_settings

from .config import PROJECT_ROOT


MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_MEMORY_SNAPSHOT_BYTES = 64 * 1024 * 1024
_SOURCE_RUNNERS = {"moltnet_thread", "moltnet_forum"}
_COMPLETED_STATUSES = {"completed", "completed_with_errors"}
_BACKEND_KEYS = {
    "api_base", "api_base_env", "api_key_env", "mem0_api_key_env", "model", "model_env",
    "concurrency", "timeout_seconds", "temperature", "max_tokens",
    "max_transient_retries", "retry_backoff_seconds", "max_retry_wait_seconds",
    "min_request_interval_seconds",
}


class _MemorySourceIntegrityError(ValueError):
    """A persisted memory snapshot exists but cannot be trusted or restored."""


def read_source_config(run: dict[str, Any]) -> dict[str, Any]:
    """Return frozen public settings, never resolve credentials or read .env."""
    run_dir, manifest = _source_manifest(run)
    return _read_source_config(run, run_dir, manifest)


def _read_source_config(
    run: dict[str, Any], run_dir: Path, manifest: dict[str, Any],
) -> dict[str, Any]:
    config_path = _inside(run_dir, "config.yaml")
    if not config_path.is_file():
        registered = run.get("worker_config_path")
        if not isinstance(registered, str) or not registered:
            raise ValueError("source run has no frozen config.yaml")
        config_path = Path(registered)
        if not config_path.is_absolute() or config_path.suffix.lower() != ".yaml":
            raise ValueError("source worker configuration path is invalid")
    try:
        raw = yaml.safe_load(_read_text(config_path))
    except yaml.YAMLError as exc:
        raise ValueError("source frozen configuration is not valid YAML") from exc
    if not isinstance(raw, dict):
        raise ValueError("source frozen configuration must be an object")
    run_config = _section(raw, "run")
    configured_id = run_config.get("id")
    if configured_id is not None and configured_id != run["run_id"]:
        raise ValueError("source frozen configuration run identity does not match")
    environment = _section(raw, "environment")
    kind = environment.get("type", "moltnet_thread")
    if kind != manifest["environment_type"]:
        raise ValueError("source configuration environment does not match its manifest")
    memory = _section(raw, "memory")
    strategy = memory.get("strategy", DEFAULT_MEMORY_STRATEGY)
    options = _section(memory, "options")
    memory_model = memory.get("model")
    if not isinstance(strategy, str):
        raise ValueError("source memory strategy must be a string")
    if memory_model is not None and (
        not isinstance(memory_model, str) or not memory_model.strip()
    ):
        raise ValueError("source memory model must be a non-empty string")
    canonical = create_memory(strategy, agent_id="source-validation", options=options)
    backend = {
        key: value for key, value in _section(raw, "backend").items()
        if key in _BACKEND_KEYS
    }
    resolved_path = _inside(run_dir, "config.resolved.json")
    if resolved_path.is_file():
        resolved = _read_json(resolved_path)
        resolved_id = _section(resolved, "run").get("run_id")
        if resolved_id is not None and resolved_id != run["run_id"]:
            raise ValueError("source resolved configuration run identity does not match")
        resolved_memory = _section(resolved, "memory")
        if resolved_memory and (
            resolved_memory.get("strategy") != canonical.strategy
            or _section(resolved_memory, "options") != options
            or (
                resolved_memory.get("model") is not None
                and memory_model is not None
                and resolved_memory.get("model") != memory_model
            )
        ):
            raise ValueError("source frozen memory configurations disagree")
        if memory_model is None:
            memory_model = resolved_memory.get("model")
        backend.update({
            key: value for key, value in _section(resolved, "backend").items()
            if key in _BACKEND_KEYS
        })
    for setting in ("model", "api_base"):
        if backend.get(setting):
            backend.pop(f"{setting}_env", None)
    return {
        "run": {
            key: value for key, value in run_config.items()
            if key in {"id", "name", "seed"}
        },
        "environment": {"type": kind},
        "memory": {
            "strategy": canonical.strategy,
            "options": options,
            **({"model": memory_model} if memory_model is not None else {}),
        },
        "backend": backend,
    }


def read_memory_source(
    run: dict[str, Any], *, agent_id: str, round_index: int | None = None,
) -> dict[str, Any]:
    """Freeze one agent's restorable memory from a completed first-stage run.

    Exported round files are authoritative. Historical rounds require their
    exported file; only the latest completed round can fall back to the
    database, whose memory tables are overwritten. Recoverable integrity
    mismatches are repaired only in the returned copy and remain visible as
    warnings; the source artifacts are never changed.
    """
    with _memory_source_reader(run, round_index=round_index) as reader:
        return reader.read_agent(agent_id)


def describe_memory_source(
    run: dict[str, Any], *, include_invalid: bool = False,
) -> dict[str, Any] | None:
    """Describe reusable agents and optionally report invalid completed sources."""
    if (
        run.get("runner_type") not in _SOURCE_RUNNERS
        or run.get("status") not in _COMPLETED_STATUSES
    ):
        return None
    fallback = {
        "run_id": str(run.get("run_id") or ""),
        "experiment_id": str(run.get("experiment_id") or ""),
        "experiment_name": str(
            run.get("experiment_name") or run.get("experiment_id") or ""
        ),
        "completed_rounds": [],
        "source_round": None,
        "memory_strategy": "",
        "memory_options": {},
        "retrieval_settings": {},
        "retrieval_top_k": None,
        "backend_model": "",
        "memory_model": "",
        "agents": [],
        "usable": False,
        "integrity_warnings": [],
    }
    try:
        with _memory_source_reader(run) as reader:
            agents = []
            warnings = []
            for agent_id in reader.manifest["selected_agent_ids"]:
                try:
                    source = reader.read_agent(agent_id)
                except _MemorySourceIntegrityError as exc:
                    warnings.append(_integrity_warning(exc, agent_id=agent_id))
                    continue
                except (OSError, TypeError, ValueError):
                    continue
                warnings.extend(source["integrity_warnings"])
                agents.append({
                    "agent_id": agent_id,
                    "display_name": source["profile"]["display_name"],
                    "memory_revision": source["snapshot"]["revision"],
                })
            if not agents and not include_invalid:
                return None
            retrieval_settings = get_retrieval_settings(
                reader.config["memory"]["strategy"],
                reader.config["memory"]["options"],
            )
            return {
                "run_id": run["run_id"],
                "experiment_id": run["experiment_id"],
                "experiment_name": str(run.get("experiment_name") or run["experiment_id"]),
                "completed_rounds": reader.manifest["completed_rounds"],
                "source_round": reader.round_index,
                "memory_strategy": reader.config["memory"]["strategy"],
                "memory_options": deepcopy(reader.config["memory"]["options"]),
                "retrieval_settings": retrieval_settings,
                "retrieval_top_k": retrieval_settings.get("retrieval_top_k"),
                "backend_model": reader.config["backend"].get("model", ""),
                "memory_model": reader.config["memory"].get("model", ""),
                "agents": agents,
                "usable": bool(agents),
                "integrity_warnings": warnings,
            }
    except _MemorySourceIntegrityError as exc:
        if not include_invalid:
            return None
        return {
            **fallback,
            "integrity_warnings": [_integrity_warning(exc)],
        }
    except (OSError, TypeError, ValueError):
        return None


def _integrity_warning(
    error: BaseException, *, agent_id: str | None = None,
) -> dict[str, str]:
    message = " ".join(str(error).split()).strip()
    if not message:
        message = "memory source validation failed"
    warning = {
        "code": "snapshot_integrity_failed",
        "message": message[:1_000],
    }
    if agent_id is not None:
        warning["agent_id"] = agent_id
    return warning


def _remote_live_warnings(*, agent_id: str, provider: str = "Mem0 Platform") -> list[dict[str, str]]:
    return [
        {
            "code": "remote_memory_not_self_contained",
            "message": (
                f"{provider} memory depends on live provider state, network access, "
                "and a configured API key."
            ),
            "agent_id": agent_id,
        },
        {
            "code": "remote_provider_may_drift",
            "message": (
                f"{provider} retrieval can change when provider state or hosted "
                "ranking behavior changes."
            ),
            "agent_id": agent_id,
        },
    ]


def _langmem_embedding_warnings(*, agent_id: str) -> list[dict[str, str]]:
    return [{
        "code": "embedding_provider_may_drift",
        "message": (
            "LangMem memory items are frozen in the snapshot, but stage-two "
            "retrieval rebuilds their vector index and embeds each query with "
            "the saved local model or remote API; embedding runtime or model changes can alter results."
        ),
        "agent_id": agent_id,
    }]


@dataclass(frozen=True)
class _MemorySourceReader:
    """One read-only source view, shared by all agents in a single request."""

    run: dict[str, Any]
    manifest: dict[str, Any]
    config: dict[str, Any]
    round_index: int
    exported: dict[str, Any] | None
    connection: sqlite3.Connection
    tables: set[str]

    def read_agent(self, agent_id: str) -> dict[str, Any]:
        if (
            not isinstance(agent_id, str)
            or agent_id not in self.manifest["selected_agent_ids"]
        ):
            raise ValueError("source agent_id must belong to the selected first-stage agents")
        if self.exported is not None and agent_id not in self.exported:
            raise _MemorySourceIntegrityError(
                f"source memory file has no snapshot for agent {agent_id}"
            )
        strategy = self.config["memory"]["strategy"]
        options = self.config["memory"]["options"]
        profile = _read_profile(self.connection, agent_id)
        try:
            stored = None
            if self.exported is None:
                stored = _database_snapshot(
                    self.connection, self.tables, self.manifest, agent_id, strategy,
                )
            wrapper = self.exported[agent_id] if self.exported is not None else stored
            if wrapper is None:
                raise ValueError(f"no saved memory snapshot exists for agent {agent_id}")
            snapshot, integrity_warnings = _restorable_snapshot(
                wrapper, agent_id, strategy, options,
            )
            if strategy == "mem0_platform_v3":
                integrity_warnings.extend(_remote_live_warnings(agent_id=agent_id))
            elif strategy in CLOUD_STRATEGIES:
                if snapshot["options"].get("ingestion_status") != "ready":
                    raise ValueError("cloud memory ingestion is incomplete; second stage unavailable")
                integrity_warnings.extend(_remote_live_warnings(agent_id=agent_id, provider=strategy))
            elif strategy == "langmem_official":
                integrity_warnings.extend(
                    _langmem_embedding_warnings(agent_id=agent_id)
                )
        except (TypeError, ValueError) as exc:
            raise _MemorySourceIntegrityError(str(exc)) from exc
        snapshot_hash = _snapshot_hash(snapshot)
        return {
            "run_id": self.run["run_id"],
            "experiment_id": self.run["experiment_id"],
            "round_index": self.round_index,
            "agent_id": agent_id,
            "profile": profile,
            "snapshot": snapshot,
            "memory_strategy": strategy,
            "memory_options": options,
            "memory_model": self.config["memory"].get("model", ""),
            "snapshot_sha256": snapshot_hash,
            "integrity_warnings": integrity_warnings,
        }


@contextmanager
def _memory_source_reader(
    run: dict[str, Any], *, round_index: int | None = None,
) -> Iterator[_MemorySourceReader]:
    run_dir, manifest = _source_manifest(run)
    completed = manifest["completed_rounds"]
    latest = completed[-1]
    if round_index is None:
        round_index = latest
    if type(round_index) is not int or round_index not in completed:
        raise ValueError("source round must be an integer in completed_rounds")
    config = _read_source_config(run, run_dir, manifest)
    memory_path = _inside(run_dir, "memory", f"memory-round-{round_index:03d}.json")
    try:
        exported = (_read_json(memory_path, max_bytes=MAX_MEMORY_SNAPSHOT_BYTES)
                    if memory_path.is_file() else None)
    except (OSError, TypeError, ValueError) as exc:
        raise _MemorySourceIntegrityError(str(exc)) from exc
    if exported is None and round_index != latest:
        raise ValueError(
            f"source memory for round {round_index} was not exported; "
            "the database only holds the latest round"
        )
    database = _inside(run_dir, "simulation.sqlite")
    if not database.is_file():
        raise ValueError("source simulation.sqlite is unavailable")
    # immutable avoids creating WAL/SHM files; never read an uncheckpointed WAL
    # through this connection, as that could silently return an older snapshot.
    for suffix in ("-wal", "-journal"):
        pending = _inside(run_dir, f"simulation.sqlite{suffix}")
        if pending.is_file() and pending.stat().st_size:
            raise ValueError("source database is still active; wait for it to close")
    try:
        with closing(sqlite3.connect(
            database.as_uri() + "?mode=ro&immutable=1", uri=True,
        )) as connection:
            connection.row_factory = sqlite3.Row
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            _check_database_rounds(
                connection, tables, run, manifest, required=exported is None,
            )
            yield _MemorySourceReader(
                run=run, manifest=manifest, config=config, round_index=round_index,
                exported=exported, connection=connection, tables=tables,
            )
    except sqlite3.Error as exc:
        raise ValueError("source simulation database cannot be read safely") from exc
    # Recheck once after the whole batch, rather than after each agent. The view
    # is never cached across requests, so a subsequent selection sees new rounds.
    if _read_json(_inside(run_dir, "manifest.json")) != manifest:
        raise ValueError("source run changed while reading its memory; select it again")


def _source_manifest(run: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    if run.get("runner_type") not in _SOURCE_RUNNERS:
        raise ValueError("memory source must be a first-stage interaction run")
    if run.get("status") not in _COMPLETED_STATUSES:
        raise ValueError("memory source run must be completed")
    for key in ("run_id", "experiment_id", "output_dir"):
        if not isinstance(run.get(key), str) or not run[key].strip():
            raise ValueError(f"memory source is missing {key}")
    run_dir = _source_run_dir(run)
    manifest = _read_json(_inside(run_dir, "manifest.json"))
    if manifest.get("run_id") != run["run_id"]:
        raise ValueError("source manifest run identity does not match")
    if manifest.get("status") not in _COMPLETED_STATUSES:
        raise ValueError("source manifest must confirm a completed run")
    if manifest.get("environment_type") != run["runner_type"]:
        raise ValueError("source manifest environment does not match its runner")
    completed = manifest.get("completed_rounds")
    if (
        not isinstance(completed, list) or not completed
        or any(type(value) is not int or value < 1 for value in completed)
        or completed != list(range(1, len(completed) + 1))
    ):
        raise ValueError("source completed_rounds must contain a completed contiguous sequence")
    for field in ("latest_round", "current_round"):
        if field in manifest and manifest[field] != completed[-1]:
            raise ValueError(f"source manifest {field} conflicts with completed_rounds")
    agents = manifest.get("selected_agent_ids")
    if (
        not isinstance(agents, list) or not agents
        or not all(isinstance(value, str) and value.strip() for value in agents)
        or len(set(agents)) != len(agents)
    ):
        raise ValueError("source manifest selected_agent_ids is invalid")
    return run_dir, manifest


def _source_run_dir(run: dict[str, Any]) -> Path:
    """Resolve a registered run path after moving between Windows and WSL."""
    registered = Path(run["output_dir"]).resolve()
    if registered.is_dir():
        return registered

    # Platform rows can survive a host switch while retaining an absolute path
    # such as /mnt/g/.../output/... or G:\\...\\output\\.... Only remap the
    # project-owned output suffix, and confirm the run identity from manifest next.
    parts = PurePosixPath(str(run["output_dir"]).replace("\\", "/")).parts
    output_root = (PROJECT_ROOT / "output").resolve()
    for index, part in enumerate(parts):
        if part != "output":
            continue
        candidate = PROJECT_ROOT.joinpath(*parts[index:]).resolve()
        if (
            output_root in candidate.parents
            and candidate.name == run["run_id"]
            and candidate.is_dir()
        ):
            return candidate
    raise ValueError("memory source output directory is unavailable")


def _read_profile(connection: sqlite3.Connection, agent_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT agent_id, display_name, persona, topic FROM agents WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"source agent {agent_id} is missing from simulation.sqlite")
    # Imported initial_history must never bypass the chosen memory strategy.
    return {**dict(row), "metadata": {}}


def _check_database_rounds(
    connection: sqlite3.Connection, tables: set[str],
    run: dict[str, Any], manifest: dict[str, Any], *, required: bool,
) -> None:
    if "simulation_rounds" not in tables:
        if required:
            raise ValueError("source database cannot confirm the latest completed round")
        return
    rows = connection.execute(
        "SELECT round_index, status FROM simulation_rounds WHERE run_id = ? "
        "ORDER BY round_index", (run["run_id"],),
    ).fetchall()
    if (
        [row["round_index"] for row in rows] != manifest["completed_rounds"]
        or any(row["status"] not in _COMPLETED_STATUSES for row in rows)
    ):
        raise ValueError("source database rounds conflict with its completed manifest")


def _database_snapshot(
    connection: sqlite3.Connection, tables: set[str], manifest: dict[str, Any],
    agent_id: str, strategy: str,
) -> dict[str, Any] | None:
    if manifest["environment_type"] in {"moltnet_thread"}:
        table = "agent_memory_snapshots"
        scope_column, scope = "post_id", manifest.get("post_id")
        if type(scope) is not int or scope < 1:
            raise ValueError("source thread manifest is missing a valid post_id")
    else:
        table = "environment_memory_snapshots"
        scope_column, scope = "scope_id", manifest["run_id"]
    if table not in tables:
        return None
    limit = MAX_MEMORY_SNAPSHOT_BYTES
    row = connection.execute(
        "SELECT revision, CASE WHEN length(CAST(snapshot_json AS BLOB)) <= ? "
        "THEN snapshot_json ELSE NULL END AS snapshot_json, "
        "length(CAST(snapshot_json AS BLOB)) AS size "
        f"FROM {table} WHERE {scope_column} = ? AND agent_id = ? AND strategy = ?",
        (limit, scope, agent_id, strategy),
    ).fetchone()
    if row is None:
        return None
    if row["size"] > limit:
        raise ValueError(
            f"source memory snapshot exceeds the {limit / (1024 * 1024):g} MiB "
            f"size limit ({limit} bytes)"
        )
    try:
        return {"revision": row["revision"], "snapshot": json.loads(row["snapshot_json"])}
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("source database memory snapshot is not valid JSON") from exc


def _validate_snapshot(
    wrapper: Any, agent_id: str, strategy: str, options: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("snapshot"), dict):
        raise ValueError(f"source memory for agent {agent_id} is missing a snapshot object")
    if wrapper.get("agent_id", agent_id) != agent_id:
        raise ValueError("source snapshot agent identity does not match")
    snapshot = wrapper["snapshot"]
    if snapshot.get("agent_id", agent_id) != agent_id:
        raise ValueError("source snapshot agent identity does not match")
    if snapshot.get("strategy") != strategy:
        raise ValueError("source snapshot strategy does not match its frozen configuration")
    revision = snapshot.get("revision")
    events = snapshot.get("events")
    if (
        type(revision) is not int or revision < 0
        or type(wrapper.get("revision")) is not int
        or wrapper["revision"] != revision
        or not isinstance(events, list)
        or not all(isinstance(item, dict) for item in events)
        or not isinstance(snapshot.get("options"), dict)
    ):
        raise ValueError("source snapshot revision, events or options is invalid")
    for event in events:
        if event.get("kind") == "profile_identity" and event.get("actor_id") != agent_id:
            raise ValueError("source snapshot profile identity belongs to another agent")
    if not events or revision == 0:
        raise ValueError("selected agent has no saved interaction memory")
    try:
        memory = create_memory(strategy, agent_id=agent_id, options=options)
        memory.restore(MemorySnapshot.from_dict(snapshot))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"source memory snapshot cannot be restored: {exc}") from exc
    return snapshot


def _restorable_snapshot(
    wrapper: Any, agent_id: str, strategy: str, options: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return a strict snapshot, repairing only redundant integrity metadata.

    A warning is retained whenever the original snapshot fails validation. The
    repaired copy must then pass the complete normal restore check, so malformed
    JSON, identity/strategy conflicts, missing data, and unrestorable memories
    remain hard failures.
    """
    try:
        return _validate_snapshot(wrapper, agent_id, strategy, options), []
    except (TypeError, ValueError) as original:
        repaired = _repair_recoverable_integrity(wrapper, strategy)
        if repaired is None:
            raise
        try:
            snapshot = _validate_snapshot(repaired, agent_id, strategy, options)
        except (TypeError, ValueError):
            raise original
        return snapshot, [_integrity_warning(original, agent_id=agent_id)]


def _repair_recoverable_integrity(wrapper: Any, strategy: str) -> dict[str, Any] | None:
    """Repair a readable snapshot copy; never alter source artifacts."""
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("snapshot"), dict):
        return None
    repaired = deepcopy(wrapper)
    snapshot = repaired["snapshot"]
    changed = False

    # The outer revision duplicates the canonical revision stored in the
    # snapshot. A mismatch can be reconciled without changing memory content.
    revision = snapshot.get("revision")
    events = snapshot.get("events")
    if (
        type(revision) is int
        and isinstance(events, list)
        and revision == len(events)
        and type(repaired.get("revision")) is int
        and repaired["revision"] != revision
    ):
        repaired["revision"] = revision
        changed = True

    # A-Mem stores the canonical observed events alongside derived notes. If a
    # note's text fails the provenance check, use the event's verbatim content
    # and author in the returned copy. Keep the frozen vector and all other note
    # metadata, retain an integrity warning, and require the repaired snapshot
    # to pass the adapter's complete restore validation below.
    if strategy == "a_mem_official":
        snapshot_options = snapshot.get("options")
        records = (
            snapshot_options.get("records")
            if isinstance(snapshot_options, dict)
            else None
        )
        if isinstance(events, list) and isinstance(records, list):
            events_by_id = {
                event.get("event_id"): event
                for event in events
                if isinstance(event, dict) and isinstance(event.get("event_id"), str)
            }
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("note"), dict):
                    continue
                event = events_by_id.get(record.get("source_event_id"))
                if event is None or not isinstance(event.get("content"), str):
                    continue
                actor_id = event.get("actor_id")
                speaker = actor_id if isinstance(actor_id, str) and actor_id else "unknown"
                legacy_content = f"Speaker {speaker} says:\n{event['content']}"
                if record["note"].get("content") not in {event["content"], legacy_content}:
                    record["note"]["content"] = event["content"]
                    changed = True
    return repaired if changed else None


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _inside(run_dir: Path, *parts: str) -> Path:
    path = run_dir.joinpath(*parts).resolve()
    if not path.is_relative_to(run_dir):
        raise ValueError("source artifact path escapes its registered run directory")
    return path


def _read_text(path: Path, *, max_bytes: int | None = None) -> str:
    limit = MAX_SOURCE_BYTES if max_bytes is None else max_bytes
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise ValueError(
                f"source artifact exceeds the {limit / (1024 * 1024):g} MiB "
                f"size limit ({limit} bytes)"
            )
        return raw.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"source artifact {path.name} is unavailable or not UTF-8") from exc


def _read_json(path: Path, *, max_bytes: int | None = None) -> dict[str, Any]:
    try:
        value = json.loads(_read_text(path, max_bytes=max_bytes))
    except json.JSONDecodeError as exc:
        raise ValueError(f"source artifact {path.name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"source artifact {path.name} must contain an object")
    return value


def _section(value: dict[str, Any], key: str) -> dict[str, Any]:
    section = value.get(key, {})
    if not isinstance(section, dict):
        raise ValueError(f"source configuration {key} must be an object")
    return section


__all__ = ["describe_memory_source", "read_memory_source", "read_source_config"]
