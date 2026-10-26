"""Validate and restore self-contained post files, without a source database."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import PurePath
from typing import Any

from .identity import usable_identity_text
from .moltnet import _latest_vote_snapshot
from .simulation import AgentProfile, ForumStore, HistoricalComment, HistoricalPost


MAX_SELECTION_FILE_BYTES = 1024 * 1024
MAX_SELECTED_POSTS = 500


def parse_post_selection_file(
    filename: str, content: str, *, environment_type: str = "moltnet_forum"
) -> dict[str, Any]:
    """Parse complete posts and author profiles; ID-only files are not accepted."""
    if not isinstance(filename, str) or not isinstance(content, str):
        raise ValueError("filename and content must be strings")
    if (
        len(content.encode("utf-8")) > MAX_SELECTION_FILE_BYTES
    ):
        raise ValueError("Post file must not exceed 1 MiB")
    content = content.lstrip("\ufeff").strip()
    if not content:
        raise ValueError("Post file is empty")
    try:
        extension = PurePath(filename).suffix.lower()
        if extension == ".json":
            value = json.loads(content)
        elif extension == ".jsonl":
            value = []
            for line_number, line in enumerate(content.splitlines(), start=1):
                if line.strip():
                    try:
                        value.append(json.loads(line))
                    except ValueError as exc:
                        raise ValueError(f"Invalid JSONL line {line_number}: {exc}") from exc
        else:
            raise ValueError("Complete post files must be .json or .jsonl, not ID lists")
        dataset = normalize_post_dataset(value, environment_type=environment_type)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Invalid or excessively nested post file") from exc
    return {
        "file": {"filename": filename, "dataset": dataset},
        "count": len(dataset["posts"]),
        "comment_count": sum(len(post["comments"]) for post in dataset["posts"]),
        "agent_count": len(dataset["agents"]),
        "identity_agent_count": sum(
            bool(usable_identity_text(a["persona"])) for a in dataset["agents"]
        ),
    }


def normalize_post_dataset(value: Any, *, environment_type: str) -> dict[str, Any]:
    """Canonicalize JSON data at both upload and config-load boundaries."""
    if environment_type not in {"moltnet_forum", "moltnet_thread"}:
        raise ValueError("Unknown environment type")
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if (
            len(serialized.encode("utf-8")) > 2 * MAX_SELECTION_FILE_BYTES
        ):
            raise ValueError("Embedded post data must not exceed 2 MiB")
    except (TypeError, RecursionError, UnicodeError) as exc:
        raise ValueError("Post data must contain only JSON values") from exc
    separate_comments: list[Any] = []
    if isinstance(value, list):
        posts, raw_agents = value, []
    elif isinstance(value, dict) and "posts" in value:
        unknown = set(value) - {"posts", "agents", "comments"}
        if unknown:
            raise ValueError("Unknown dataset fields: " + ", ".join(sorted(unknown)))
        posts, raw_agents = value["posts"], value.get("agents", [])
        separate_comments = value.get("comments", [])
    elif isinstance(value, dict) and ("body" in value or "content" in value):
        posts, raw_agents = [value], []
    else:
        raise ValueError("File must contain complete posts and author profiles, not just post IDs")
    if not isinstance(posts, list) or not posts:
        raise ValueError("posts must be a nonempty array of complete post objects")
    if len(posts) > MAX_SELECTED_POSTS:
        raise ValueError("posts must contain 1 to 500 complete post objects")
    if environment_type == "moltnet_thread" and len(posts) != 1:
        raise ValueError("Single-post mode requires exactly one complete post")
    if not isinstance(raw_agents, list):
        raise ValueError("agents must be an array of author profiles")
    if not isinstance(separate_comments, list):
        raise ValueError("comments must be an array")
    comments_by_post: dict[str, list[Any]] = {}
    for comment in separate_comments:
        comment = _object(comment, "comment")
        parent_post = _identifier(comment.get("post_id"), "comment.post_id")
        comments_by_post.setdefault(parent_post, []).append(comment)

    agents: dict[str, dict[str, Any]] = {}

    def add_agent(raw: Any) -> str:
        raw = _object(raw, "author profile")
        agent_id = _identifier(raw.get("id", raw.get("agent_id")), "author.id")
        profile = {
            "id": agent_id,
            "name": _text(raw.get("name", raw.get("display_name")), "author.name"),
            "persona": _text(
                raw.get("persona", raw.get("description", "")), "author.persona", empty=True
            ),
            "topic": _text(raw.get("topic", ""), "author.topic", empty=True),
            "metadata": _metadata(
                raw, {"id", "agent_id", "name", "display_name", "persona", "description", "topic"}
            ),
        }
        if agent_id in agents and agents[agent_id] != profile:
            raise ValueError(f"Conflicting author profiles for {agent_id}")
        agents[agent_id] = profile
        return agent_id

    def author_id(raw: dict[str, Any], label: str) -> str:
        reference = raw.get("author_id", raw.get("agent_id"))
        if "author" in raw:
            inline_id = add_agent(raw["author"])
            if reference is not None and _identifier(reference, label) != inline_id:
                raise ValueError(f"{label} conflicts with author.id")
            return inline_id
        return _identifier(reference, label)

    for raw in raw_agents:
        add_agent(raw)
    normalized = []
    post_ids: set[str] = set()
    comment_ids: set[str] = set()
    for index, raw_post in enumerate(posts, start=1):
        raw = _object(raw_post, f"post {index}")
        post_id = _identifier(raw.get("id", raw.get("source_post_id")), f"post {index}.id")
        if post_id in post_ids:
            raise ValueError(f"Duplicate post id: {post_id}")
        post_ids.add(post_id)
        vote_snapshot = _latest_vote_snapshot(raw.get("votes_history"))
        post = {
            "id": post_id,
            "author_id": author_id(raw, f"post {post_id}.author_id"),
            "title": _text(raw.get("title"), f"post {post_id}.title", empty=True),
            "body": _text(raw.get("body", raw.get("content")), f"post {post_id}.body", empty=True),
            "community": _text(
                raw.get("community", raw.get("submolt_name")), f"post {post_id}.community"
            ),
            "created_at": _timestamp(raw.get("created_at"), f"post {post_id}.created_at"),
            "upvotes": _votes(
                raw.get("upvotes", vote_snapshot.get("upvotes")), f"post {post_id}.upvotes"
            ),
            "downvotes": _votes(
                raw.get("downvotes", vote_snapshot.get("downvotes")), f"post {post_id}.downvotes"
            ),
            "metadata": _metadata(
                raw,
                {
                    "id",
                    "source_post_id",
                    "author",
                    "author_id",
                    "agent_id",
                    "title",
                    "body",
                    "content",
                    "community",
                    "submolt_name",
                    "created_at",
                    "upvotes",
                    "downvotes",
                    "comments",
                    "comment_count",
                },
            ),
            "comments": [],
        }
        if not post["title"].strip() and not post["body"].strip():
            raise ValueError(f"Post {post_id} has no title or body")
        comments = raw.get("comments", [])
        if not isinstance(comments, list):
            raise ValueError(f"post {post_id}.comments must be an array")
        comments = comments + comments_by_post.get(post_id, [])
        if "comment_count" in raw and (
            type(raw["comment_count"]) is not int or raw["comment_count"] != len(comments)
        ):
            raise ValueError(
                f"Post {post_id}: comment_count does not match the complete comments array"
            )
        for item in comments:
            item = _object(item, f"post {post_id} comment")
            if "post_id" in item and _identifier(item["post_id"], "comment.post_id") != post_id:
                raise ValueError(f"Comment post_id conflicts with containing post {post_id}")
            comment_id = _identifier(item.get("id", item.get("source_comment_id")), "comment.id")
            if comment_id in comment_ids:
                raise ValueError(f"Duplicate comment id: {comment_id}")
            comment_ids.add(comment_id)
            parent = item.get("parent_id", item.get("parent_source_comment_id"))
            post["comments"].append(
                {
                    "id": comment_id,
                    "author_id": author_id(item, f"comment {comment_id}.author_id"),
                    "body": _text(
                        item.get("body", item.get("content")), f"comment {comment_id}.body"
                    ),
                    "created_at": _timestamp(
                        item.get("created_at"), f"comment {comment_id}.created_at"
                    ),
                    "parent_id": None
                    if parent is None
                    else _identifier(parent, "comment.parent_id"),
                    "metadata": _metadata(
                        item,
                        {
                            "id",
                            "source_comment_id",
                            "author",
                            "author_id",
                            "agent_id",
                            "body",
                            "content",
                            "created_at",
                            "parent_id",
                            "parent_source_comment_id",
                        },
                    ),
                }
            )
        _validate_parents(post["comments"])
        normalized.append(post)
    if set(comments_by_post) - post_ids:
        raise ValueError("Comments reference posts that are missing from the file")
    for post in normalized:
        for item in [post, *post["comments"]]:
            if item["author_id"] not in agents:
                raise ValueError(
                    f"Author {item['author_id']} is missing from the file; add an agents profile or inline author"
                )
    return {"posts": normalized, "agents": list(agents.values())}


def select_file_reader(
    dataset: dict[str, Any], *, agent_id: str, require_identity: bool
) -> list[str]:
    """Resolve one explicitly requested file author, without cohort sampling."""
    profiles = {agent["id"]: agent for agent in dataset["agents"]}
    source_id = agent_id if agent_id in profiles else agent_id.removeprefix("file:")
    profile = profiles.get(source_id)
    if profile is None:
        raise ValueError(f"Unknown file reader agent_id: {agent_id}")
    if require_identity and not usable_identity_text(profile["persona"]):
        raise ValueError(f"Reader {agent_id} has no usable identity")
    return [source_id]


def import_post_dataset(
    store: ForumStore,
    dataset: dict[str, Any],
    *,
    selected_ids: list[str],
    history_limit: int,
) -> dict[str, Any]:
    """Write supplied content to the run store; never open a source DB."""
    existing = {agent["agent_id"]: agent for agent in store.list_agents()}
    # Resume partial imports using their persisted IDs, including legacy runs.
    # New authors use the exact source ID; never manufacture a source prefix.
    imported_ids = {
        agent["metadata"]["source_agent_id"]: agent["agent_id"]
        for agent in existing.values()
        if agent["metadata"].get("source") == "file"
        and agent["metadata"].get("source_agent_id") is not None
    }
    histories: dict[str, list[dict[str, Any]]] = {a["id"]: [] for a in dataset["agents"]}
    for post in dataset["posts"]:
        for kind, item in [("post", post), *(("comment", c) for c in post["comments"])]:
            histories[item["author_id"]].append(
                {
                    "kind": kind,
                    "source_id": item["id"],
                    "post_id": post["id"],
                    "title": item.get("title", ""),
                    "content": item["body"],
                    "created_at": item["created_at"],
                    "parent_id": item.get("parent_id"),
                }
            )
    profiles = {}
    for agent in dataset["agents"]:
        imported_id = imported_ids.get(agent["id"], agent["id"])
        occupied = existing.get(imported_id)
        if occupied is not None and (
            occupied["metadata"].get("source") != "file"
            or occupied["metadata"].get("source_agent_id") != agent["id"]
        ):
            raise ValueError(f"Imported author {agent['id']} conflicts with an existing agent_id")
        history = sorted(
            histories[agent["id"]], key=lambda item: (item["created_at"], item["source_id"])
        )
        loaded = history[-history_limit:]
        profiles[agent["id"]] = AgentProfile(
            agent_id=imported_id,
            display_name=agent["name"],
            persona=agent["persona"],
            topic=agent["topic"] or dataset["posts"][0]["title"],
            metadata={
                **agent["metadata"],
                "source": "file",
                "source_agent_id": agent["id"],
                "description": agent["persona"],
                "username": agent["name"],
                "initial_history": loaded,
                "initial_history_summary": {
                    "post_count": sum(h["kind"] == "post" for h in history),
                    "comment_count": sum(h["kind"] == "comment" for h in history),
                    "total_count": len(history),
                    "loaded_count": len(loaded),
                    "limit": history_limit,
                    "truncated": len(loaded) < len(history),
                },
            },
        )
    imports = []
    for post in dataset["posts"]:
        participants = list(
            dict.fromkeys([post["author_id"], *(c["author_id"] for c in post["comments"])])
        )
        restored = store.import_thread(
            [profiles[a] for a in participants],
            HistoricalPost(
                source="file",
                source_post_id=post["id"],
                agent_id=profiles[post["author_id"]].agent_id,
                community=post["community"],
                title=post["title"],
                body=post["body"],
                created_at=post["created_at"],
                upvotes=post["upvotes"],
                downvotes=post["downvotes"],
                metadata=post["metadata"],
            ),
            [
                HistoricalComment(
                    source_comment_id=c["id"],
                    agent_id=profiles[c["author_id"]].agent_id,
                    body=c["body"],
                    created_at=c["created_at"],
                    parent_source_comment_id=c["parent_id"],
                    metadata=c["metadata"],
                )
                for c in post["comments"]
            ],
        )
        imports.append({**restored, "community": post["community"]})
    records = []
    for index, agent_id in enumerate(selected_ids, start=1):
        profile = profiles[agent_id]
        store.upsert_agent(profile)
        records.append(
            {
                "order": index,
                "agent_id": profile.agent_id,
                "source_agent_id": agent_id,
                "display_name": profile.display_name,
                "persona": profile.persona,
                "selection_source": "file",
                "has_identity": bool(usable_identity_text(profile.persona)),
                "initial_history_loaded": len(profile.metadata["initial_history"]),
                "initial_history_summary": profile.metadata["initial_history_summary"],
            }
        )
    return {"imports": imports, "agents": {"selected_count": len(records), "agents": records}}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a complete object")
    return value


def _text(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(
            f"{label} must be {'a string' if empty else 'a nonempty string'} in the file"
        )
    return value


def _identifier(value: Any, label: str) -> str:
    if type(value) is int and value >= 0:
        value = str(value)
    value = _text(value, label)
    if len(value) > 256 or any(c.isspace() or c == "\x00" for c in value):
        raise ValueError(f"Invalid {label}")
    return value


def _timestamp(value: Any, label: str) -> str:
    value = _text(value, label)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 date/time") from exc
    return value


def _votes(value: Any, label: str) -> int | None:
    if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
        raise ValueError(f"{label} must be a nonnegative integer or null")
    return value


def _metadata(raw: dict[str, Any], known: set[str]) -> dict[str, Any]:
    metadata = dict(_object(raw.get("metadata", {}), "metadata"))
    extras = {k: v for k, v in raw.items() if k not in known and k != "metadata"}
    if extras:
        metadata["file_fields"] = {
            **_object(metadata.get("file_fields", {}), "metadata.file_fields"),
            **extras,
        }
    return metadata


def _validate_parents(comments: list[dict[str, Any]]) -> None:
    parents = {c["id"]: c["parent_id"] for c in comments}
    checked = set()
    for comment_id in parents:
        path = set()
        current = comment_id
        while current is not None and current not in checked:
            if current not in parents:
                raise ValueError(f"Missing parent comment in the same post: {current}")
            if current in path:
                raise ValueError(f"Comment reply cycle at {current}")
            path.add(current)
            current = parents[current]
        checked.update(path)
