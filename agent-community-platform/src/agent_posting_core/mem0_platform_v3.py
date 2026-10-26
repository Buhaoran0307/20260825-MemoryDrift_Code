from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import socket
import time
from copy import deepcopy
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from http.client import IncompleteRead
from typing import Any, Protocol, TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .memory import (
    MemoryContext,
    MemoryEvent,
    MemoryQuery,
    MemorySnapshot,
    _EventMemory,
)
from .memory_read_router import classify_cloud_failure

if TYPE_CHECKING:
    from .llm import LLMClient


MEM0_PLATFORM_API_BASE = "https://api.mem0.ai"
MEM0_PLATFORM_SNAPSHOT_SCHEMA_VERSION = 1

_ADD_PATH = "/v3/memories/add/"
_SEARCH_PATH = "/v3/memories/search/"
_SCOPE_SCHEME = "sha256_run_id_agent_id_v1"
_TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_EVENT_PENDING_STATUSES = frozenset({"PENDING", "RUNNING"})
_EVENT_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED"})
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_ERROR_EXCERPT_CHARS = 512
_ADD_MAX_RETRIES = 3


class Mem0PlatformError(RuntimeError):
    """Base error for the hosted Mem0 Platform adapter."""


class Mem0PlatformTransportError(Mem0PlatformError):
    """A request failed before a usable HTTP response was received."""


class Mem0PlatformHTTPError(Mem0PlatformError):
    """Mem0 Platform returned a non-successful HTTP response."""

    def __init__(self, message: str, *, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(message)


class Mem0PlatformUnknownCommitError(Mem0PlatformError):
    """An add request has no usable acknowledgement and may have committed."""


class Mem0PlatformAddRetriesExhausted(Mem0PlatformUnknownCommitError):
    """The initial add and all three permitted resends lacked acknowledgement."""


class Mem0PlatformEventError(Mem0PlatformError):
    """An asynchronous Mem0 add event failed or did not reach a terminal state."""


@dataclass(frozen=True)
class Mem0PlatformHTTPResponse:
    """Small transport-neutral HTTP response used by injectable test transports."""

    status_code: int
    body: Any = b""
    headers: Mapping[str, str] = field(default_factory=dict)


class Mem0PlatformTransport(Protocol):
    """Runtime-only transport contract; implementations may be sync or async."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Mem0PlatformHTTPResponse | Awaitable[Mem0PlatformHTTPResponse]: ...


class _StdlibTransport:
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Mem0PlatformHTTPResponse:
        return await asyncio.to_thread(
            self._request_sync,
            method,
            url,
            dict(headers),
            body,
            timeout,
        )

    @staticmethod
    def _request_sync(
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Mem0PlatformHTTPResponse:
        request = Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                response_body = _read_bounded(response)
                return Mem0PlatformHTTPResponse(
                    status_code=int(response.status),
                    body=response_body,
                    headers=dict(response.headers.items()),
                )
        except HTTPError as exc:
            try:
                response_body = _read_bounded(exc)
            finally:
                exc.close()
            return Mem0PlatformHTTPResponse(
                status_code=int(exc.code),
                body=response_body,
                headers=dict(exc.headers.items()) if exc.headers is not None else {},
            )
        except (URLError, TimeoutError, socket.timeout, OSError, IncompleteRead) as exc:
            raise Mem0PlatformTransportError(
                f"Mem0 Platform transport failed: {type(exc).__name__}"
            ) from exc


class Mem0PlatformV3Memory(_EventMemory):
    """Hosted Mem0 Platform V3 memory scoped to one run/agent identity.

    Construction and restore are deliberately offline. Credentials and injected
    runtime helpers remain private and never enter ``MemorySnapshot``. Retrieval
    after restore is a live remote read, not a frozen copy of Mem0's data.
    """

    strategy = "mem0_platform_v3"

    def __init__(
        self,
        *,
        agent_id: str,
        retrieval_top_k: int = 10,
        search_threshold: float = 0.1,
        rerank: bool = False,
        request_timeout_seconds: float = 30,
        event_timeout_seconds: float = 300,
        event_poll_interval_seconds: float = 1,
        max_transient_retries: int = 3,
        add_max_retries: int = _ADD_MAX_RETRIES,
        retry_backoff_seconds: float = 1,
        max_retry_wait_seconds: float = 30,
        custom_instructions: str = "",
        llm: "LLMClient | None" = None,
        runtime: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(agent_id=agent_id, llm=llm)
        self.retrieval_top_k = _bounded_int(
            retrieval_top_k,
            "mem0_platform_v3 retrieval_top_k",
            minimum=1,
            maximum=1_000,
        )
        self.search_threshold = _bounded_float(
            search_threshold,
            "mem0_platform_v3 search_threshold",
            minimum=0.0,
            maximum=1.0,
        )
        if not isinstance(rerank, bool):
            raise ValueError("mem0_platform_v3 rerank must be a boolean")
        self.rerank = rerank
        self.request_timeout_seconds = _positive_float(
            request_timeout_seconds,
            "mem0_platform_v3 request_timeout_seconds",
        )
        self.event_timeout_seconds = _positive_float(
            event_timeout_seconds,
            "mem0_platform_v3 event_timeout_seconds",
        )
        self.event_poll_interval_seconds = _positive_float(
            event_poll_interval_seconds,
            "mem0_platform_v3 event_poll_interval_seconds",
        )
        self.max_transient_retries = _bounded_int(
            max_transient_retries,
            "mem0_platform_v3 max_transient_retries",
            minimum=0,
        )
        self.add_max_retries = _bounded_int(
            add_max_retries, "mem0_platform_v3 add_max_retries", minimum=0,
        )
        self.retry_backoff_seconds = _positive_float(
            retry_backoff_seconds,
            "mem0_platform_v3 retry_backoff_seconds",
        )
        self.max_retry_wait_seconds = _positive_float(
            max_retry_wait_seconds,
            "mem0_platform_v3 max_retry_wait_seconds",
        )
        if not isinstance(custom_instructions, str):
            raise ValueError(
                "mem0_platform_v3 custom_instructions must be a string"
            )
        self.custom_instructions = custom_instructions.strip()
        if len(self.custom_instructions) > 4_000:
            raise ValueError(
                "mem0_platform_v3 custom_instructions cannot exceed 4000 characters"
            )

        runtime_values = _runtime_values(runtime)
        self._api_key = runtime_values.get("api_key")
        self._physical_scope = runtime_values.get("physical_scope")
        self._account_id = runtime_values.get("account_id")
        self._read_only = runtime_values.get("read_only", False)
        self._api_base = (runtime_values.get("api_base") or MEM0_PLATFORM_API_BASE).rstrip("/")
        self._run_id = runtime_values.get("run_id")
        self._transport = runtime_values.get("transport") or _StdlibTransport()
        self._sleep: Callable[[float], Any] = (
            runtime_values.get("sleep") or asyncio.sleep
        )
        self._monotonic: Callable[[], float] = (
            runtime_values.get("monotonic") or time.monotonic
        )
        self._scope_user_id = (
            _scope_user_id(self._run_id, self.agent_id)
            if self._run_id is not None
            else None
        )
        self._completed_add_events: list[dict[str, Any]] = []
        self._skipped_add_events: dict[str, dict[str, Any]] = {}

    @property
    def skipped_update_events(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(list(self._skipped_add_events.values())))

    async def initialize(self, events: list[MemoryEvent]) -> None:
        if self.revision == 0:
            await self.observe(events)

    async def observe(self, events: list[MemoryEvent]) -> None:
        if self._read_only:
            raise Mem0PlatformError("second-stage Mem0 memory is read-only")
        unseen: list[MemoryEvent] = []
        seen = set(self._event_ids) | self._skipped_add_events.keys()
        for event in events:
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            unseen.append(event)
        if not unseen:
            return

        user_id = self._require_scope()
        batch_digest = _event_batch_digest(unseen)
        body: dict[str, Any] = {
            "messages": [
                _event_message(event, self.agent_id) for event in unseen
            ],
            "user_id": user_id,
            "metadata": {
                "source": "agent_community",
                "source_event_ids": [event.event_id for event in unseen],
                "batch_digest": batch_digest,
                "run_agent_scope": user_id,
            },
            "infer": True,
        }
        if self.custom_instructions:
            body["custom_instructions"] = self.custom_instructions

        try:
            add_response = await self._request_add(body)
        except Mem0PlatformAddRetriesExhausted as exc:
            for event in unseen:
                self._skipped_add_events[event.event_id] = {
                    "source_event": event.as_dict(),
                    "batch_digest": batch_digest,
                    "attempts": self.add_max_retries + 1,
                    "retries": self.add_max_retries,
                    "error": str(exc),
                    "remote_state_may_have_changed": True,
                }
            return
        event_id = _required_string(add_response.get("event_id"), "add event_id")
        event_response = await self._poll_event(event_id)
        remembered = self._remember_events(unseen)
        self._completed_add_events.append(
            {
                "event_id": event_id,
                "status": "SUCCEEDED",
                "batch_digest": batch_digest,
                "source_event_ids": [event.event_id for event in remembered],
                "memory_ids": _event_memory_ids(event_response),
            }
        )

    async def retrieve(self, query: MemoryQuery) -> MemoryContext:
        query_text = str(query.text or "").strip()
        if not query_text:
            raise ValueError("mem0_platform_v3 retrieval query cannot be empty")
        limit = self.retrieval_top_k
        if query.max_items is not None:
            if isinstance(query.max_items, bool) or not isinstance(
                query.max_items, int
            ):
                raise ValueError("mem0_platform_v3 query max_items must be an integer")
            if query.max_items < 1:
                return MemoryContext(self.strategy, (), self.revision)
            limit = min(limit, query.max_items)

        response = await self._request_read(
            "POST",
            _SEARCH_PATH,
            {
                "query": query_text,
                "filters": {"user_id": self._physical_scope or self._require_scope()},
                "top_k": limit,
                "threshold": self.search_threshold,
                "rerank": self.rerank,
            },
        )
        results = response.get("results")
        if not isinstance(results, list):
            raise Mem0PlatformError(
                "Mem0 Platform search response must contain a results list"
            )
        items: list[MemoryEvent] = []
        for index, result in enumerate(results):
            if not isinstance(result, dict):
                raise Mem0PlatformError(
                    f"Mem0 Platform search result {index} must be an object"
                )
            memory_id = _required_string(
                result.get("id") or result.get("memory_id"),
                f"search result {index} id",
            )
            content = _required_string(
                result.get("memory"),
                f"search result {index} memory",
            )
            score = _optional_score(result.get("score"), index=index)
            remote_metadata = result.get("metadata")
            if remote_metadata is None:
                remote_metadata = {}
            if not isinstance(remote_metadata, dict):
                raise Mem0PlatformError(
                    f"Mem0 Platform search result {index} metadata must be an object"
                )
            categories = result.get("categories")
            if categories is None:
                categories = []
            if not isinstance(categories, list) or not all(
                isinstance(value, str) for value in categories
            ):
                raise Mem0PlatformError(
                    f"Mem0 Platform search result {index} categories must be strings"
                )
            items.append(
                MemoryEvent(
                    event_id=f"mem0_platform_v3:{memory_id}",
                    thread_id=query.thread_id,
                    kind="memory_fact",
                    actor_id=self.agent_id,
                    content=content,
                    created_at=str(
                        result.get("created_at")
                        or result.get("updated_at")
                        or "memory-derived"
                    ),
                    provenance="mem0_platform_v3",
                    visibility="retrieved",
                    metadata={
                        "memory_id": memory_id,
                        "score": score,
                        "categories": list(categories),
                        "remote_metadata": dict(remote_metadata),
                        "remote_live": True,
                    },
                )
            )
        # MemoryContext.render() consumes items from the end first. Mem0 returns
        # best-first, so store the response in reverse to keep the best match at
        # the truncation-preferred end of the context.
        items.reverse()
        return MemoryContext(self.strategy, tuple(items), self.revision)

    def snapshot(self) -> MemorySnapshot:
        return super().snapshot()

    def restore(self, snapshot: MemorySnapshot) -> None:
        before_events = list(self._events)
        before_event_ids = set(self._event_ids)
        before_revision = self.revision
        before_run_id = self._run_id
        before_scope = self._scope_user_id
        before_completed = [dict(item) for item in self._completed_add_events]
        before_skipped = deepcopy(self._skipped_add_events)
        try:
            if (
                isinstance(snapshot.revision, bool)
                or not isinstance(snapshot.revision, int)
                or snapshot.revision < 0
                or snapshot.revision != len(snapshot.events)
            ):
                raise ValueError(
                    "mem0_platform_v3 snapshot revision must equal its event count"
                )
            super().restore(snapshot)
            if self.snapshot().as_dict() != snapshot.as_dict():
                raise ValueError(
                    "mem0_platform_v3 snapshot is not in canonical round-trip form"
                )
        except BaseException:
            self._events = before_events
            self._event_ids = before_event_ids
            self.revision = before_revision
            self._run_id = before_run_id
            self._scope_user_id = before_scope
            self._completed_add_events = before_completed
            self._skipped_add_events = before_skipped
            raise

    def _snapshot_options(self) -> dict[str, Any]:
        return {
            "schema_version": MEM0_PLATFORM_SNAPSHOT_SCHEMA_VERSION,
            "provider": "mem0_platform",
            "api_version": "v3",
            "api_base": MEM0_PLATFORM_API_BASE,
            "remote_live": True,
            "scope": {
                "scheme": _SCOPE_SCHEME,
                "run_id": self._run_id,
                "agent_id": self.agent_id,
                "user_id": self._scope_user_id,
            },
            "retrieval_top_k": self.retrieval_top_k,
            "search_threshold": self.search_threshold,
            "rerank": self.rerank,
            "request_timeout_seconds": self.request_timeout_seconds,
            "event_timeout_seconds": self.event_timeout_seconds,
            "event_poll_interval_seconds": self.event_poll_interval_seconds,
            "max_transient_retries": self.max_transient_retries,
            **({"add_max_retries": self.add_max_retries}
               if self.add_max_retries != _ADD_MAX_RETRIES else {}),
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "max_retry_wait_seconds": self.max_retry_wait_seconds,
            "custom_instructions": self.custom_instructions,
            "completed_add_events": [
                {
                    "event_id": item["event_id"],
                    "status": item["status"],
                    "batch_digest": item["batch_digest"],
                    "source_event_ids": list(item["source_event_ids"]),
                    "memory_ids": list(item["memory_ids"]),
                }
                for item in self._completed_add_events
            ],
            # Omit when empty to preserve exact round trips for legacy snapshots.
            **({"skipped_add_events": list(self.skipped_update_events)}
               if self._skipped_add_events else {}),
        }

    def _restore_options(self, options: dict[str, Any]) -> None:
        expected_keys = {
            "schema_version",
            "provider",
            "api_version",
            "api_base",
            "remote_live",
            "scope",
            "retrieval_top_k",
            "search_threshold",
            "rerank",
            "request_timeout_seconds",
            "event_timeout_seconds",
            "event_poll_interval_seconds",
            "max_transient_retries",
            "retry_backoff_seconds",
            "max_retry_wait_seconds",
            "custom_instructions",
            "completed_add_events",
        }
        if not isinstance(options, dict):
            raise ValueError("mem0_platform_v3 snapshot options must be an object")
        if "skipped_add_events" in options:
            expected_keys.add("skipped_add_events")
        if "add_max_retries" in options:
            expected_keys.add("add_max_retries")
        saved_add_max_retries = options.get("add_max_retries", _ADD_MAX_RETRIES)
        if type(saved_add_max_retries) is not int or saved_add_max_retries != self.add_max_retries:
            raise ValueError("mem0_platform_v3 snapshot add_max_retries does not match configured value")
        if set(options) != expected_keys:
            unexpected = sorted(str(key) for key in set(options) - expected_keys)
            missing = sorted(expected_keys - set(options))
            raise ValueError(
                "mem0_platform_v3 snapshot options do not match schema "
                f"(missing={missing}, unexpected={unexpected})"
            )
        schema_version = options.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != MEM0_PLATFORM_SNAPSHOT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported mem0_platform_v3 snapshot schema_version")
        if options.get("provider") != "mem0_platform":
            raise ValueError("mem0_platform_v3 snapshot provider does not match")
        if options.get("api_version") != "v3":
            raise ValueError("mem0_platform_v3 snapshot API version does not match")
        if options.get("api_base") != MEM0_PLATFORM_API_BASE:
            raise ValueError("mem0_platform_v3 snapshot API base does not match")
        if options.get("remote_live") is not True:
            raise ValueError("mem0_platform_v3 snapshot must be marked remote_live")

        _require_snapshot_value(
            options, "retrieval_top_k", self.retrieval_top_k, integer=True
        )
        _require_snapshot_value(
            options, "search_threshold", self.search_threshold, number=True
        )
        _require_snapshot_value(options, "rerank", self.rerank, boolean=True)
        _require_snapshot_value(
            options,
            "request_timeout_seconds",
            self.request_timeout_seconds,
            number=True,
        )
        _require_snapshot_value(
            options,
            "event_timeout_seconds",
            self.event_timeout_seconds,
            number=True,
        )
        _require_snapshot_value(
            options,
            "event_poll_interval_seconds",
            self.event_poll_interval_seconds,
            number=True,
        )
        _require_snapshot_value(
            options,
            "max_transient_retries",
            self.max_transient_retries,
            integer=True,
        )
        _require_snapshot_value(
            options,
            "retry_backoff_seconds",
            self.retry_backoff_seconds,
            number=True,
        )
        _require_snapshot_value(
            options,
            "max_retry_wait_seconds",
            self.max_retry_wait_seconds,
            number=True,
        )
        _require_snapshot_value(
            options, "custom_instructions", self.custom_instructions, string=True
        )

        scope = options.get("scope")
        if not isinstance(scope, dict) or set(scope) != {
            "scheme",
            "run_id",
            "agent_id",
            "user_id",
        }:
            raise ValueError("mem0_platform_v3 snapshot scope is invalid")
        if scope.get("scheme") != _SCOPE_SCHEME:
            raise ValueError("mem0_platform_v3 snapshot scope scheme is unsupported")
        if scope.get("agent_id") != self.agent_id:
            raise ValueError("mem0_platform_v3 snapshot agent scope does not match")
        run_id = scope.get("run_id")
        user_id = scope.get("user_id")
        if run_id is None:
            if user_id is not None or self._events:
                raise ValueError("mem0_platform_v3 snapshot has an incomplete scope")
        else:
            run_id = _nonempty_string(run_id, "snapshot run_id")
            expected_user_id = _scope_user_id(run_id, self.agent_id)
            if user_id != expected_user_id:
                raise ValueError("mem0_platform_v3 snapshot scope hash does not match")

        completed = options.get("completed_add_events")
        if not isinstance(completed, list):
            raise ValueError(
                "mem0_platform_v3 completed_add_events must be a list"
            )
        available_event_ids = {event.event_id for event in self._events}
        restored_completed: list[dict[str, Any]] = []
        seen_remote_event_ids: set[str] = set()
        covered_source_event_ids: set[str] = set()
        for index, item in enumerate(completed):
            restored = _restore_completed_event(item, index=index)
            if restored["event_id"] in seen_remote_event_ids:
                raise ValueError(
                    "mem0_platform_v3 snapshot contains duplicate remote event ids"
                )
            if any(
                value not in available_event_ids
                for value in restored["source_event_ids"]
            ):
                raise ValueError(
                    "mem0_platform_v3 snapshot remote event refers to an unknown source"
                )
            if any(
                value in covered_source_event_ids
                for value in restored["source_event_ids"]
            ):
                raise ValueError(
                    "mem0_platform_v3 snapshot source event was uploaded more than once"
                )
            seen_remote_event_ids.add(restored["event_id"])
            covered_source_event_ids.update(restored["source_event_ids"])
            restored_completed.append(restored)
        if covered_source_event_ids != available_event_ids:
            raise ValueError(
                "mem0_platform_v3 snapshot does not account for every source event"
            )

        skipped = options.get("skipped_add_events", [])
        if not isinstance(skipped, list):
            raise ValueError("mem0_platform_v3 skipped_add_events must be a list")
        restored_skipped: dict[str, dict[str, Any]] = {}
        for item in skipped:
            if not isinstance(item, dict) or set(item) != {
                "source_event", "batch_digest", "attempts", "retries", "error",
                "remote_state_may_have_changed",
            }:
                raise ValueError("invalid mem0_platform_v3 skipped add record")
            event = MemoryEvent.from_dict(item["source_event"])
            if (event.event_id in available_event_ids or event.event_id in restored_skipped
                    or run_id is None or item["attempts"] != self.add_max_retries + 1
                    or item["retries"] != self.add_max_retries
                    or not isinstance(item["error"], str) or not item["error"]
                    or not isinstance(item["batch_digest"], str)
                    or len(item["batch_digest"]) != 64
                    or any(char not in "0123456789abcdef" for char in item["batch_digest"])
                    or item["remote_state_may_have_changed"] is not True):
                raise ValueError("invalid mem0_platform_v3 skipped add record")
            restored_skipped[event.event_id] = deepcopy(item)

        self._run_id = run_id
        self._scope_user_id = user_id
        self._completed_add_events = restored_completed
        self._skipped_add_events = restored_skipped

    async def _request_add(self, body: dict[str, Any]) -> dict[str, Any]:
        # This per-run budget is independent of event/search read retries.
        for attempt in range(self.add_max_retries + 1):
            try:
                return await self._request_add_once(body)
            except Mem0PlatformUnknownCommitError as exc:
                if attempt == self.add_max_retries:
                    raise Mem0PlatformAddRetriesExhausted(
                        "Mem0 Platform add received no usable acknowledgement after "
                        f"{attempt + 1} attempts ({self.add_max_retries} retries); source event skipped: {exc}"
                    ) from exc
                await self._sleep_before_retry(attempt, retry_after=None)
        raise AssertionError("unreachable add retry state")

    async def _request_add_once(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._request_once("POST", _ADD_PATH, body)
            if response.status_code < 200 or response.status_code >= 300:
                if response.status_code in {408, 500, 502, 503, 504}:
                    raise Mem0PlatformUnknownCommitError(
                        f"Mem0 Platform add returned ambiguous HTTP {response.status_code}"
                    )
                raise _http_error(
                    response, operation="add", redactions=(self._api_key,)
                )
            try:
                parsed = _response_object(response, operation="add")
                _required_string(parsed.get("event_id"), "add event_id")
                return parsed
            except Mem0PlatformError as exc:
                raise Mem0PlatformUnknownCommitError(
                    "Mem0 Platform add returned an unusable response or missing event_id"
                ) from exc
        except Mem0PlatformHTTPError:
            raise
        except Mem0PlatformUnknownCommitError:
            raise
        except Mem0PlatformTransportError as exc:
            raise Mem0PlatformUnknownCommitError(
                "Mem0 Platform add transport outcome is unknown: "
                + str(exc).replace(self._api_key or "\0", "[REDACTED]")[:512]
            ) from exc

    async def _poll_event(self, event_id: str) -> dict[str, Any]:
        deadline = _monotonic_value(self._monotonic) + self.event_timeout_seconds
        path = f"/v1/event/{quote(event_id, safe='')}/"
        while True:
            try:
                response = await self._request_read("GET", path, None)
            except Mem0PlatformHTTPError as exc:
                # A newly-created event can briefly be absent from the event read
                # replica. Retry only this poll path; a general read 404 remains
                # terminal. Once acknowledged, add itself is not replayed.
                if exc.status_code != 404:
                    raise
                remaining = deadline - _monotonic_value(self._monotonic)
                if remaining <= 0:
                    raise Mem0PlatformEventError(
                        "Mem0 Platform add event was not visible before the timeout"
                    ) from exc
                await _await_sleep(
                    self._sleep,
                    min(self.event_poll_interval_seconds, remaining),
                )
                continue
            status = _required_string(response.get("status"), "event status").upper()
            if status == "SUCCEEDED":
                return response
            if status == "FAILED":
                detail = _safe_error_detail(
                    response, redactions=(self._api_key,)
                )
                raise Mem0PlatformEventError(
                    f"Mem0 Platform add event failed{detail}"
                )
            if status not in _EVENT_PENDING_STATUSES:
                raise Mem0PlatformEventError(
                    f"Mem0 Platform add event returned unknown status {status!r}"
                )
            remaining = deadline - _monotonic_value(self._monotonic)
            if remaining <= 0:
                raise Mem0PlatformEventError(
                    "Mem0 Platform add event did not complete before the timeout"
                )
            await _await_sleep(
                self._sleep,
                min(self.event_poll_interval_seconds, remaining),
            )

    async def _request_read(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        last_transport_error: Mem0PlatformTransportError | None = None
        for attempt in range(self.max_transient_retries + 1):
            try:
                response = await self._request_once(method, path, body)
            except Mem0PlatformTransportError as exc:
                last_transport_error = exc
                if attempt >= self.max_transient_retries:
                    raise
                await self._sleep_before_retry(attempt, retry_after=None)
                continue
            if 200 <= response.status_code < 300:
                return _response_object(response, operation="read")
            if (
                response.status_code not in _TRANSIENT_STATUS_CODES
                or attempt >= self.max_transient_retries
                or classify_cloud_failure(response.status_code, response.body, response.headers)["category"] == "quota_exhausted"
            ):
                raise _http_error(
                    response, operation="read", redactions=(self._api_key,)
                )
            await self._sleep_before_retry(
                attempt,
                retry_after=_retry_after_seconds(response.headers),
            )
        if last_transport_error is not None:
            raise last_transport_error
        raise Mem0PlatformTransportError("Mem0 Platform read retry loop failed")

    async def _request_once(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> Mem0PlatformHTTPResponse:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Mem0 Platform request path must be relative to API base")
        api_key = self._require_api_key()
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Authorization": f"Token {api_key}",
        }
        encoded_body = None
        if body is not None:
            try:
                encoded_body = json.dumps(
                    body,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Mem0 Platform request body is not valid JSON"
                ) from exc
            headers["Content-Type"] = "application/json"

        request_callable = getattr(self._transport, "request", None)
        if request_callable is None and callable(self._transport):
            request_callable = self._transport
        if request_callable is None:
            raise TypeError(
                "mem0_platform_v3 runtime transport must be callable or expose "
                "request()"
            )
        try:
            result = request_callable(
                method,
                f"{self._api_base}{path}",
                headers=headers,
                body=encoded_body,
                timeout=self.request_timeout_seconds,
            )
            if inspect.isawaitable(result):
                result = await result
            return _coerce_transport_response(result)
        except Mem0PlatformTransportError:
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise Mem0PlatformTransportError(
                f"Mem0 Platform transport failed: {type(exc).__name__}"
            ) from exc

    async def _sleep_before_retry(
        self,
        attempt: int,
        *,
        retry_after: float | None,
    ) -> None:
        delay = self.retry_backoff_seconds * (2**attempt)
        if retry_after is not None:
            delay = max(delay, retry_after)
        delay = min(delay, self.max_retry_wait_seconds)
        await _await_sleep(self._sleep, delay)

    def _require_scope(self) -> str:
        if self._scope_user_id is None:
            raise ValueError(
                "mem0_platform_v3 requires runtime['run_id'] before remote access"
            )
        return self._scope_user_id

    def _require_api_key(self) -> str:
        if self._api_key is None:
            raise ValueError(
                "mem0_platform_v3 requires runtime['api_key'] before remote access"
            )
        return self._api_key


def build_mem0_platform_v3_memory(
    *,
    agent_id: str,
    llm: "LLMClient | None" = None,
    options: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> Mem0PlatformV3Memory:
    if options is not None and not isinstance(options, dict):
        raise ValueError("mem0_platform_v3 options must be an object")
    values = dict(options or {})
    allowed = {
        "retrieval_top_k",
        "search_threshold",
        "rerank",
        "request_timeout_seconds",
        "event_timeout_seconds",
        "event_poll_interval_seconds",
        "max_transient_retries",
        "add_max_retries",
        "retry_backoff_seconds",
        "max_retry_wait_seconds",
        "custom_instructions",
    }
    unknown = sorted(str(key) for key in set(values) - allowed)
    if unknown:
        raise ValueError(
            f"Unknown mem0_platform_v3 memory options: {unknown}"
        )
    return Mem0PlatformV3Memory(
        agent_id=agent_id,
        llm=llm,
        retrieval_top_k=values.get("retrieval_top_k", 10),
        search_threshold=values.get("search_threshold", 0.1),
        rerank=values.get("rerank", False),
        request_timeout_seconds=values.get("request_timeout_seconds", 30),
        event_timeout_seconds=values.get("event_timeout_seconds", 300),
        event_poll_interval_seconds=values.get("event_poll_interval_seconds", 1),
        max_transient_retries=values.get("max_transient_retries", 3),
        add_max_retries=values.get("add_max_retries", _ADD_MAX_RETRIES),
        retry_backoff_seconds=values.get("retry_backoff_seconds", 1),
        max_retry_wait_seconds=values.get("max_retry_wait_seconds", 30),
        custom_instructions=values.get("custom_instructions", ""),
        runtime=runtime,
    )


def _runtime_values(runtime: dict[str, Any] | None) -> dict[str, Any]:
    if runtime is not None and not isinstance(runtime, dict):
        raise ValueError("mem0_platform_v3 runtime must be an object")
    values = dict(runtime or {})
    allowed = {"api_key", "run_id", "transport", "sleep", "monotonic", "physical_scope", "read_only",
               "account_id", "api_base", "org_id", "project_id"}
    unknown = sorted(str(key) for key in set(values) - allowed)
    if unknown:
        raise ValueError(f"Unknown mem0_platform_v3 runtime values: {unknown}")
    for name in ("api_key", "run_id", "physical_scope", "account_id", "org_id", "project_id"):
        value = values.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"mem0_platform_v3 runtime {name} must be a string")
        values[name] = value.strip()
    if type(values.get("read_only", False)) is not bool:
        raise ValueError("mem0_platform_v3 runtime read_only must be boolean")
    if values.get("physical_scope") is not None and not values.get("read_only"):
        raise ValueError("physical Mem0 scope is allowed only for read-only retrieval")
    if values.get("api_base") is not None and (not isinstance(values["api_base"], str)
                                               or not values["api_base"].startswith(("https://", "http://"))):
        raise ValueError("Mem0 API base must be an HTTP URL")
    for name in ("sleep", "monotonic"):
        value = values.get(name)
        if value is not None and not callable(value):
            raise ValueError(f"mem0_platform_v3 runtime {name} must be callable")
    transport = values.get("transport")
    if transport is not None and not (
        callable(transport) or callable(getattr(transport, "request", None))
    ):
        raise ValueError(
            "mem0_platform_v3 runtime transport must be callable or expose request()"
        )
    return values


def _scope_user_id(run_id: str, agent_id: str) -> str:
    payload = json.dumps(
        {"agent_id": agent_id, "run_id": run_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _event_batch_digest(events: list[MemoryEvent]) -> str:
    payload = json.dumps(
        [event.as_dict() for event in events],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _event_message(event: MemoryEvent, agent_id: str) -> dict[str, str]:
    # Preserve the hosted API role contract; source attribution stays in snapshots.
    return {"role": "assistant", "content": event.content}


def _read_bounded(stream: Any) -> bytes:
    body = stream.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise Mem0PlatformTransportError(
            "Mem0 Platform response exceeded the maximum supported size"
        )
    return body


def _coerce_transport_response(value: Any) -> Mem0PlatformHTTPResponse:
    if isinstance(value, Mem0PlatformHTTPResponse):
        response = value
    elif isinstance(value, tuple) and len(value) == 3:
        response = Mem0PlatformHTTPResponse(
            status_code=value[0],
            headers=value[1],
            body=value[2],
        )
    elif isinstance(value, dict) and (
        "status_code" in value or "status" in value
    ):
        response = Mem0PlatformHTTPResponse(
            status_code=value.get("status_code", value.get("status")),
            headers=value.get("headers") or {},
            body=value.get("body", value.get("json", b"")),
        )
    else:
        raise Mem0PlatformTransportError(
            "Mem0 Platform transport returned an unsupported response"
        )
    if (
        isinstance(response.status_code, bool)
        or not isinstance(response.status_code, int)
        or response.status_code < 100
        or response.status_code > 599
    ):
        raise Mem0PlatformTransportError(
            "Mem0 Platform transport returned an invalid HTTP status"
        )
    if not isinstance(response.headers, Mapping):
        raise Mem0PlatformTransportError(
            "Mem0 Platform transport returned invalid headers"
        )
    return Mem0PlatformHTTPResponse(
        status_code=response.status_code,
        body=response.body,
        headers={str(key).lower(): str(item) for key, item in response.headers.items()},
    )


def _response_object(
    response: Mem0PlatformHTTPResponse,
    *,
    operation: str,
) -> dict[str, Any]:
    body = response.body
    if isinstance(body, dict):
        return dict(body)
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Mem0PlatformError(
                f"Mem0 Platform {operation} response is not UTF-8 JSON"
            ) from exc
    elif isinstance(body, str):
        text = body
    else:
        raise Mem0PlatformError(
            f"Mem0 Platform {operation} response body is not JSON"
        )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Mem0PlatformError(
            f"Mem0 Platform {operation} response is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise Mem0PlatformError(
            f"Mem0 Platform {operation} response must be a JSON object"
        )
    return value


def _http_error(
    response: Mem0PlatformHTTPResponse,
    *,
    operation: str,
    redactions: tuple[str | None, ...] = (),
) -> Mem0PlatformHTTPError:
    detail = _response_error_excerpt(response.body, redactions=redactions)
    suffix = f": {detail}" if detail else ""
    error = Mem0PlatformHTTPError(
        f"Mem0 Platform {operation} failed with HTTP {response.status_code}{suffix}",
        status_code=response.status_code,
    )
    error.cloud_failure = classify_cloud_failure(response.status_code, response.body, response.headers,
                                                  provider="mem0_platform_v3", redactions=redactions)
    return error


def _response_error_excerpt(
    body: Any, *, redactions: tuple[str | None, ...] = ()
) -> str:
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    elif isinstance(body, str):
        text = body
    else:
        try:
            text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return ""
    text = " ".join(text.split())
    for secret in redactions:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:_ERROR_EXCERPT_CHARS]


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    value = None
    for key, item in headers.items():
        if str(key).lower() == "retry-after":
            value = str(item).strip()
            break
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


async def _await_sleep(sleep: Callable[[float], Any], delay: float) -> None:
    result = sleep(delay)
    if inspect.isawaitable(result):
        await result


def _monotonic_value(monotonic: Callable[[], float]) -> float:
    value = monotonic()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("mem0_platform_v3 monotonic() must return a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("mem0_platform_v3 monotonic() must return a finite number")
    return result


def _event_memory_ids(response: dict[str, Any]) -> list[str]:
    results = response.get("results")
    if not isinstance(results, list):
        return []
    memory_ids: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        value = item.get("id") or item.get("memory_id")
        if isinstance(value, str) and value.strip() and value not in memory_ids:
            memory_ids.append(value.strip())
    return memory_ids


def _safe_error_detail(
    response: dict[str, Any], *, redactions: tuple[str | None, ...] = ()
) -> str:
    value = response.get("error") or response.get("message")
    if value is None:
        return ""
    detail = " ".join(str(value).split())
    for secret in redactions:
        if secret:
            detail = detail.replace(secret, "[REDACTED]")
    detail = detail[:_ERROR_EXCERPT_CHARS]
    return f": {detail}" if detail else ""


def _restore_completed_event(value: Any, *, index: int) -> dict[str, Any]:
    expected_keys = {
        "event_id",
        "status",
        "batch_digest",
        "source_event_ids",
        "memory_ids",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError(
            f"mem0_platform_v3 completed_add_events[{index}] is invalid"
        )
    event_id = _nonempty_string(value.get("event_id"), "remote event_id")
    if value.get("status") != "SUCCEEDED":
        raise ValueError("mem0_platform_v3 snapshot remote event is not successful")
    batch_digest = _nonempty_string(value.get("batch_digest"), "batch_digest")
    if len(batch_digest) != 64 or any(
        character not in "0123456789abcdef" for character in batch_digest
    ):
        raise ValueError("mem0_platform_v3 snapshot batch_digest is invalid")
    source_event_ids = _unique_string_list(
        value.get("source_event_ids"),
        "source_event_ids",
        require_nonempty=True,
    )
    memory_ids = _unique_string_list(value.get("memory_ids"), "memory_ids")
    return {
        "event_id": event_id,
        "status": "SUCCEEDED",
        "batch_digest": batch_digest,
        "source_event_ids": source_event_ids,
        "memory_ids": memory_ids,
    }


def _unique_string_list(
    value: Any,
    name: str,
    *,
    require_nonempty: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"mem0_platform_v3 snapshot {name} must be a list")
    restored = [_nonempty_string(item, name) for item in value]
    if len(restored) != len(set(restored)):
        raise ValueError(f"mem0_platform_v3 snapshot {name} contains duplicates")
    if require_nonempty and not restored:
        raise ValueError(f"mem0_platform_v3 snapshot {name} cannot be empty")
    return restored


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Mem0PlatformError(f"Mem0 Platform response {name} cannot be empty")
    return value.strip()


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"mem0_platform_v3 {name} cannot be empty")
    return value.strip()


def _optional_score(value: Any, *, index: int) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Mem0PlatformError(
            f"Mem0 Platform search result {index} score must be numeric"
        )
    score = float(value)
    if not math.isfinite(score):
        raise Mem0PlatformError(
            f"Mem0 Platform search result {index} score must be finite"
        )
    return score


def _bounded_int(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ValueError(f"{name} must be at least {minimum}")
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return result


def _positive_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _require_snapshot_value(
    options: dict[str, Any],
    name: str,
    expected: Any,
    *,
    integer: bool = False,
    number: bool = False,
    boolean: bool = False,
    string: bool = False,
) -> None:
    value = options.get(name)
    valid = True
    if integer:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif number:
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    elif boolean:
        valid = isinstance(value, bool)
    elif string:
        valid = isinstance(value, str)
    if not valid or value != expected:
        raise ValueError(
            f"mem0_platform_v3 snapshot {name} does not match configured value"
        )


__all__ = [
    "MEM0_PLATFORM_API_BASE",
    "MEM0_PLATFORM_SNAPSHOT_SCHEMA_VERSION",
    "Mem0PlatformError",
    "Mem0PlatformEventError",
    "Mem0PlatformHTTPError",
    "Mem0PlatformHTTPResponse",
    "Mem0PlatformTransport",
    "Mem0PlatformTransportError",
    "Mem0PlatformUnknownCommitError",
    "Mem0PlatformV3Memory",
    "build_mem0_platform_v3_memory",
]
