"""Observed vote-count normalization shared by uploaded post datasets."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any


def _latest_vote_snapshot(value: Any) -> dict[str, Any]:
    """Use the latest observed counts, never a sum or a reconstruction from score.

    Missing counts stay unknown. Timestamps take precedence over array order;
    undated records are a fallback, and ties use the last record in the array.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    records = value if isinstance(value, list) else [value]
    earliest = datetime.min.replace(tzinfo=timezone.utc)
    latest_key = (False, earliest, -1)
    latest: dict[str, Any] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        upvotes = _vote_count(record.get("upvotes"))
        downvotes = _vote_count(record.get("downvotes"))
        if upvotes is None and downvotes is None:
            continue
        timestamp = _text(record.get("timestamp")).strip()
        try:
            observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            observed_at = observed_at.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            observed_at = None
        key = (observed_at is not None, observed_at or earliest, index)
        if key > latest_key:
            latest_key = key
            latest = {
                "upvotes": upvotes,
                "downvotes": downvotes,
                "timestamp": timestamp or None,
                "source": record.get("source"),
            }
    return latest


def _vote_count(value: Any) -> int | None:
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return None
    # SQLite INTEGER is signed 64-bit. Reject booleans and fractional counts.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 2**63 - 1 else None


def _text(value: Any) -> str:
    return "" if value is None else str(value)
