"""Read-only forum snapshots for uploaded post collections."""
from __future__ import annotations
from typing import Any
from .simulation import ForumStore


def forum_snapshot(
    store: ForumStore,
    communities: tuple[str, ...],
    *,
    post_limit: int,
    comments_per_post: int = 200,
) -> dict[str, Any]:
    posts = store.list_forum_posts(communities, limit=post_limit)
    rendered: list[dict[str, Any]] = []
    for post in posts:
        comments = store.list_comments(int(post["post_id"]))
        visible_comments = comments[-comments_per_post:]
        rendered.append(
            {
                **post,
                "comment_count": len(comments),
                "comments": visible_comments,
                "truncated_comments": len(comments) - len(visible_comments),
            }
        )
    counts = store.forum_counts(communities)
    return {
        "communities": list(communities),
        "posts": rendered,
        "total_post_count": counts["posts"],
        "total_comment_count": counts["comments"],
        "truncated_posts": max(0, counts["posts"] - len(rendered)),
    }


def forum_feed(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "communities": list(snapshot.get("communities") or []),
        "total_post_count": int(snapshot.get("total_post_count") or 0),
        "posts": [
            {
                key: value
                for key, value in post.items()
                if key not in {"comments", "truncated_comments"}
            }
            for post in snapshot.get("posts") or []
        ],
    }


__all__ = ["forum_feed", "forum_snapshot"]
