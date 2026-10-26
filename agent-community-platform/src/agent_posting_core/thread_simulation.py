"""Thread and profile events for file-based, single-agent memory ingestion."""
from __future__ import annotations
import json
from collections.abc import Callable
from typing import Any
from .identity import usable_identity_text
from .memory import Memory, MemoryEvent
from .memory_content import memory_message_content
from .simulation import AgentProfile

MemoryFactory = Callable[[AgentProfile], Memory]


def thread_memory_events(
    thread: dict[str, Any], *, hide_speaker: bool = True, speaker_as_json: bool = False,
) -> list[MemoryEvent]:
    post = thread["post"]
    thread_id = str(post["post_id"])
    post_event_id = _post_event_id(post)
    events = [
        MemoryEvent(
            event_id=post_event_id,
            thread_id=thread_id,
            kind="post",
            actor_id=str(post["agent_id"]),
            content=memory_message_content(
                str(post["body"]) if str(post["body"]).strip() else str(post["title"]),
                str(post["agent_id"]), hide_speaker=hide_speaker, speaker_as_json=speaker_as_json,
            ),
            created_at=str(post["created_at"]),
            provenance=str(post.get("source") or "platform"),
            metadata={
                "platform_post_id": post["post_id"],
                "source_post_id": post.get("source_post_id"),
            },
        )
    ]
    comment_event_ids = {
        int(comment["comment_id"]): _comment_event_id(comment)
        for comment in thread["comments"]
    }
    for comment in thread["comments"]:
        parent_id = comment.get("parent_comment_id")
        events.append(
            MemoryEvent(
                event_id=comment_event_ids[int(comment["comment_id"])],
                thread_id=thread_id,
                kind="comment",
                actor_id=str(comment["agent_id"]),
                content=memory_message_content(
                    str(comment["body"]), str(comment["agent_id"]),
                    hide_speaker=hide_speaker, speaker_as_json=speaker_as_json,
                ),
                created_at=str(comment["created_at"]),
                parent_event_id=(
                    comment_event_ids.get(int(parent_id))
                    if parent_id is not None
                    else post_event_id
                ),
                provenance=str(comment.get("source") or "platform"),
                metadata={
                    "platform_comment_id": comment["comment_id"],
                    "source_comment_id": comment.get("source_comment_id"),
                },
            )
        )
    return events


def personalize_events(
    events: list[MemoryEvent], agent_id: str
) -> list[MemoryEvent]:
    return [
        MemoryEvent(
            **{
                **event.as_dict(),
                "visibility": (
                    "authored" if event.actor_id == agent_id else "inferred_visible"
                ),
            }
        )
        for event in events
    ]


def profile_seed_events(
    profile: AgentProfile,
    environment_events: list[MemoryEvent],
    *,
    thread_id: str,
    hide_speaker: bool = True, speaker_as_json: bool = False,
) -> list[MemoryEvent]:
    authored = [
        event
        for event in personalize_events(environment_events, profile.agent_id)
        if event.actor_id == profile.agent_id
    ]
    profile_history = profile.metadata.get("description_history") or []
    facts: list[MemoryEvent] = []
    for index, item in enumerate(profile_history):
        if isinstance(item, dict):
            content = usable_identity_text(
                item.get("value") or item.get("description")
            )
            created_at = str(
                item.get("created_at") or item.get("timestamp") or ""
            )
        else:
            content = usable_identity_text(item)
            created_at = ""
        if content:
            facts.append(
                MemoryEvent(
                    event_id=f"profile:{profile.agent_id}:{index + 1}",
                    thread_id=thread_id,
                    kind="profile_fact",
                    actor_id=profile.agent_id,
                    content=content,
                    created_at=created_at,
                    provenance=str(profile.metadata.get("source") or "platform"),
                    visibility="authored",
                    metadata={"seed_scope": "identity_history"},
                )
            )
    history_events = [
        event
        for item in profile.metadata.get("initial_history") or []
        if isinstance(item, dict)
        for event in [_history_seed_event(
            profile, item, thread_id=thread_id, hide_speaker=hide_speaker, speaker_as_json=speaker_as_json,
        )]
        if event is not None
    ]
    identity = {
        "agent_id": profile.agent_id,
        "display_name": profile.display_name,
        "persona": profile.persona,
        "username": profile.metadata.get("username"),
        "owner_x_name": profile.metadata.get("owner_x_name"),
        "owner_x_handle": profile.metadata.get("owner_x_handle"),
        "owner_x_bio": profile.metadata.get("owner_x_bio"),
    }
    identity_event = MemoryEvent(
        event_id=f"profile:{profile.agent_id}:identity",
        thread_id=thread_id,
        kind="profile_identity",
        actor_id=profile.agent_id,
        content=json.dumps(identity, ensure_ascii=False, sort_keys=True),
        created_at=_latest_profile_timestamp(profile_history),
        provenance=str(profile.metadata.get("source") or "platform"),
        visibility="authored",
        metadata={"seed_scope": "current_identity", "pinned_identity": True},
    )
    result: list[MemoryEvent] = []
    seen: set[str] = set()
    for event in [*facts, *history_events, *authored, identity_event]:
        if event.event_id not in seen:
            result.append(event)
            seen.add(event.event_id)
    return result


def _history_seed_event(
    profile: AgentProfile,
    item: dict[str, Any],
    *,
    thread_id: str,
    hide_speaker: bool = True, speaker_as_json: bool = False,
) -> MemoryEvent | None:
    kind = str(item.get("kind") or "").strip().lower()
    if kind not in {"post", "comment"}:
        return None
    source_id = str(item.get("source_id") or "").strip()
    if not source_id:
        return None
    body = str(item.get("content") or "")
    title = str(item.get("title") or "")
    content = body if body.strip() else (title if kind == "post" else "")
    if not content.strip():
        return None
    source = str(profile.metadata.get("source") or "platform")
    source_post_id = str(item.get("post_id") or "").strip()
    history_thread_id = (
        f"{source}:{source_post_id}" if source_post_id else thread_id
    )
    parent_id = str(item.get("parent_id") or "").strip()
    parent_event_id = None
    if kind == "comment":
        if parent_id:
            parent_event_id = f"{source}:comment:{parent_id}"
        elif source_post_id:
            parent_event_id = f"{source}:post:{source_post_id}"
    return MemoryEvent(
        event_id=f"{source}:{kind}:{source_id}",
        thread_id=history_thread_id,
        kind=kind,
        actor_id=profile.agent_id,
        content=memory_message_content(content, profile.agent_id, hide_speaker=hide_speaker, speaker_as_json=speaker_as_json),
        created_at=str(item.get("created_at") or ""),
        parent_event_id=parent_event_id,
        provenance=source,
        visibility="authored",
        metadata={
            "seed_scope": "personal_history",
            "source_id": source_id,
            "source_post_id": source_post_id or None,
        },
    )


def _latest_profile_timestamp(history: Any) -> str:
    if not isinstance(history, list):
        return ""
    for item in reversed(history):
        if isinstance(item, dict):
            value = item.get("created_at") or item.get("timestamp")
            if value:
                return str(value)
    return ""


def comment_memory_event(
    comment: dict[str, Any], *, visibility: str, hide_speaker: bool = True, speaker_as_json: bool = False,
) -> MemoryEvent:
    return MemoryEvent(
        event_id=_comment_event_id(comment),
        thread_id=str(comment["post_id"]),
        kind="comment",
        actor_id=str(comment["agent_id"]),
        content=memory_message_content(
            str(comment["body"]), str(comment["agent_id"]), hide_speaker=hide_speaker, speaker_as_json=speaker_as_json,
        ),
        created_at=str(comment["created_at"]),
        parent_event_id=(
            f"platform:comment:{comment['parent_comment_id']}"
            if comment.get("parent_comment_id") is not None
            else f"platform:post:{comment['post_id']}"
        ),
        provenance=str(comment.get("source") or "platform"),
        visibility=visibility,
        metadata={"platform_comment_id": comment["comment_id"]},
    )


def _post_event_id(post: dict[str, Any]) -> str:
    if post.get("source") and post.get("source_post_id"):
        return f"{post['source']}:post:{post['source_post_id']}"
    return f"platform:post:{post['post_id']}"


def _comment_event_id(comment: dict[str, Any]) -> str:
    if comment.get("source") and comment.get("source_comment_id"):
        return f"{comment['source']}:comment:{comment['source_comment_id']}"
    return f"platform:comment:{comment['comment_id']}"


__all__ = [
    "MemoryFactory",
    "comment_memory_event",
    "personalize_events",
    "profile_seed_events",
    "thread_memory_events",
]
