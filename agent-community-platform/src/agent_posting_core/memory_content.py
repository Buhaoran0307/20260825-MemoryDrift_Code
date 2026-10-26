"""Format source text at the boundary where forum records become memory events."""

from __future__ import annotations

import json


def memory_message_content(
    body: str, speaker: str, *, hide_speaker: bool = True, speaker_as_json: bool = False,
) -> str:
    """Keep source text verbatim and optionally make its author prompt-visible.

    Call this only for raw posts/comments, never for persisted memory events.
    Event identity and attribution metadata remain independent of this setting.
    """
    if hide_speaker:
        return body
    if speaker_as_json:
        return json.dumps({"speaker": speaker, "content": body}, ensure_ascii=False)
    return f"{speaker}：{body}"
