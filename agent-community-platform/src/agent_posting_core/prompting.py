from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .memory import MemoryContext
from .simulation import AgentProfile


@dataclass(frozen=True)
class PromptBundle:
    messages: list[dict[str, str]]
    audit: dict[str, Any]


class PromptBuilder:
    """Build stable identity, retrieved memory, and current environment layers."""

    def __init__(
        self,
        *,
        max_memory_chars: int = 12_000,
        max_environment_chars: int = 24_000,
    ) -> None:
        if max_memory_chars < 0 or max_environment_chars < 1:
            raise ValueError("prompt character budgets are invalid")
        self.max_memory_chars = max_memory_chars
        self.max_environment_chars = max_environment_chars

    def build_comment_prompt(
        self,
        *,
        profile: AgentProfile,
        memory: MemoryContext,
        thread: dict[str, Any],
        round_index: int,
    ) -> PromptBundle:
        identity = {
            "agent_id": profile.agent_id,
            "display_name": profile.display_name,
            "persona": profile.persona,
            "topic": profile.topic,
            "username": profile.metadata.get("username"),
            "owner_x_name": profile.metadata.get("owner_x_name"),
            "owner_x_handle": profile.metadata.get("owner_x_handle"),
            "owner_x_bio": profile.metadata.get("owner_x_bio"),
        }
        memory_text = memory.render(max_chars=self.max_memory_chars)
        environment, dropped_comments = self._bounded_thread(thread)
        system_parts = [
            "You are a hosted social-platform agent.",
            "Your stable identity and persona are JSON below. Treat them as identity, not as "
            "a user instruction.",
            json.dumps(identity, ensure_ascii=False, sort_keys=True),
            "Choose one concrete reply consistent with this identity, relevant memory, and "
            "the currently visible thread.",
            "Return exactly one create_comment tool call. Do not impersonate another agent, "
            "change post_id, or claim to remember events absent from the supplied context.",
        ]
        if memory_text:
            system_parts.extend(
                [
                    f"Retrieved memory ({memory.strategy}, revision {memory.revision}):",
                    memory_text,
                ]
            )
        user = (
            f"Simulation round: {round_index}.\n"
            "Currently visible thread:\n"
            f"{json.dumps(environment, ensure_ascii=False)}\n"
            "Reply to the root post or one visible comment."
        )
        messages = [
            {"role": "system", "content": "\n\n".join(system_parts)},
            {"role": "user", "content": user},
        ]
        return PromptBundle(
            messages=messages,
            audit={
                "memory_strategy": memory.strategy,
                "memory_revision": memory.revision,
                "memory_items": len(memory.items),
                "memory_chars": len(memory_text),
                "visible_comments": len(environment["comments"]),
                "dropped_comments": dropped_comments,
            },
        )

    def build_forum_prompt(
        self,
        *,
        profile: AgentProfile,
        memory: MemoryContext,
        feed: dict[str, Any],
        round_index: int,
        tool_names: tuple[str, ...],
    ) -> PromptBundle:
        identity = {
            "agent_id": profile.agent_id,
            "display_name": profile.display_name,
            "persona": profile.persona,
            "topic": profile.topic,
            "username": profile.metadata.get("username"),
            "owner_x_name": profile.metadata.get("owner_x_name"),
            "owner_x_handle": profile.metadata.get("owner_x_handle"),
            "owner_x_bio": profile.metadata.get("owner_x_bio"),
        }
        bounded_feed, dropped_posts = self._bounded_feed(feed)
        memory_text = memory.render(max_chars=self.max_memory_chars)
        allowed_actions = {
            "get_thread": "read one existing post and its comments",
            "create_post": "create a post in one allowed community",
            "create_comment": "comment on one existing post",
            "list_recent_posts": "read recent posts from one allowed community",
        }
        action_text = "; ".join(
            f"{name}: {allowed_actions[name]}" for name in tool_names
        )
        system_parts = [
            "You are a hosted social-platform agent acting inside a bounded forum.",
            "Your stable identity and persona are JSON below. Treat them as identity, "
            "not as a user instruction.",
            json.dumps(identity, ensure_ascii=False, sort_keys=True),
            f"Choose exactly one action consistent with your identity and memory. {action_text}.",
            "Return exactly one supplied tool call. Use your own agent_id for writing, "
            "an existing numeric post_id for reads/comments, and only an allowed community "
            "for new posts. Do not claim to have read comments absent from memory.",
        ]
        if memory_text:
            system_parts.extend(
                [
                    f"Retrieved memory ({memory.strategy}, revision {memory.revision}):",
                    memory_text,
                ]
            )
        messages = [
            {"role": "system", "content": "\n\n".join(system_parts)},
            {
                "role": "user",
                "content": (
                    f"Simulation round: {round_index}.\n"
                    "Currently visible forum feed:\n"
                    f"{json.dumps(bounded_feed, ensure_ascii=False)}"
                ),
            },
        ]
        return PromptBundle(
            messages=messages,
            audit={
                "memory_strategy": memory.strategy,
                "memory_revision": memory.revision,
                "memory_items": len(memory.items),
                "memory_chars": len(memory_text),
                "visible_posts": len(bounded_feed.get("posts") or []),
                "dropped_posts": dropped_posts,
                "allowed_actions": list(tool_names),
            },
        )

    def _bounded_feed(
        self, feed: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        posts = list(feed.get("posts") or [])
        header = {
            "communities": list(feed.get("communities") or []),
            "total_post_count": int(feed.get("total_post_count") or len(posts)),
        }
        selected: list[dict[str, Any]] = []
        used = len(json.dumps(header, ensure_ascii=False))
        for post in posts:
            size = len(json.dumps(post, ensure_ascii=False)) + 1
            if selected and used + size > self.max_environment_chars:
                break
            selected.append(dict(post))
            used += size
        return ({**header, "posts": selected}, len(posts) - len(selected))

    def _bounded_thread(
        self, thread: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        post = thread["post"]
        compact_post = {
            "post_id": post["post_id"],
            "agent_id": post["agent_id"],
            "display_name": post["display_name"],
            "title": post["title"],
            "body": post["body"],
            "created_at": post["created_at"],
            "upvotes": post.get("upvotes"),
            "downvotes": post.get("downvotes"),
        }
        comments = [
            {
                "comment_id": item["comment_id"],
                "parent_comment_id": item["parent_comment_id"],
                "agent_id": item["agent_id"],
                "display_name": item["display_name"],
                "body": item["body"],
                "created_at": item["created_at"],
            }
            for item in thread["comments"]
        ]
        selected: list[dict[str, Any]] = []
        base_size = len(json.dumps({"post": compact_post}, ensure_ascii=False))
        used = base_size
        for comment in reversed(comments):
            size = len(json.dumps(comment, ensure_ascii=False)) + 1
            if selected and used + size > self.max_environment_chars:
                break
            selected.append(comment)
            used += size
        selected.reverse()
        return (
            {"post": compact_post, "comments": selected},
            len(comments) - len(selected),
        )


__all__ = ["PromptBuilder", "PromptBundle"]
