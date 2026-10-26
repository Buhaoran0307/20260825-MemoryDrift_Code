"""Hermes-style memory decisions, with output only and no memory tool execution."""
from __future__ import annotations

import json
from typing import Any

from .llm import LLMRequest, LLMResponse, response_finish_reason

HERMES_REVISION = "f42f579cf8bac4918ac9599bece71618afadd846"
HERMES_PROMPT_SOURCE = (
    f"https://github.com/NousResearch/hermes-agent/blob/{HERMES_REVISION}/agent/prompt_builder.py#L193-L231"
)
HERMES_SCHEMA_SOURCE = (
    f"https://github.com/NousResearch/hermes-agent/blob/{HERMES_REVISION}/tools/memory_tool.py#L317-L342"
)

# Verbatim build_memory_guidance(True, True, skill_manage_available=False).
# Skill tools are unavailable in this output-only experiment.
HERMES_MEMORY_GUIDANCE = (
    "You have persistent memory, carried across sessions and loaded "
    "into each new session's context; the memory tool's schema defines what belongs there. "
    "Task-specific knowledge — procedures, pitfalls, and the user's preferences "
    "and corrections for that kind of work — belongs in skills, not in memory, "
    "even when skill writing is unavailable. "
    "Memory is the narrow exception for facts that apply to EVERY "
    "session regardless of task (who the user is, environment facts, "
    "standing conventions with no task home); it has a hard character "
    "budget, so when it fills, replace or consolidate stale entries "
    "rather than skipping the save. Write entries as declarative facts, "
    "not instructions to yourself: 'User prefers concise responses' ✓ — "
    "'Always respond concisely' ✗ (imperative phrasing gets re-read as "
    "a directive in later sessions and can override the user's current "
    "request). A fact stale within a week belongs in session history; "
    "procedures and workflows belong in skills."
)

# Verbatim description from MEMORY_SCHEMA. This is reference text, not a tool.
HERMES_MEMORY_DESCRIPTION = (
    "Save durable facts to persistent memory that survive across sessions. Memory is "
    "injected into every future turn, so keep entries compact and high-signal.\n\n"
    "HOW: make ALL your changes in ONE call via an 'operations' array (each item: "
    "{action, content?, old_text?}). The batch applies atomically and the char limit is "
    "checked only on the FINAL result — so a single call can remove/replace stale entries "
    "to free room AND add new ones, even when an add alone would overflow. The response "
    "reports current/limit chars and confirms completion; one batch call finishes the "
    "update, so don't repeat it. Use the bare action/content/old_text fields only for a "
    "single lone change.\n\n"
    "WHEN: only for facts that apply to EVERY session regardless of task: who the user "
    "is, stable environment facts, standing conventions with no task home. Anything "
    "learned while doing a task (procedures, pitfalls, and the user's preferences and "
    "corrections for that kind of work) belongs in the task's skill via skill_manage, "
    "where it loads only when relevant; memory is injected into every turn and must "
    "stay small.\n\n"
    "IF FULL: an add is rejected with the current entries shown. Reissue as ONE batch that "
    "removes or shortens enough stale entries and adds the new one together.\n\n"
    "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
    "notes (environment, conventions, tool quirks, lessons).\n\n"
    "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, "
    "completed-work logs, temporary TODO state (use session_search for those). Reusable "
    "procedures belong in a skill, not memory."
)

PREVIEW_ADAPTATION = (
    "This is an output-only test of the above Hermes memory policy. No memory store "
    "or skill tool is available; do not call tools or claim that anything was saved. "
    "Read the supplied forum thread and decide for yourself whether you would save "
    "any durable declarative facts under that policy. Do not force a memory for every "
    "thread. Output only JSON with this shape: "
    '{"decision":"save" or "skip","facts":[{"target":"user" or "memory",'
    '"content":"one compact declarative fact"}]}. '
    "Use decision='skip' with facts=[] when nothing qualifies. Earlier proposals "
    "are conversation context only, not existing persistent memories."
)


def build_preview_request(
    *, agent_id: str, profile: dict[str, Any], thread: dict[str, Any],
    previous_proposals: list[dict[str, Any]],
) -> LLMRequest:
    return LLMRequest(
        messages=[
            {"role": "system", "content": "\n\n".join((
                HERMES_MEMORY_GUIDANCE,
                "Hermes memory tool reference description:\n" + HERMES_MEMORY_DESCRIPTION,
                PREVIEW_ADAPTATION,
            ))},
            {"role": "user", "content": json.dumps({
                "task": "Read this forum thread and output your memory fact proposal.",
                "reader_profile": profile,
                "earlier_proposals": previous_proposals,
                "forum_thread": thread,
            }, ensure_ascii=False)},
        ],
        tools=[], tool_choice="auto", purpose="agent_memory_preview", agent_id=agent_id,
    )


def parse_preview_response(response: LLMResponse) -> dict[str, Any]:
    """Validate decisions without replacing or silently inventing model facts."""
    if response.tool_calls:
        raise ValueError("output-only memory preview returned tool calls; none were executed")
    if (response_finish_reason(response) in {"length", "max_tokens", "max_output_tokens", "content_filter"}
            or response.raw.get("status") == "incomplete"):
        raise ValueError("memory preview response was incomplete")
    text = response.content.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[len("```json\n"):-3].strip()
    elif text.startswith("```\n") and text.endswith("```"):
        text = text[4:-3].strip()
    result = json.loads(text)
    if not isinstance(result, dict) or set(result) != {"decision", "facts"}:
        raise ValueError("memory preview must contain exactly decision and facts")
    if result["decision"] not in {"save", "skip"} or not isinstance(result["facts"], list):
        raise ValueError("invalid memory preview decision or facts")
    for fact in result["facts"]:
        if (not isinstance(fact, dict) or set(fact) != {"target", "content"}
                or fact["target"] not in {"user", "memory"}
                or not isinstance(fact["content"], str) or not fact["content"].strip()):
            raise ValueError("each proposed fact needs target=user|memory and nonempty content")
    if (result["decision"] == "save") != bool(result["facts"]):
        raise ValueError("save requires facts; skip requires an empty facts list")
    return result
