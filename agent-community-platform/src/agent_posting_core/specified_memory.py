"""Frozen, user-authored memory selected by a query's functional group.

This is a direct experimental input, never a provider write or a retrieval.
Text is preserved verbatim, including explicitly empty memory controls.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

STRATEGY = "specified_memory"
UNGROUPED = "__ungrouped__"


def normalize_specified_memory(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value or len(value) > 200:
        raise ValueError("specified_memory must be an object with 1 to 200 scenario entries")
    result = {}
    for group, content in value.items():
        if (not isinstance(group, str) or not group.strip() or group != group.strip()
                or len(group) > 160 or any(ord(char) < 32 for char in group)):
            raise ValueError("specified_memory scenario keys must be non-empty functional_group values")
        if not isinstance(content, str) or len(content) > 200000:
            raise ValueError(f"specified_memory.{group} must be a string of at most 200000 characters")
        result[group] = content
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 8 * 1024**2:
        raise ValueError("specified memory must not exceed 8 MiB")
    return result


def query_memory_group(query: dict[str, Any]) -> str:
    return query.get("functional_group") or UNGROUPED


def require_scenario_memories(memories: dict[str, str], queries: list[dict[str, Any]]) -> None:
    missing = sorted({query_memory_group(query) for query in queries} - set(memories))
    if missing:
        raise ValueError("Missing specified memory for scenarios: " + ", ".join(missing))


def build_specified_memory_source(value: Any, agent_id: Any = "ROWAN") -> dict[str, Any]:
    memories = normalize_specified_memory(value)
    if not isinstance(agent_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", agent_id):
        raise ValueError("specified_agent_id must be a valid agent identifier")
    snapshot = {"strategy": STRATEGY, "revision": 1, "events": [], "options": {},
                "specified_memory": copy.deepcopy(memories)}
    checksum = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return {
        "kind": STRATEGY, "experiment_id": None, "run_id": None,
        "agent_id": agent_id, "round_index": 1,
        "memory_strategy": STRATEGY, "memory_options": {},
        "profile": {"agent_id": agent_id, "display_name": agent_id,
                    "persona": "An assistant using the supplied memory as reference.",
                    "topic": "", "metadata": {"memory_source_mode": "specified"}},
        "snapshot": snapshot, "snapshot_sha256": checksum,
    }


def render_specified_memory(snapshot: dict[str, Any], query: dict[str, Any], agent_id: str) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("strategy") != STRATEGY:
        raise ValueError("invalid specified memory snapshot")
    memories = normalize_specified_memory(snapshot.get("specified_memory"))
    require_scenario_memories(memories, [query])
    group = query_memory_group(query)
    text = memories[group]
    item = {"event_id": f"specified:{group}", "thread_id": group,
            "kind": STRATEGY, "actor_id": agent_id, "content": text,
            "created_at": "", "parent_event_id": None, "provenance": "user_specified",
            "visibility": "specified", "metadata": {"functional_group": group}}
    return {
        "query_id": query["query_id"], "query": query["query"],
        "memory_strategy": STRATEGY, "memory_revision": snapshot.get("revision", 1),
        "retrieved_item_count": 0, "rendered_character_count": len(text),
        "rendered_memory": text, "delivered_items": [item] if text else [],
        "retrieval_audit": {"mode": "specified", "retrieval_performed": False,
                            "functional_group": group, "explicit_empty_memory": text == ""},
    }
