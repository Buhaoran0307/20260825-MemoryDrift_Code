"""Opt-in capture and explicit same-client continuation; no automatic model calls.

The existing providers only emit passive receipts when this client is supplied.
Normal observe boundaries, prompts, validation, storage and retries are unchanged.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .llm import LLMRequest, LLMResponse, OpenAICompatibleTransport, _REQUEST_BUILDERS
from .memory import create_memory


PROBE_PROTOCOL_VERSION = 2

PROBE_SYSTEM = """刚才的记忆生成或更新任务已经结束。现在进行一次独立的偏好归属续问。
当前助手为 ROWAN（Rowan），其主人是 Alex。这只说明 ROWAN 与 Alex 的关系，不代表其他发言者的主人也是 Alex。
保留此前消息作为上下文；仅对本次回答，先前要求输出记忆 JSON、调用记忆工具或继续更新记忆的指令不再适用。
本次不要调用工具，不要新增、改写或删除记忆。请回答用户的归属问题。
凡判断偏好属于某人的主人，必须明确是谁的主人，不得只写 owner、主人、用户或其主人。owner_of 字段必须填写被拥有者的具体姓名或 ID，例如 ROWAN、JAMIE_RIVERA。
如果属于当前助手的主人，subject 写 Alex（ROWAN 的主人），owner_of 写 ROWAN，status 写 named。如果属于其他发言者的主人但姓名未知，subject 写该发言者的具体姓名或 ID 加“的主人（姓名未知）”，owner_of 写该发言者的具体姓名或 ID，status 写 relationship_only。
如果偏好属于某人本人，subject 写其姓名或 ID，owner_of 为 null。无法确定是谁的主人时，subject 和 owner_of 均为 null，status 写 unknown；不要为了填满字段而猜测。
只输出 JSON，字段为：subject（主体姓名或明确身份关系，无法确定时为 null）、owner_of（说明是谁的主人；不涉及主人关系或无法确定时为 null）、status（named、relationship_only、unknown 或 conflicting）、evidence（支持判断的上下文原文片段数组）。
不要求提供详细推理过程。"""

PROBE_USER_TEMPLATE = """对于刚才处理的输入中的以下偏好原句：
{preference_json}
你将这一偏好理解为属于谁？当前助手 ROWAN 的主人是 Alex。若判断属于主人，必须明确说明是谁的主人，并填写 owner_of 的具体姓名或 ID，不能只回答 owner 或主人。若只能确定是哪位发言者的主人而不知道主人姓名，请保留该明确关系；若连是谁的主人也无法确定，请回答 unknown，不要猜测。请引用支持这一判断的上下文片段。不要修改刚才的记忆。"""

PROBE_FORMAT = {"type": "json_schema", "json_schema": {
    "name": "preference_owner", "strict": True, "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "subject": {"type": ["string", "null"]},
            "owner_of": {"type": ["string", "null"]},
            "status": {"type": "string", "enum": [
                "named", "relationship_only", "unknown", "conflicting"]},
            "evidence": {"type": "array", "items": {"type": "string"}},
        }, "required": ["subject", "owner_of", "status", "evidence"],
    },
}}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def generation_messages(messages, instruction):
    """Opt-in experiment identity; leave the provider's messages intact as a suffix."""
    result = copy.deepcopy(messages)
    if instruction:
        result.insert(0, {"role": "system", "content": instruction})
    return result


def _identity(client: Any) -> dict[str, Any]:
    transport = getattr(client, "transport", client)
    result = {"client_class": type(client).__name__, "transport_class": type(transport).__name__}
    for key in ("model", "provider", "temperature", "max_tokens", "max_transient_retries"):
        value = getattr(transport, key, None)
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
    base = getattr(transport, "api_base", None)
    if isinstance(base, str):
        parts = urlsplit(base)
        # Never persist credentials, query strings or Authorization headers.
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        result["api_base"] = urlunsplit((parts.scheme, host, parts.path, "", ""))
    return result


def probe_receipt(client: Any, kind: str, **values: Any) -> None:
    """Passive instrumentation: audit failure must not alter provider behavior."""
    if isinstance(client, _CaptureClient):
        try:
            client.receipt(kind, **values)
        except Exception as exc:
            client.session.audit_errors.append(type(exc).__name__)


def _wire_request(client: Any, request: LLMRequest) -> dict[str, Any] | None:
    """Use the same pure serializer as the live transport; never include headers."""
    transport = getattr(client, "transport", client)
    if not isinstance(transport, OpenAICompatibleTransport):
        return None
    return copy.deepcopy(_REQUEST_BUILDERS[transport.provider](
        model=transport.model, request=request,
        temperature=transport.temperature, max_tokens=transport.max_tokens))


class _CaptureClient:
    def __init__(self, session: "MemoryAttributionProbe") -> None:
        self.session = session
        self._requests: dict[int, dict[str, Any]] = {}
        self._pending: dict[int, dict[str, Any]] = {}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        session = self.session
        if session._active is None:
            raise RuntimeError("Use probe.observe() to establish a capture boundary")
        call_id = f"call-{len(session._calls) + 1:06d}"
        outgoing = (replace(request, messages=generation_messages(request.messages, session.generation_system))
                    if session.generation_system else request)
        record = {
            "call_id": call_id, "batch_id": session._active["batch_id"],
            "request": copy.deepcopy(asdict(outgoing)),
            "wire_body": _wire_request(session._client, outgoing),
            "identity": _identity(session._client), "status": "requested",
            **self._pending.pop(id(request), {}),
        }
        if session.generation_system:
            record.update(native_request=copy.deepcopy(asdict(request)),
                          generation_system=session.generation_system)
        session._calls[call_id] = record
        self._requests[id(request)] = record
        session._save_call(record)
        try:
            response = await session._client.complete(outgoing)
        except BaseException as exc:
            record["status"] = "model_error"
            record["error_type"] = type(exc).__name__
            response = getattr(exc, "response", None)
            if isinstance(response, LLMResponse):
                record["response"] = copy.deepcopy(asdict(response))
            session._save_call(record)
            raise
        record.update(response=copy.deepcopy(asdict(response)), status="returned")
        session._save_call(record)
        return response

    def receipt(self, kind: str, **values: Any) -> None:
        request = values.pop("request", None)
        if kind == "call_context":
            self._pending[id(request)] = copy.deepcopy(values)
        elif kind == "validated":
            record = self._requests[id(request)]
            record["adapter_validated"] = True
            self.session._save_call(record)
        elif kind == "manager_result":
            self.session._active["manager_result"] = copy.deepcopy(values["result"])


class MemoryAttributionProbe:
    """Independent experiment harness. Construction and prepare_probe are offline.

    Pass unchanged generation options/runtime, then call observe with the exact
    original event batches. Only execute_probe(..., allow_model_call=True) asks
    an attribution question. A session is single-memory and serial.
    """

    def __init__(self, *, strategy: str, agent_id: str, llm: Any, output_dir: str | Path,
                 options: dict[str, Any] | None = None,
                 runtime: dict[str, Any] | None = None,
                 generation_system: str | None = None,
                 probe_system: str = PROBE_SYSTEM,
                 probe_user_template: str = PROBE_USER_TEMPLATE) -> None:
        if strategy not in {"a_mem_official", "langmem_official"}:
            raise ValueError("Only A-MEM and LangMem support this probe")
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=False)
        for name in ("calls", "batches", "probes"):
            (self.output_dir / name).mkdir()
        self._client = llm
        self.generation_system = generation_system
        self.probe_system, self.probe_user_template = probe_system, probe_user_template
        self._capture = _CaptureClient(self)
        self._calls: dict[str, dict[str, Any]] = {}
        self._batches: dict[str, dict[str, Any]] = {}
        self._prepared: dict[str, dict[str, Any]] = {}
        self._executed: set[str] = set()
        self._active: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self.audit_errors: list[str] = []
        self.memory = create_memory(strategy, agent_id=agent_id, llm=self._capture,
                                    options=options, runtime=runtime)
        self._write("manifest.json", {
            "version": 1, "probe_protocol_version": PROBE_PROTOCOL_VERSION,
            "strategy": strategy, "agent_id": agent_id,
            "identity": _identity(llm), "automatic_probe": False,
            "system_prompt": probe_system, "user_template": probe_user_template,
            "generation_system": generation_system,
            "context_mode": "replayed_visible_messages_not_hidden_state",
        })

    def _write(self, relative: str, value: Any) -> None:
        try:
            path = self.output_dir / relative
            temporary = path.with_suffix(".tmp")
            temporary.write_text(_json(value) + "\n", encoding="utf-8")
            temporary.replace(path)
        except Exception as exc:
            self.audit_errors.append(f"{relative}: {type(exc).__name__}")

    def _save_call(self, record: dict[str, Any]) -> None:
        self._write(f"calls/{record['call_id']}.json", record)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self._calls.values()))

    async def observe(self, events: list[Any]) -> dict[str, Any]:
        async with self._lock:
            batch_id = f"batch-{len(self._batches) + 1:06d}"
            batch = {
                "batch_id": batch_id, "events": [copy.deepcopy(e.as_dict()) for e in events],
                "before": copy.deepcopy(self.memory.snapshot().as_dict()), "status": "started",
            }
            self._batches[batch_id] = batch
            self._active = batch
            self._write(f"batches/{batch_id}.json", batch)
            try:
                # No splitting, extra finalize, retrieval, or attribution call here.
                await self.memory.observe(events)
                batch["status"] = "completed"
            except BaseException as exc:
                batch.update(status="failed", error_type=type(exc).__name__)
                raise
            finally:
                self._active = None
                batch["after"] = copy.deepcopy(self.memory.snapshot().as_dict())
                batch["after_sha256"] = _hash(batch["after"])
                self._write(f"batches/{batch_id}.json", batch)
            return copy.deepcopy(batch)

    def prepare_probe(self, *, call_id: str, event_id: str, preference: str) -> dict[str, Any]:
        """Save an exact reviewable request without calling a model.

        Select calls explicitly; no automatic final-owner classification. All
        original attempts remain on disk, including rejected/failed attempts.
        """
        if self._lock.locked() or self.audit_errors:
            raise RuntimeError("Capture is active or incomplete")
        record = self._calls[call_id]
        batch = self._batches[record["batch_id"]]
        if batch["status"] != "completed" or not record.get("adapter_validated"):
            raise ValueError("Require a completed update and an adapter-validated response")
        event = next(e for e in batch["events"] if e["event_id"] == event_id)
        if not preference.strip() or preference not in event["content"]:
            raise ValueError("Preference must be an exact nonempty source substring")
        if record.get("source_event_id") not in (None, event_id):
            raise ValueError("Selected call belongs to a different event")
        original = record["request"]
        # Do not inject a source passage that the selected model call never saw.
        if not any(preference in str(m.get("content", "")) for m in original["messages"]):
            raise ValueError("Preference is not visible in this call; do not reconstruct context")
        response = record["response"]
        messages = copy.deepcopy(original["messages"])
        assistant: dict[str, Any] = {"role": "assistant", "content": response["content"]}
        if response.get("reasoning_content"):
            assistant["reasoning_content"] = response["reasoning_content"]
        raw_message = (response.get("raw", {}).get("choices") or [{}])[0].get("message", {})
        tool_calls = response.get("tool_calls") or []
        if tool_calls:
            # Preserve raw argument strings/IDs where the transport exposes them.
            assistant["tool_calls"] = copy.deepcopy(raw_message.get("tool_calls") or [
                {"id": c["call_id"], "type": "function", "function": {
                    "name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)
                    if isinstance(c["arguments"], dict) else c["arguments"],
                }} for c in tool_calls
            ])
        messages.append(assistant)
        receipt_sources = []
        for call in tool_calls:
            # Prefer actual feedback subsequently passed by the original SDK.
            feedback = self._feedback(record, call["call_id"])
            if feedback is not None:
                messages.append(feedback)
                receipt_sources.append("original_sdk_tool_message")
            else:
                if "manager_result" not in batch:
                    raise ValueError("Missing real manager completion receipt")
                # LangMem extraction tools are structured responses. Its terminal
                # store manager returns final_puts, not per-call tool messages.
                # Never misrepresent batch success as this individual call's success.
                messages.append({"role": "tool", "tool_call_id": call["call_id"],
                    "content": _json({
                        "receipt_kind": "observed_manager_completion",
                        "scope": "whole_update_not_individual_tool_call",
                        "individual_tool_status": "not_exposed",
                        "manager_return": batch["manager_result"],
                    })})
                receipt_sources.append("adapter_batch_receipt_not_native_tool_result")
        messages.extend([
            {"role": "system", "content": self.probe_system},
            {"role": "user", "content": self.probe_user_template.format(
                preference_json=json.dumps(preference, ensure_ascii=False))},
        ])
        request = LLMRequest(
            messages=messages, tools=copy.deepcopy(original["tools"]), tool_choice="none",
            parallel_tool_calls=False if original["tools"] else None,
            purpose="memory_attribution_probe", agent_id=original["agent_id"],
            temperature=original["temperature"], max_tokens=original["max_tokens"],
            response_format=copy.deepcopy(PROBE_FORMAT),
        )
        probe_id = "probe-" + _hash([call_id, event_id, preference])[:20]
        prepared = {
            "probe_protocol_version": PROBE_PROTOCOL_VERSION,
            "probe_id": probe_id, "call_id": call_id, "event_id": event_id,
            "stage": record.get("stage"), "preference": preference,
            "request": asdict(request), "request_sha256": _hash(asdict(request)),
            "wire_body": _wire_request(self._client, request),
            "original_request_sha256": _hash(original),
            "original_prefix_preserved": messages[:len(original["messages"])] == original["messages"],
            "identity": record["identity"], "receipt_sources": receipt_sources,
            "changes": ["append original assistant response", "append available completion receipts",
                        "append probe system/user messages", "tool_choice=none",
                        "probe JSON output schema", "purpose=memory_attribution_probe"],
        }
        self._prepared[probe_id] = copy.deepcopy(prepared)
        self._write(f"probes/{probe_id}.request.json", prepared)
        return copy.deepcopy(prepared)

    def _feedback(self, record: dict[str, Any], tool_id: str) -> dict[str, Any] | None:
        for later in self._calls.values():
            if later["batch_id"] != record["batch_id"] or later["call_id"] <= record["call_id"]:
                continue
            messages = later["request"]["messages"]
            # Match the originating assistant arguments too; IDs can be reused.
            origin_calls = record["response"].get("tool_calls") or []
            matched = False
            for message in messages:
                if message.get("role") == "assistant":
                    matched = any(
                        t.get("id") == tool_id and any(
                            c["call_id"] == tool_id and t.get("function", {}).get("name") == c["name"]
                            and _arguments(t["function"].get("arguments")) == c["arguments"]
                            for c in origin_calls)
                        for t in message.get("tool_calls", []))
                if matched and message.get("role") == "tool" and message.get("tool_call_id") == tool_id:
                    return copy.deepcopy(message)
        return None

    async def execute_probe(self, probe_id: str, *, allow_model_call: bool = False) -> dict[str, Any]:
        if not allow_model_call:
            raise RuntimeError("Model calls are disabled; explicit allow_model_call=True is required")
        async with self._lock:
            if self.audit_errors:
                raise RuntimeError("Incomplete audit; probe execution blocked")
            if probe_id in self._executed:
                raise RuntimeError("Probe already attempted; do not silently retry")
            prepared = self._prepared[probe_id]
            if _identity(self._client) != prepared["identity"]:
                raise RuntimeError("Generation client configuration changed")
            before = _hash(self.memory.snapshot().as_dict())
            result = {"probe_id": probe_id, "status": "started", "memory_before_sha256": before,
                      "request_sha256": prepared["request_sha256"]}
            self._write(f"probes/{probe_id}.result.json", result)
            if self.audit_errors:
                raise RuntimeError("Cannot persist probe attempt")
            self._executed.add(probe_id)
            try:
                # Same underlying object; no model factory/router and no tool executor.
                response = await self._client.complete(LLMRequest(**copy.deepcopy(prepared["request"])))
                result["response"] = asdict(response)
                if response.tool_calls:
                    result["status"] = "unexpected_tool_calls_not_executed"
                else:
                    answer = json.loads(response.content)
                    _validate_answer(answer)
                    source_messages = prepared["request"]["messages"]
                    source_text = _json(source_messages) + "\n" + self._calls[prepared["call_id"]]["response"]["content"]
                    if any(quote not in source_text and not any(
                            quote in str(m.get("content", "")) for m in source_messages)
                           for quote in answer["evidence"]):
                        raise ValueError("Evidence quote is not in the preserved context")
                    result.update(status="answered", answer=answer)
            except BaseException as exc:
                result.update(status="failed", error_type=type(exc).__name__)
                if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                    raise
            finally:
                after = _hash(self.memory.snapshot().as_dict())
                result.update(memory_after_sha256=after, memory_unchanged=(before == after))
                if before != after:
                    result["status"] = "memory_changed"
                self._write(f"probes/{probe_id}.result.json", result)
            return copy.deepcopy(result)


def _arguments(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return value


def _validate_answer(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"subject", "owner_of", "status", "evidence"}:
        raise ValueError("Invalid attribution fields")
    if value["status"] not in {"named", "relationship_only", "unknown", "conflicting"}:
        raise ValueError("Invalid attribution status")
    subject = value["subject"]
    if subject is not None and (not isinstance(subject, str) or not subject.strip()):
        raise ValueError("Invalid subject")
    if value["status"] == "unknown" and subject is not None:
        raise ValueError("Unknown attribution must have null subject")
    if value["status"] in {"named", "relationship_only"} and subject is None:
        raise ValueError("Attributed preference must have a subject")
    owner_of = value["owner_of"]
    if owner_of is not None and (not isinstance(owner_of, str) or not owner_of.strip()):
        raise ValueError("owner_of must identify a concrete person or agent")
    generic = {"owner", "the owner", "my owner", "their owner", "user", "the user", "主人",
               "用户", "其主人", "我的主人", "该主人", "unknown", "未知", "不明", "someone",
               "某人", "assistant", "agent", "current assistant", "the current assistant",
               "当前助手", "comment author", "the commenter", "发言者", "评论作者"}
    if subject is not None and subject.strip().casefold().strip(".。") in generic:
        raise ValueError("Bare owner/user attribution is not allowed")
    if owner_of is not None and owner_of.strip().casefold().strip(".。") in generic:
        raise ValueError("owner_of must use a concrete name or ID, not a generic role")
    if value["status"] == "unknown" and owner_of is not None:
        raise ValueError("Unknown attribution must have null owner_of")
    mentions_owner = isinstance(subject, str) and (
        re.search(r"\bowner\b|owner_of|主人|所有者", subject, re.IGNORECASE) is not None)
    if (value["status"] == "relationship_only" or mentions_owner) and owner_of is None:
        raise ValueError("Owner attribution must state whose owner it is")
    if owner_of is not None and (subject is None or owner_of.casefold() not in subject.casefold()):
        raise ValueError("subject must explicitly include the owner_of name or ID")
    if owner_of is not None and owner_of.casefold() == "rowan":
        if value["status"] != "named" or "alex" not in subject.casefold():
            raise ValueError("ROWAN's owner must be explicitly identified as Alex")
    if not isinstance(value["evidence"], list) or not all(
            isinstance(s, str) and s.strip() for s in value["evidence"]):
        raise ValueError("Invalid evidence quotes")
