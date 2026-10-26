"""Small, single-input harness for testing first-stage memory ingestion.

It deliberately exposes the provider's post-observe snapshot rather than trying
to normalize provider-specific memory records.  This makes attribution tests
able to inspect the exact text each provider retained.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from .memory import MemoryEvent, create_memory


async def observe_text(
    text: str,
    *,
    strategy: str,
    agent_id: str = "memory-test-agent",
    actor_id: str = "external-speaker",
    thread_id: str = "memory-test-thread",
    provenance: str = "memory-observation-test",
    visibility: str = "observed_read",
    created_at: str = "",
    options: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    llm: Any = None,
) -> dict[str, Any]:
    """Observe exactly one agent-visible text and return its retained form.

    ``saved`` means the provider accepted the event and the resulting snapshot
    contains it.  ``memories`` is intentionally provider-shaped; callers can
    inspect ``content``, ``context``, ``memory`` or ``attributed_to`` where a
    provider exposes those fields.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    event = MemoryEvent(
        event_id=f"memory-observation:{uuid4()}",
        thread_id=thread_id,
        kind="text",
        actor_id=actor_id,
        content=text,
        created_at=created_at,
        provenance=provenance,
        visibility=visibility,
    )
    memory = create_memory(
        strategy, agent_id=agent_id, llm=llm, options=options, runtime=runtime
    )
    await memory.observe([event])
    snapshot = memory.snapshot().as_dict()
    return {
        "strategy": snapshot["strategy"],
        "input": event.as_dict(),
        "saved": any(
            item.get("event_id") == event.event_id
            for item in snapshot.get("events", [])
        ),
        "revision": snapshot["revision"],
        "snapshot": snapshot,
        "memories": _extract_memories(snapshot),
    }


def _extract_memories(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    options = snapshot.get("options") or {}
    if isinstance(options.get("memories"), list):
        return options["memories"]
    if isinstance(options.get("records"), list):
        return options["records"]
    if isinstance(options.get("facts"), list):
        return options["facts"]
    return list(snapshot.get("events") or [])


__all__ = ["observe_text"]
