from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import CALL_OUTPUT_DIR, ROUND_OUTPUT_DIR, ensure_workspace_dirs


def save_round_record(record: dict[str, Any]) -> Path:
    ensure_workspace_dirs()
    round_index = int(record["round_index"])
    path = ROUND_OUTPUT_DIR / f"round-{round_index:03d}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_tool_call_record(call_id: str, payload: dict[str, Any]) -> Path:
    ensure_workspace_dirs()
    safe_call_id = call_id.replace("/", "_").replace("\\", "_")
    path = CALL_OUTPUT_DIR / f"{safe_call_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
