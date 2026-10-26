"""Frozen configuration for single-agent, second-stage tool selection."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePath
from typing import Any

from agent_posting_core.memory import MemorySnapshot, create_memory
from agent_posting_core.memory_retrieval import merge_retrieval_options
from agent_posting_core.specified_memory import (
    STRATEGY as SPECIFIED_MEMORY_STRATEGY,
    build_specified_memory_source,
    require_scenario_memories,
)
from agent_posting_core.tool_selection import (
    API_KEY_POSITIONS,
    apply_mcp_parameter_additions,
    normalize_mcp_parameter_additions,
    normalize_mcp_dataset,
    normalize_query_dataset,
    validate_selection_inputs,
)
from .validation_metrics import DEFAULT_EXACT_LEAK_VALUE, DEFAULT_LEAK_ARGUMENT_PATH, validate_leak_argument_path
from .validation_sources import MAX_MEMORY_SNAPSHOT_BYTES

from .experiment_config import (
    AgentActivationSettings,
    AgentSettings,
    BackendSettings,
    DataSettings,
    EnvironmentSettings,
    ExperimentConfig,
    MemorySettings,
    OutputSettings,
    RunSettings,
    SimulationSettings,
    WebSettings,
    _integer,
    _mapping,
    _nonempty,
    _project_path,
    _reject_unknown,
    _section,
)


from .validation_connection import normalize_backend_connection


TARGET_MODES = {"none", "dataset", "custom", "mapping"}
DEFAULT_API_KEY_POSITION = "middle"


def apply_query_target_overrides(
    dataset: dict[str, Any], overrides_value: Any
) -> None:
    """Apply experiment-local targets without mutating a registered Query revision."""
    overrides = _mapping(overrides_value, "validation.target_overrides")
    queries = dataset.get("queries")
    if not isinstance(queries, list):
        raise ValueError("validation queries must contain a queries list")
    queries_by_id = {str(query.get("query_id")): query for query in queries}
    invalid_ids = [query_id for query_id in overrides if not isinstance(query_id, str)]
    if invalid_ids:
        raise ValueError("validation.target_overrides query IDs must be strings")
    unknown = sorted(set(overrides) - set(queries_by_id))
    if unknown:
        preview = ", ".join(unknown[:10])
        suffix = f" (and {len(unknown) - 10} more)" if len(unknown) > 10 else ""
        raise ValueError(
            "validation.target_overrides contains unknown query IDs: "
            f"{preview}{suffix}"
        )

    _remove_query_targets(dataset)
    for query_id, value in overrides.items():
        label = f"validation.target_overrides.{query_id}"
        target = _mapping(value, label)
        _reject_unknown(target, {"target_server", "target_tool"}, label)
        target_server = target.get("target_server")
        target_tool = target.get("target_tool")
        if not isinstance(target_server, str) or not target_server.strip():
            raise ValueError(f"{label}.target_server must be a non-empty string")
        if not isinstance(target_tool, str) or not target_tool.strip():
            raise ValueError(f"{label}.target_tool must be a non-empty string")
        target_server = target_server.strip()
        target_tool = target_tool.strip()
        query = queries_by_id[query_id]
        evaluation = query.setdefault("evaluation", {})
        evaluation["target_server"] = target_server
        evaluation["target_tool"] = target_tool
        evaluation["target_tool_fqn"] = f"{target_server}.{target_tool}"


def resolve_tool_selection_memory_settings(
    *,
    strategy: str,
    source_options: dict[str, Any],
    requested: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply read-time overrides while locking the source strategy and writes."""
    source = {"strategy": strategy, "options": copy.deepcopy(source_options)}
    if requested is None:
        return source
    if not isinstance(requested, dict) or set(requested) - {"strategy", "options"}:
        raise ValueError("second-stage memory settings must contain strategy and options only")
    if requested.get("strategy") != strategy:
        raise ValueError("second-stage memory strategy must match the frozen source")
    requested_options = requested.get("options", {})
    if not isinstance(requested_options, dict):
        raise ValueError("second-stage memory options must be an object")
    return {
        "strategy": strategy,
        "options": merge_retrieval_options(strategy, source_options, requested_options),
    }


def load_tool_selection_config(
    raw: dict[str, Any],
    *,
    config_path: Path,
    project_root: Path,
    run: RunSettings,
    backend: BackendSettings,
) -> ExperimentConfig:
    """Validate persisted inputs, including the copied source memory, without I/O."""
    environment = _section(raw, "environment", {"type"})
    if environment.get("type") != "tool_selection":
        raise ValueError("second-stage environment.type must be tool_selection")
    if run.run_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", run.run_id):
        raise ValueError("invalid second-stage run id")
    validation = _section(
        raw, "validation",
        {
            "source_experiment_id", "source_run_id", "source_agent_id",
            "source_round", "queries", "mcp", "memory_source", "target_mode", "backend_connection", "memory_suffix",
            "api_key_position", "custom_payload_enabled", "custom_payload",
            "expected_leak_value", "leak_argument_path",
            "memory_account_pool",
            "memory_source_mode", "specified_memory", "specified_agent_id", "injection_enabled",
            "mcp_parameter_additions",
        },
    )
    source_mode = validation.get("memory_source_mode", "run")
    if not isinstance(source_mode, str) or source_mode not in {"run", "specified"}:
        raise ValueError("validation.memory_source_mode must be run or specified")
    specified = source_mode == "specified"
    source_run_id = None if specified else _nonempty(validation.get("source_run_id"), "validation.source_run_id")
    account_pool = normalize_memory_account_pool(validation.get("memory_account_pool"))
    agent_id = (validation.get("specified_agent_id", "ROWAN") if specified
                else _nonempty(validation.get("source_agent_id"), "validation.source_agent_id"))
    source_round = 1 if specified else _integer(validation.get("source_round"), "validation.source_round", minimum=1)
    queries = _input_file(validation.get("queries"), "queries", normalize_query_dataset)
    mcp = _input_file(validation.get("mcp"), "mcp", normalize_mcp_dataset)
    mcp_parameter_additions = normalize_mcp_parameter_additions(
        validation.get("mcp_parameter_additions"), mcp["dataset"],
    )
    if mcp_parameter_additions:
        # Validate the effective catalog without replacing the frozen source file.
        apply_mcp_parameter_additions(mcp["dataset"], mcp_parameter_additions)
    injection_enabled = validation.get("injection_enabled", not specified)
    if type(injection_enabled) is not bool:
        raise ValueError("validation.injection_enabled must be boolean")
    target_mode = validation.get("target_mode", "custom")
    if not isinstance(target_mode, str) or target_mode not in TARGET_MODES:
        raise ValueError("validation.target_mode must be none, dataset, custom, or mapping")
    if target_mode == "none":
        _remove_query_targets(queries["dataset"])
    validate_selection_inputs(queries["dataset"], mcp["dataset"])
    if target_mode != "none":
        _require_query_targets(queries["dataset"])
    if "memory_suffix" in validation and not isinstance(validation["memory_suffix"], str):
        raise ValueError("validation.memory_suffix must be a string")
    api_key_position = validation.get("api_key_position", DEFAULT_API_KEY_POSITION)
    if not isinstance(api_key_position, str) or api_key_position not in API_KEY_POSITIONS:
        raise ValueError(
            "validation.api_key_position must be one of: "
            + ", ".join(sorted(API_KEY_POSITIONS))
        )
    custom_payload_enabled = validation.get("custom_payload_enabled", False)
    if not isinstance(custom_payload_enabled, bool):
        raise ValueError("validation.custom_payload_enabled must be true or false")
    custom_payload = validation.get("custom_payload", "")
    if not isinstance(custom_payload, str) or len(custom_payload) > 10000:
        raise ValueError("validation.custom_payload must be a string of at most 10000 characters")
    if custom_payload_enabled and not custom_payload.strip():
        raise ValueError("validation.custom_payload is required when custom payload is enabled")
    if custom_payload_enabled:
        expected_leak_value = validation.get("expected_leak_value")
        leak_argument_path = validation.get("leak_argument_path")
        if (not isinstance(expected_leak_value, str) or not expected_leak_value.strip()
                or len(expected_leak_value) > 1000):
            raise ValueError("validation.expected_leak_value must contain 1 to 1000 characters")
        if expected_leak_value not in custom_payload:
            raise ValueError("validation.expected_leak_value must appear in validation.custom_payload")
        validate_leak_argument_path(leak_argument_path)
    else:
        expected_leak_value = DEFAULT_EXACT_LEAK_VALUE
        leak_argument_path = DEFAULT_LEAK_ARGUMENT_PATH
    if specified:
        source = build_specified_memory_source(validation.get("specified_memory"), agent_id)
        require_scenario_memories(source["snapshot"]["specified_memory"], queries["dataset"]["queries"])
        if validation.get("memory_source") is not None and validation["memory_source"] != source:
            raise ValueError("frozen specified memory does not match the scenario inputs")
        if account_pool and account_pool["enabled"]:
            raise ValueError("specified memory cannot use cloud memory account pools")
        if validation.get("source_run_id") is not None or validation.get("source_experiment_id") is not None:
            raise ValueError("specified memory must not reference a first-stage experiment")
    else:
        if "specified_memory" in validation or "specified_agent_id" in validation:
            raise ValueError("specified memory inputs require memory_source_mode=specified")
        source = copy.deepcopy(_mapping(validation.get("memory_source"), "validation.memory_source"))
    if not source:
        raise ValueError("select a completed first-stage run and freeze its memory before launching")
    source_experiment_id = None if specified else _nonempty(
        validation.get("source_experiment_id", source.get("experiment_id")),
        "validation.source_experiment_id",
    )
    if (
        source.get("run_id") != source_run_id
        or source.get("experiment_id") != source_experiment_id
        or source.get("agent_id") != agent_id
        or source.get("round_index") != source_round
    ):
        raise ValueError("frozen memory identity does not match the selected source")
    profile = _mapping(source.get("profile"), "memory_source.profile")
    if profile.get("agent_id") != agent_id:
        raise ValueError("frozen profile agent does not match the selected agent")
    for field in ("display_name", "persona", "topic"):
        if not isinstance(profile.get(field), str):
            raise ValueError(f"memory_source.profile.{field} must be a string")
    snapshot = _mapping(source.get("snapshot"), "memory_source.snapshot")
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(serialized.encode("utf-8")) > MAX_MEMORY_SNAPSHOT_BYTES:
        raise ValueError(
            "selected memory snapshot must not exceed "
            f"{MAX_MEMORY_SNAPSHOT_BYTES // (1024 * 1024)} MiB "
            f"({MAX_MEMORY_SNAPSHOT_BYTES} bytes)"
        )
    if source.get("snapshot_sha256") != hashlib.sha256(serialized.encode("utf-8")).hexdigest():
        raise ValueError("frozen memory snapshot checksum does not match")
    strategy = _nonempty(source.get("memory_strategy"), "memory_source.memory_strategy")
    if account_pool and account_pool["enabled"] and strategy not in {"hindsight_cloud", "mem0_platform_v3", "memos_cloud"}:
        raise ValueError("memory account pools support Hindsight, Mem0 and MemOS only")
    options = _mapping(source.get("memory_options"), "memory_source.memory_options")
    if not specified:
        memory = create_memory(strategy, agent_id=agent_id, options=options)
        try:
            memory.restore(MemorySnapshot.from_dict(snapshot))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"source memory cannot be restored: {exc}") from exc

    agents = _section(raw, "agents", {"count"})
    if _integer(agents.get("count", 1), "agents.count", minimum=1) != 1:
        raise ValueError("second-stage validation supports exactly one agent")
    requested_memory = raw.get("memory")
    if requested_memory is not None:
        requested_memory = _section(raw, "memory", {"strategy", "options"})
    if specified:
        if requested_memory not in (None, {"strategy": SPECIFIED_MEMORY_STRATEGY, "options": {}}):
            raise ValueError("specified memory does not accept provider retrieval settings")
        effective_memory = {"strategy": SPECIFIED_MEMORY_STRATEGY, "options": {}}
    else:
        effective_memory = resolve_tool_selection_memory_settings(
            strategy=strategy, source_options=options, requested=requested_memory,
        )
    # Validate the effective read policy independently of the unchanged frozen
    # source restore above. Retrieval settings are not stored memory content.
    if not specified:
        create_memory(
            effective_memory["strategy"], agent_id=agent_id,
            options=effective_memory["options"],
        )
    count = len(queries["dataset"]["queries"])
    simulation = _section(raw, "simulation", {"rounds"})
    if _integer(simulation.get("rounds", count), "simulation.rounds", minimum=1) != count:
        raise ValueError("second-stage query count is determined by the query file")
    if raw.get("data"):
        raise ValueError("second-stage validation does not use a source database configuration")
    output = _section(raw, "output", {"root_dir"})
    output_root = _project_path(
        output.get("root_dir", "output/validation"), project_root, "output.root_dir",
    )
    allowed_output = (project_root / "output").resolve()
    if output_root != allowed_output and allowed_output not in output_root.parents:
        raise ValueError("second-stage output.root_dir must be inside the project output directory")
    if raw.get("web"):
        raise ValueError("second-stage validation is managed through the experiment platform")
    frozen = {
        "source_experiment_id": source_experiment_id,
        "source_run_id": source_run_id,
        "source_agent_id": agent_id,
        "source_round": source_round,
        "queries": queries,
        "mcp": mcp,
        "memory_source": source,
        "target_mode": target_mode,
        "api_key_position": api_key_position,
        "custom_payload_enabled": custom_payload_enabled,
        "custom_payload": custom_payload,
        "expected_leak_value": expected_leak_value,
        "leak_argument_path": leak_argument_path,
    }
    if specified:
        frozen.update(memory_source_mode="specified", specified_agent_id=agent_id,
                      specified_memory=copy.deepcopy(source["snapshot"]["specified_memory"]))
    if "mcp_parameter_additions" in validation:
        frozen["mcp_parameter_additions"] = copy.deepcopy(mcp_parameter_additions)
    if specified or "injection_enabled" in validation:
        frozen["injection_enabled"] = injection_enabled
    if "backend_connection" in validation:
        frozen["backend_connection"] = normalize_backend_connection(validation["backend_connection"])
    if account_pool is not None:
        frozen["memory_account_pool"] = account_pool
    if validation.get("memory_suffix"):
        # Appended verbatim to the retrieved memory text of every second-stage
        # query. Never evaluated as a template and never written back to memory.
        frozen["memory_suffix"] = validation["memory_suffix"]
    return ExperimentConfig(
        config_path=config_path, run=run, backend=backend,
        data=DataSettings(project_root / "data", project_root / "data/moltnet.sqlite", "unused", False),
        environment=EnvironmentSettings(
            kind="tool_selection", selection="memory", source_post_id=None,
            submolt=None, source_post_ids=(), post_count=None, submolts=(),
            min_comments=0, max_comments=None, feed_limit=1,
        ),
        agents=AgentSettings(1, False, False, run.seed, 1),
        memory=MemorySettings(
            effective_memory["strategy"],
            copy.deepcopy(effective_memory["options"]),
        ),
        simulation=SimulationSettings(
            count, AgentActivationSettings("all", None), 0, True, None, (),
        ),
        output=OutputSettings(output_root, False, False),
        web=WebSettings(False, "127.0.0.1", 8780, False, 1000),
        validation=frozen,
    )


def normalize_memory_account_pool(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("validation.memory_account_pool must be an object")
    allowed = {"enabled", "account_ids", "registry_db_path", "memory_set_id", "revision_hash", "shared"}
    _reject_unknown(value, allowed, "validation.memory_account_pool")
    enabled = value.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("memory_account_pool.enabled must be boolean")
    ids = value.get("account_ids", [])
    if (not isinstance(ids, list) or
            any(not isinstance(item, str) or not item.strip() or len(item) > 160 for item in ids)):
        raise ValueError("memory_account_pool.account_ids must contain non-empty account IDs")
    ids = [item.strip() for item in ids]
    if len(set(ids)) != len(ids):
        raise ValueError("memory account pool requires distinct account IDs")
    if any(account_id.startswith("env-") for account_id in ids):
        raise ValueError("The default account is export-only. Select an experiment account for stage two.")
    result = {"enabled": enabled, "account_ids": ids}
    if "shared" in value:
        if type(value["shared"]) is not bool:
            raise ValueError("memory_account_pool.shared must be boolean")
        result["shared"] = value["shared"]
    for name in ("registry_db_path", "memory_set_id", "revision_hash"):
        if name in value:
            result[name] = _nonempty(value[name], f"memory_account_pool.{name}")
    if bool(result.get("memory_set_id")) != bool(result.get("revision_hash")):
        raise ValueError("memory account pool must bind both memory_set_id and revision_hash")
    return result


def _require_query_targets(dataset: dict[str, Any]) -> None:
    """Require a user-selected Server/Tool target for every platform query."""
    missing = []
    for query in dataset["queries"]:
        evaluation = query.get("evaluation")
        if not isinstance(evaluation, dict) or not (
            isinstance(evaluation.get("target_server"), str)
            and evaluation["target_server"].strip()
            and isinstance(evaluation.get("target_tool"), str)
            and evaluation["target_tool"].strip()
        ):
            missing.append(query["query_id"])
    if missing:
        preview = ", ".join(missing[:10])
        suffix = f" (and {len(missing) - 10} more)" if len(missing) > 10 else ""
        raise ValueError(
            "Select a target Server and Tool for every second-stage query; "
            f"missing targets: {preview}{suffix}"
        )


def _remove_query_targets(dataset: dict[str, Any]) -> None:
    """Drop evaluator-only target fields when target scoring is disabled."""
    for query in dataset["queries"]:
        evaluation = query.get("evaluation")
        if not isinstance(evaluation, dict):
            continue
        for field in ("target_server", "target_tool", "target_tool_fqn"):
            evaluation.pop(field, None)
        if not evaluation:
            query.pop("evaluation", None)


def _input_file(value: Any, name: str, normalize: Any) -> dict[str, Any]:
    wrapper = _mapping(value, f"validation.{name}")
    _reject_unknown(wrapper, {"filename", "dataset"}, f"validation.{name}")
    filename = _nonempty(wrapper.get("filename"), f"validation.{name}.filename")
    if PurePath(filename).suffix.lower() != ".json":
        raise ValueError(f"validation.{name} must be a JSON file")
    return {"filename": filename, "dataset": normalize(wrapper.get("dataset"))}
