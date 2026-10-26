from __future__ import annotations

import asyncio
import errno
import http.client
import json
import logging
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Protocol

from .simulation import ToolCall
from .llm_diagnostics import LLMHTTPError, http_failure_detail, redact, safe_endpoint


DEFAULT_LLM_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_TRANSIENT_RETRIES = 8
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0
DEFAULT_MAX_RETRY_WAIT_SECONDS = 60.0
_RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMRequest:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = "auto"
    parallel_tool_calls: bool | None = None
    purpose: str = "unspecified"
    agent_id: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # Keep older callers and SDK defaults from forcing a tool call. Normalize
        # here so the saved request and every provider's payload agree.
        if self.tool_choice == "required":
            object.__setattr__(self, "tool_choice", "auto")


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)
    reasoning_content: str = ""

    @classmethod
    def from_openai_payload(cls, payload: dict[str, Any]) -> "LLMResponse":
        choices = payload.get("choices") or []
        if not choices:
            raise ValueError("LLM response did not contain choices")
        message = choices[0].get("message") or {}
        content_value = message.get("content") or ""
        if isinstance(content_value, list):
            content = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content_value
            )
        else:
            content = str(content_value)
        calls: list[ToolCall] = []
        argument_errors: list[ValueError] = []
        for index, item in enumerate(message.get("tool_calls") or []):
            function = item.get("function") or {}
            arguments = function.get("arguments") or "{}"
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    argument_errors.append(exc)
                    # Keep the original argument string for the response audit.
                    # All call identities must remain available for classification.
                    calls.append(ToolCall(
                        call_id=str(item.get("id") or f"llm-call-{index + 1}"),
                        name=str(function.get("name") or ""),
                        arguments=arguments,
                    ))
                    continue
            if not isinstance(arguments, dict):
                argument_errors.append(ValueError("LLM tool call arguments must be an object"))
            calls.append(
                ToolCall(
                    call_id=str(item.get("id") or f"llm-call-{index + 1}"),
                    name=str(function.get("name") or ""),
                    arguments=dict(arguments) if isinstance(arguments, dict) else arguments,
                )
            )
        response = cls(
            content=content,
            tool_calls=tuple(calls),
            raw=payload,
            reasoning_content=str(message.get("reasoning_content") or ""),
        )
        if argument_errors:
            raise LLMToolCallParseError(str(argument_errors[0]), response) from argument_errors[0]
        return response

    @classmethod
    def from_responses_payload(cls, payload: dict[str, Any]) -> "LLMResponse":
        output = payload.get("output")
        if not isinstance(output, list):
            raise ValueError("Responses API response did not contain output")
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: list[ToolCall] = []
        argument_errors: list[ValueError] = []
        for index, item in enumerate(output):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "reasoning":
                for summary in item.get("summary") or []:
                    if isinstance(summary, dict) and summary.get("text"):
                        reasoning_parts.append(str(summary["text"]))
                continue
            if item_type == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("text"):
                        content_parts.append(str(part["text"]))
                continue
            if item_type != "function_call":
                continue
            arguments = item.get("arguments") or "{}"
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    argument_errors.append(exc)
                    calls.append(ToolCall(
                        call_id=str(item.get("call_id") or item.get("id") or f"llm-call-{index + 1}"),
                        name=str(item.get("name") or ""),
                        arguments=arguments,
                    ))
                    continue
            if not isinstance(arguments, dict):
                argument_errors.append(ValueError("Responses function call arguments must be an object"))
            calls.append(
                ToolCall(
                    call_id=str(item.get("call_id") or item.get("id") or f"llm-call-{index + 1}"),
                    name=str(item.get("name") or ""),
                    arguments=dict(arguments) if isinstance(arguments, dict) else arguments,
                )
            )
        response = cls(
            content="".join(content_parts),
            tool_calls=tuple(calls),
            raw=payload,
            reasoning_content="\n\n".join(reasoning_parts),
        )
        if argument_errors:
            raise LLMToolCallParseError(str(argument_errors[0]), response) from argument_errors[0]
        return response


class LLMToolCallParseError(ValueError):
    """Invalid tool arguments with the complete response retained for audit."""

    def __init__(self, message: str, response: LLMResponse):
        super().__init__(message)
        self.response = response
        self.parse_error = message


def response_finish_reason(response: LLMResponse) -> str | None:
    """Return a Chat Completions-compatible finish reason for either API."""
    choices = response.raw.get("choices") if isinstance(response.raw, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0].get("finish_reason")
    if not isinstance(response.raw, dict):
        return None
    if response.raw.get("status") == "incomplete":
        details = response.raw.get("incomplete_details") or {}
        if isinstance(details, dict) and details.get("reason"):
            return str(details["reason"])
    if response.tool_calls:
        return "tool_calls"
    if response.raw.get("status") == "completed" and response.content:
        return "stop"
    return None


class LLMTransport(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class LLMClient(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class LLMCallManager:
    """Process-wide concurrency gate for every model request."""

    def __init__(self, max_concurrency: int = 15):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.max_concurrency = max_concurrency
        self._lock = threading.Lock()
        self._waiters: deque[int] = deque()
        self._next_ticket = 0
        self._in_flight = 0
        self._peak_in_flight = 0
        self._total_calls = 0
        self._by_purpose: dict[str, int] = {}

    async def run(
        self,
        operation: Callable[[], Awaitable[LLMResponse]],
        *,
        purpose: str = "unspecified",
    ) -> LLMResponse:
        await self._acquire(purpose)
        try:
            return await operation()
        finally:
            with self._lock:
                self._in_flight -= 1

    async def _acquire(self, purpose: str) -> None:
        # asyncio primitives bind to one loop, while the stdlib HTTP server can run
        # several loops on request threads. A short cooperative ticket wait remains
        # process-wide without occupying executor threads needed by HTTP transports.
        with self._lock:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiters.append(ticket)
        acquired = False
        try:
            while not acquired:
                with self._lock:
                    if (
                        self._waiters
                        and self._waiters[0] == ticket
                        and self._in_flight < self.max_concurrency
                    ):
                        self._waiters.popleft()
                        self._in_flight += 1
                        self._peak_in_flight = max(
                            self._peak_in_flight, self._in_flight
                        )
                        self._total_calls += 1
                        self._by_purpose[purpose] = (
                            self._by_purpose.get(purpose, 0) + 1
                        )
                        acquired = True
                if not acquired:
                    await asyncio.sleep(0.002)
        finally:
            if not acquired:
                with self._lock:
                    try:
                        self._waiters.remove(ticket)
                    except ValueError:
                        pass

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_concurrency": self.max_concurrency,
                "in_flight": self._in_flight,
                "queued": len(self._waiters),
                "peak_in_flight": self._peak_in_flight,
                "total_calls": self._total_calls,
                "by_purpose": dict(self._by_purpose),
            }


class ManagedLLMClient:
    def __init__(self, transport: LLMTransport, manager: LLMCallManager):
        self.transport = transport
        self.manager = manager

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return await self.manager.run(
            lambda: self.transport.complete(request),
            purpose=request.purpose,
        )


_KNOWN_LLM_PROVIDERS = frozenset({"deepseek", "openai", "qwen", "compatible"})


def _infer_llm_provider(*, model: str, api_base: str) -> str:
    model_name = model.strip().lower()
    base = api_base.strip().lower()
    if "deepseek" in model_name or "deepseek" in base:
        return "deepseek"
    if "qwen" in model_name or "dashscope" in base or "aliyuncs.com" in base:
        return "qwen"
    if (
        model_name.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4"))
        or "openai.com" in base
    ):
        return "openai"
    return "compatible"


def _request_temperature(request: LLMRequest, default: float) -> float:
    return request.temperature if request.temperature is not None else default


def _request_max_tokens(request: LLMRequest, default: int) -> int:
    return request.max_tokens if request.max_tokens is not None else default


def _function_tools(request: LLMRequest) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for schema in request.tools:
        function = {
            "name": schema["name"],
            "description": schema.get("description", ""),
            "parameters": schema.get("input_schema", {}),
        }
        if isinstance(schema.get("strict"), bool):
            function["strict"] = schema["strict"]
        tools.append({"type": "function", "function": function})
    return tools


def _add_tool_fields(body: dict[str, Any], request: LLMRequest) -> None:
    if not request.tools:
        return
    body["tools"] = _function_tools(request)
    if request.tool_choice is not None:
        body["tool_choice"] = request.tool_choice
    if request.parallel_tool_calls is not None:
        body["parallel_tool_calls"] = request.parallel_tool_calls


def _compatible_response_format(value: dict[str, Any]) -> dict[str, Any]:
    # DeepSeek and DashScope compatible mode support JSON object output broadly,
    # while OpenAI's json_schema contract is not portable across their models.
    if value.get("type") == "json_schema":
        return {"type": "json_object"}
    return dict(value)


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the platform's Chat Completions history to Responses items."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        tool_calls = message.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            if message.get("content"):
                converted.append({"role": "assistant", "content": message["content"]})
            for item in tool_calls:
                function = item.get("function") or {}
                arguments = function.get("arguments") or "{}"
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                converted.append({
                    "type": "function_call",
                    "call_id": str(item.get("id") or uuid.uuid4().hex),
                    "name": str(function.get("name") or ""),
                    "arguments": arguments,
                })
            continue
        if role == "tool":
            converted.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or ""),
                "output": str(message.get("content") or ""),
            })
            continue
        plain_message = dict(message)
        # This is a DashScope Chat Completions extension and is not a valid
        # Responses input field; GPT reasoning items are returned separately.
        plain_message.pop("reasoning_content", None)
        converted.append(plain_message)
    return converted


def _responses_tools(request: LLMRequest) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for schema in request.tools:
        tool: dict[str, Any] = {
            "type": "function",
            "name": schema["name"],
            "description": schema.get("description", ""),
            "parameters": schema.get("input_schema", {}),
        }
        if isinstance(schema.get("strict"), bool):
            tool["strict"] = schema["strict"]
        tools.append(tool)
    return tools


def _responses_tool_choice(value: Any) -> Any:
    if isinstance(value, dict) and value.get("type") == "function":
        function = value.get("function") or {}
        return {"type": "function", "name": function.get("name", "")}
    return value


def _responses_text_format(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("type") == "json_schema":
        schema = value.get("json_schema") or {}
        return {
            "type": "json_schema",
            "name": schema.get("name", "response"),
            "schema": schema.get("schema", {}),
            "strict": schema.get("strict", True),
        }
    return dict(value)


def _is_openai_reasoning_model(model: str) -> bool:
    model_name = model.strip().lower()
    return model_name.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


def _build_openai_request(
    *, model: str, request: LLMRequest, temperature: float, max_tokens: int,
) -> dict[str, Any]:
    reasoning_model = _is_openai_reasoning_model(model)
    body: dict[str, Any] = {
        "model": model,
        "input": _responses_input(request.messages),
        "max_output_tokens": _request_max_tokens(request, max_tokens),
    }
    if reasoning_model:
        body["reasoning"] = {"effort": "medium", "summary": "auto"}
    else:
        body["temperature"] = _request_temperature(request, temperature)
    if request.tools:
        body["tools"] = _responses_tools(request)
        if request.tool_choice is not None:
            body["tool_choice"] = _responses_tool_choice(request.tool_choice)
        if request.parallel_tool_calls is not None:
            body["parallel_tool_calls"] = request.parallel_tool_calls
    if request.response_format is not None:
        body["text"] = {"format": _responses_text_format(request.response_format)}
    return body


def _build_deepseek_request(
    *, model: str, request: LLMRequest, temperature: float, max_tokens: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": request.messages,
        "max_tokens": _request_max_tokens(request, max_tokens),
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }
    if "reasoner" not in model.strip().lower():
        body["temperature"] = _request_temperature(request, temperature)
    _add_tool_fields(body, request)
    if request.response_format is not None:
        body["response_format"] = _compatible_response_format(
            request.response_format
        )
    return body


def _build_qwen_request(
    *, model: str, request: LLMRequest, temperature: float, max_tokens: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": request.messages,
        "temperature": _request_temperature(request, temperature),
        "max_completion_tokens": _request_max_tokens(request, max_tokens),
        "enable_thinking": True,
        "reasoning_effort": "medium",
    }
    if request.tools:
        body["tools"] = _function_tools(request)
        # Qwen thinking mode does not support required/named tool choice.
        body["tool_choice"] = (
            "none" if request.purpose == "memory_attribution_probe" and request.tool_choice == "none"
            else "auto"
        )
        if request.parallel_tool_calls is not None:
            body["parallel_tool_calls"] = request.parallel_tool_calls
    if request.response_format is not None:
        body["response_format"] = _compatible_response_format(
            request.response_format
        )
    return body


def _build_compatible_request(
    *, model: str, request: LLMRequest, temperature: float, max_tokens: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": request.messages,
        "temperature": _request_temperature(request, temperature),
        "max_tokens": _request_max_tokens(request, max_tokens),
    }
    _add_tool_fields(body, request)
    if request.response_format is not None:
        body["response_format"] = dict(request.response_format)
    return body


_REQUEST_BUILDERS = {
    "openai": _build_openai_request,
    "deepseek": _build_deepseek_request,
    "qwen": _build_qwen_request,
    "compatible": _build_compatible_request,
}


class OpenAICompatibleTransport:
    """Provider-aware chat transport; concurrency belongs to the manager."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        provider: str | None = None,
        timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        temperature: float = 0.2,
        max_tokens: int = 800,
        max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        max_retry_wait_seconds: float = DEFAULT_MAX_RETRY_WAIT_SECONDS,
        min_request_interval_seconds: float = 0.0,
    ):
        if not api_base.strip() or not model.strip():
            raise ValueError("api_base and model are required")
        if timeout <= 0 or max_tokens < 1:
            raise ValueError("timeout must be positive and max_tokens must be positive")
        if max_transient_retries < 0:
            raise ValueError("max_transient_retries cannot be negative")
        if retry_backoff_seconds <= 0 or max_retry_wait_seconds <= 0:
            raise ValueError("retry waits must be positive")
        if min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds cannot be negative")
        if provider is not None and provider.strip().lower() not in _KNOWN_LLM_PROVIDERS:
            raise ValueError(
                "provider must be one of: compatible, deepseek, openai, qwen"
            )
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = (
            provider.strip().lower() if provider is not None
            else _infer_llm_provider(model=model, api_base=api_base)
        )
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_transient_retries = max_transient_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_retry_wait_seconds = max_retry_wait_seconds
        self.min_request_interval_seconds = min_request_interval_seconds
        self._pacing_lock = threading.Lock()
        self._next_request_at = 0.0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return await asyncio.to_thread(self._complete_sync, request)

    def _complete_sync(self, request: LLMRequest) -> LLMResponse:
        body = _REQUEST_BUILDERS[self.provider](
            model=self.model,
            request=request,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        endpoint = "/responses" if self.provider == "openai" else "/chat/completions"
        parser = (
            LLMResponse.from_responses_payload
            if self.provider == "openai"
            else LLMResponse.from_openai_payload
        )
        request_obj = urllib.request.Request(
            f"{self.api_base}{endpoint}",
            data=encoded,
            headers=headers,
            method="POST",
        )
        diagnostic_secrets = (self.api_key, *(
            message["content"] for message in request.messages
            if isinstance(message.get("content"), str)
        ))
        diagnostics = {
            "error_kind": "http_transport", "request_id": uuid.uuid4().hex,
            "endpoint": safe_endpoint(request_obj.full_url, diagnostic_secrets),
            "provider": self.provider, "model": self.model,
            "purpose": request.purpose,
            "agent_id": request.agent_id, "max_attempts": self.max_transient_retries + 1,
            "http_attempts": [], "network_attempts": [],
        }
        for attempt in range(self.max_transient_retries + 1):
            self._pace_request()
            try:
                with urllib.request.urlopen(request_obj, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("LLM API response must be a JSON object")
                result = parser(payload)
                if attempt:
                    self._log_retry("llm_retry_recovered", request, attempt + 1)
                return result
            except urllib.error.HTTPError as exc:
                failure = http_failure_detail(exc, secrets=diagnostic_secrets)
                failure.update(at=datetime.now(timezone.utc).isoformat(), attempt=attempt + 1)
                diagnostics["http_attempts"].append(failure)
                self._log_retry("llm_http_error", request, attempt + 1,
                                request_id=diagnostics["request_id"],
                                endpoint=diagnostics["endpoint"], model=self.model, **{
                                    key: value for key, value in failure.items()
                                    if key not in {"attempt", "at"}
                                })
                detail = failure["response_body"]
                if exc.code not in _RETRYABLE_HTTP_STATUSES:
                    diagnostics.update(final_attempt=attempt + 1, final_reason=f"HTTP {exc.code}")
                    raise LLMHTTPError(
                        f"LLM API returned HTTP {exc.code}: {detail}", diagnostics,
                    ) from exc
                self._retry_or_raise(
                    request, attempt, exc,
                    message=f"LLM API returned HTTP {exc.code}: {detail}",
                    reason=f"HTTP {exc.code}",
                    wait_seconds=self._transient_error_wait(exc, detail, attempt),
                    diagnostics=diagnostics,
                )
            except (OSError, http.client.IncompleteRead) as exc:
                # URLError is an OSError, but certificate/configuration errors are
                # permanent. Retry only transport failures, never a tool action.
                cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                message = redact(f"LLM API request failed: {cause}", diagnostic_secrets)
                retryable = _is_transient_network_error(cause)
                failure = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "attempt": attempt + 1, "error_type": type(cause).__name__,
                    "message": message[:16_384], "retryable": retryable,
                }
                diagnostics["network_attempts"].append(failure)
                self._log_retry(
                    "llm_network_error", request, attempt + 1,
                    request_id=diagnostics["request_id"], endpoint=diagnostics["endpoint"],
                    **{key: value for key, value in failure.items()
                       if key not in {"attempt", "at"}},
                )
                if not retryable:
                    diagnostics.update(final_attempt=attempt + 1,
                                       final_reason=type(cause).__name__)
                    raise LLMHTTPError(message, diagnostics) from exc
                self._retry_or_raise(
                    request, attempt, exc, message=message,
                    reason=type(cause).__name__,
                    wait_seconds=self._transient_error_wait(None, "", attempt),
                    diagnostics=diagnostics,
                )
        raise AssertionError("LLM retry loop exited without a result")

    def _retry_or_raise(
        self,
        request: LLMRequest,
        attempt: int,
        error: Exception,
        *,
        message: str,
        reason: str,
        wait_seconds: float,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        if attempt >= self.max_transient_retries:
            self._log_retry(
                "llm_retry_exhausted", request, attempt + 1, reason=reason,
            )
            message = f"{message} (after {attempt + 1} attempts)"
            if diagnostics is not None:
                diagnostics.update(final_attempt=attempt + 1, final_reason=reason)
                raise LLMHTTPError(message, diagnostics) from error
            raise RuntimeError(message) from error
        self._log_retry(
            "llm_retry", request, attempt + 1,
            reason=reason, wait_seconds=round(wait_seconds, 3),
            next_attempt=attempt + 2,
        )
        time.sleep(wait_seconds)

    def _log_retry(
        self, event: str, request: LLMRequest, attempt: int, **details: Any,
    ) -> None:
        # Workers capture stderr; HTTP details are bounded and redacted before here.
        _LOGGER.warning(json.dumps({
            "event": event,
            "at": datetime.now(timezone.utc).isoformat(),
            "purpose": request.purpose,
            "agent_id": request.agent_id,
            "attempt": attempt,
            "max_attempts": self.max_transient_retries + 1,
            **details,
        }, ensure_ascii=False))

    def _pace_request(self) -> None:
        if self.min_request_interval_seconds == 0:
            return
        with self._pacing_lock:
            now = time.monotonic()
            wait_seconds = max(0.0, self._next_request_at - now)
            if wait_seconds:
                time.sleep(wait_seconds)
            self._next_request_at = (
                time.monotonic() + self.min_request_interval_seconds
            )

    def _transient_error_wait(
        self,
        error: urllib.error.HTTPError | None,
        detail: str,
        attempt: int,
    ) -> float:
        # Saturate before exponentiation and add jitter to spread concurrent workers.
        wait_seconds = min(self.retry_backoff_seconds, self.max_retry_wait_seconds)
        for _ in range(attempt):
            wait_seconds = min(wait_seconds * 2, self.max_retry_wait_seconds)
            if wait_seconds >= self.max_retry_wait_seconds:
                break
        wait_seconds += random.uniform(0, wait_seconds * 0.25)
        retry_after = (
            error.headers.get("Retry-After") if error and error.headers else None
        )
        if retry_after:
            try:
                wait_seconds = max(wait_seconds, float(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    wait_seconds = max(
                        wait_seconds,
                        (retry_at - datetime.now(timezone.utc)).total_seconds(),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        try:
            payload = json.loads(detail)
            reset_time = (payload.get("error") or {}).get("reset_time")
            if reset_time:
                reset_at = datetime.fromisoformat(str(reset_time).replace("Z", "+00:00"))
                if reset_at.tzinfo is None:
                    reset_at = reset_at.replace(tzinfo=timezone.utc)
                until_reset = (reset_at - datetime.now(timezone.utc)).total_seconds()
                wait_seconds = max(wait_seconds, until_reset)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return min(max(0.0, wait_seconds), self.max_retry_wait_seconds)


def _is_transient_network_error(error: Any) -> bool:
    if isinstance(error, ssl.SSLCertVerificationError):
        return False
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    if isinstance(error, (
        TimeoutError, ConnectionError, http.client.IncompleteRead, ssl.SSLEOFError,
    )):
        return True
    return isinstance(error, OSError) and error.errno in {
        errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED,
        errno.EPIPE, errno.ENETRESET, errno.ENETDOWN, errno.ENETUNREACH,
        errno.EHOSTUNREACH,
        # Winsock codes can be exposed as errno by urllib on Windows.
        10050, 10051, 10052, 10053, 10054, 10060, 10061, 10064, 10065,
    }


__all__ = [
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "LLMCallManager",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "LLMToolCallParseError",
    "LLMTransport",
    "ManagedLLMClient",
    "OpenAICompatibleTransport",
    "response_finish_reason",
]
