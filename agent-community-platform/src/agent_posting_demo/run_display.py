"""Small, cached display metadata from frozen run configurations."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from agent_posting_core.memory import DEFAULT_MEMORY_STRATEGY


def config_display(config: dict[str, Any]) -> dict[str, Any]:
    def section(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    environment = section(config.get("environment"))
    validation = section(config.get("validation"))
    source = section(validation.get("memory_source"))
    backend = section(config.get("backend"))
    filename = section(environment.get("file")).get("filename")
    strategy = section(config.get("memory")).get("strategy")
    if environment.get("type") == "tool_selection":
        strategy = source.get("memory_strategy") or strategy
    elif environment:
        strategy = strategy or DEFAULT_MEMORY_STRATEGY
    hidden = environment.get("hide_speaker", True) if environment else None
    as_json = environment.get("speaker_as_json", False) if environment else None
    return {
        "post_filename": filename.replace("\\", "/").rsplit("/", 1)[-1]
        if isinstance(filename, str) and filename else None,
        "post_source": "file" if environment.get("file") else
        "database" if environment.get("type") in {"moltnet_thread", "moltnet_forum"}
        else None,
        "memory_strategy": strategy if isinstance(strategy, str) else None,
        "hide_speaker": hidden if type(hidden) is bool else None,
        "speaker_as_json": as_json if type(as_json) is bool else None,
        "source_run_id": source.get("run_id") or validation.get("source_run_id"),
        "behavior_model": backend.get("model")
        if isinstance(backend.get("model"), str) else None,
    }


@lru_cache(maxsize=512)
def _read_display(path: str, mtime_ns: int, size: int) -> dict[str, Any]:
    # Cache only these few fields, not post bodies, snapshots or credentials.
    text = Path(path).read_text(encoding="utf-8")
    config = json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
    if not isinstance(config, dict):
        raise ValueError("run configuration must be an object")
    return config_display(config)


def read_run_display(run: dict[str, Any]) -> dict[str, Any]:
    output = Path(run["output_dir"])
    paths = [output / "config.resolved.json", output / "config.yaml"]
    if run.get("worker_config_path"):
        paths.append(Path(run["worker_config_path"]))
    for path in paths:
        try:
            stat = path.stat()
            return dict(_read_display(str(path), stat.st_mtime_ns, stat.st_size))
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            continue
    return config_display({})
