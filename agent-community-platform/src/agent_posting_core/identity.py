from __future__ import annotations

import json
from typing import Any


_MISSING_IDENTITY_VALUES = {
    "",
    "-",
    "(none)",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
}


def usable_identity_text(value: Any) -> str:
    """Return normalized identity text, excluding common dataset placeholders."""
    text = "" if value is None else str(value).strip()
    if text.casefold() in _MISSING_IDENTITY_VALUES:
        return ""
    return text


def identity_description(owner_x_bio: Any, raw_history: Any) -> str:
    """Resolve the latest usable agent description with owner bio as fallback."""
    history = raw_history
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except json.JSONDecodeError:
            history = [history]
    if not isinstance(history, list):
        history = [history] if history is not None else []
    for item in reversed(history):
        if isinstance(item, dict):
            candidate = item.get("value") or item.get("description")
        else:
            candidate = item
        description = usable_identity_text(candidate)
        if description:
            return description
    return usable_identity_text(owner_x_bio)


__all__ = ["identity_description", "usable_identity_text"]
