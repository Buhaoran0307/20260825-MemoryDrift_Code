from __future__ import annotations

import asyncio
import ast
import copy
import hashlib
import inspect
import json
import logging
import math
import re
import threading
from contextvars import ContextVar
from pathlib import Path
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, TYPE_CHECKING

from .llm import DEFAULT_LLM_TIMEOUT_SECONDS, LLMRequest, response_finish_reason
from .memory import (
    MemoryContext,
    MemoryEvent,
    MemoryQuery,
    MemorySnapshot,
    MemoryProviderResponseError,
    _EventMemory,
)

if TYPE_CHECKING:
    from .llm import LLMClient


LANGMEM_SDK_VERSION = "0.0.30"
LANGMEM_SNAPSHOT_SCHEMA_VERSION = 2
LANGMEM_LEGACY_SNAPSHOT_SCHEMA_VERSION = 1
LANGMEM_MEMORY_SCHEMA_LEGACY = "legacy_content_v1"
LANGMEM_MEMORY_SCHEMA_CATEGORIZED = "categorized_v1"
LANGMEM_DEFAULT_MEMORY_SCHEMA = LANGMEM_MEMORY_SCHEMA_CATEGORIZED
LOCAL_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
LOCAL_EMBEDDING_DIMENSIONS = 384
_LOCAL_EMBEDDING_LOCK = threading.Lock()


def uses_local_embeddings(model: str) -> bool:
    return model in {LOCAL_EMBEDDING_MODEL, f"sentence-transformers/{LOCAL_EMBEDDING_MODEL}"}


def _local_embeddings() -> Any:
    """Use the same offline Chroma/SentenceTransformer model and cache as A-Mem."""
    try:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        from langchain_core.embeddings import Embeddings

        with _LOCAL_EMBEDDING_LOCK:
            function = SentenceTransformerEmbeddingFunction(
                model_name=LOCAL_EMBEDDING_MODEL,
                cache_folder=str(Path(__file__).resolve().parents[2] / "data" / "models"),
                local_files_only=True,
            )
    except (ImportError, OSError) as exc:
        raise LangMemOfficialDependencyError(
            "LangMem local embeddings require A-Mem's chromadb/sentence-transformers "
            "runtime and cached all-MiniLM-L6-v2 model in data/models"
        ) from exc

    class LocalEmbeddings(Embeddings):
        _langmem_local_embeddings = True

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            if not texts:
                return []
            with _LOCAL_EMBEDDING_LOCK:
                vectors = function(texts)
            return [[float(value) for value in vector] for vector in vectors]

        def embed_query(self, text: str) -> list[float]:
            return self.embed_documents([text])[0]

    # Embeddings' async methods offload CPU inference to a worker thread.
    return LocalEmbeddings()

_MANAGER_API = "create_memory_store_manager"
_STORE_PROVIDER = "langgraph_in_memory"
_NAMESPACE_TEMPLATE = ("agent_community", "{user_id}")
_SCOPE_SCHEME = "sha256_run_id_agent_id_v1"
_SNAPSHOT_PAGE_SIZE = 1_000
_MAX_MEMORY_RECORDS = 10_000
_MAX_MEMORY_KEY_CHARS = 512
_MAX_MEMORY_TEXT_CHARS = 100_000
_MAX_IMPORTANT_FACT_CHARS = 1_000
_MAX_DETAIL_ITEMS = 20
_MAX_DETAIL_ITEM_CHARS = 1_000
_MAX_REASONING_PROCESS_CHARS = 2_000
_MAX_BACKGROUND_CHARS = 2_000
_PATCH_TARGET_MAX_RETRIES = 2
_AUTO_TOOL_CHOICE_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
_LOGGER = logging.getLogger(__name__)

_CATEGORIZED_MEMORY_INSTRUCTIONS = """Manage long-term memory as atomic, durable facts.

Create one memory record per important fact. Separate the record into these fields:
- important_fact: the single durable fact that is useful for future behavior and retrieval.
- relationships: concise relationship details that support the fact.
- preferences: concise preference details that support the fact.
- reasoning_process: a concise summary of explicit evidence or rationale, not hidden chain-of-thought.
- background: only the minimum background needed to interpret the fact.

Ignore transient, routine, or unimportant details. Do not duplicate the important fact in the
other fields. Consolidate redundant records and update contradicted or outdated records. Keep
every field concise. Only important_fact is indexed for semantic retrieval. The complete
structured memory, including all supporting fields, is exposed to the downstream agent."""


class LangMemOfficialError(RuntimeError):
    """Base error for the official LangMem SDK adapter."""


class LangMemOfficialResponseError(MemoryProviderResponseError, LangMemOfficialError):
    """Preserve rejected one-attempt model output in reading artifacts."""


_PATCH_FAILURES: ContextVar[list[LangMemOfficialError] | None] = ContextVar(
    "langmem_patch_failures", default=None,
)


def _patch_validation_error(message: str) -> LangMemOfficialError:
    error = LangMemOfficialError(message)
    failures = _PATCH_FAILURES.get()
    if failures is not None and not failures:
        failures.append(error)
    return error


class LangMemOfficialDependencyError(LangMemOfficialError):
    """The pinned official LangMem runtime is unavailable or incompatible."""


class LangMemManager(Protocol):
    def ainvoke(
        self,
        value: dict[str, Any],
        *,
        config: dict[str, Any],
    ) -> Awaitable[Any] | Any: ...

    def asearch(
        self,
        *,
        query: str | None,
        limit: int,
        offset: int = 0,
        config: dict[str, Any],
    ) -> Awaitable[list[Any]] | list[Any]: ...

    def aput(
        self,
        key: str,
        value: dict[str, Any],
        *,
        config: dict[str, Any],
    ) -> Awaitable[Any] | Any: ...


ManagerFactory = Callable[..., LangMemManager | Awaitable[LangMemManager]]


class LangMemOfficialMemory(_EventMemory):
    """Official LangMem 0.0.30 manager backed by a snapshotted LangGraph store.

    Construction and restore are deliberately offline. The LangGraph store is
    materialized lazily from JSON-compatible snapshot records before the first
    write or semantic search. LangMem's model calls use the platform LLM client;
    embeddings default to A-Mem's cached local model. Explicit remote embedding
    models continue to use the configured OpenAI-compatible endpoint.
    Like the other memory adapters, each instance belongs to one event loop;
    restore explicitly rejects overlap with an active async memory operation.
    """

    strategy = "langmem_official"

    def __init__(
        self,
        *,
        agent_id: str,
        retrieval_top_k: int = 10,
        query_limit: int = 5,
        max_steps: int = 1,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
        enable_deletes: bool = False,
        custom_instructions: str = "",
        memory_schema: str | None = None,
        patch_target_max_retries: int = _PATCH_TARGET_MAX_RETRIES,
        trustcall_max_attempts: int = 3,
        llm: "LLMClient | None" = None,
        runtime: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(agent_id=agent_id, llm=llm)
        self.retrieval_top_k = _bounded_int(
            retrieval_top_k,
            "langmem_official retrieval_top_k",
            minimum=1,
            maximum=1_000,
        )
        self.query_limit = _bounded_int(
            query_limit,
            "langmem_official query_limit",
            minimum=4,
            maximum=1_000,
        )
        self.max_steps = _bounded_int(
            max_steps,
            "langmem_official max_steps",
            minimum=1,
            maximum=10,
        )
        self.patch_target_max_retries = _bounded_int(
            patch_target_max_retries, "langmem_official patch_target_max_retries",
            minimum=0, maximum=100,
        )
        self.trustcall_max_attempts = _bounded_int(
            trustcall_max_attempts, "langmem_official trustcall_max_attempts",
            minimum=1, maximum=100,
        )
        self._embedding_model_explicit = embedding_model is not None
        self._embedding_dimensions_explicit = embedding_dimensions is not None
        self.embedding_model = _bounded_string(
            LOCAL_EMBEDDING_MODEL if embedding_model is None else embedding_model,
            "langmem_official embedding_model",
            maximum=256,
        )
        if embedding_dimensions is None:
            embedding_dimensions = (
                LOCAL_EMBEDDING_DIMENSIONS if uses_local_embeddings(self.embedding_model) else 1_536
            )
        self.embedding_dimensions = _bounded_int(
            embedding_dimensions,
            "langmem_official embedding_dimensions",
            minimum=1,
            maximum=65_536,
        )
        if uses_local_embeddings(self.embedding_model) and self.embedding_dimensions != 384:
            raise ValueError("all-MiniLM-L6-v2 requires embedding_dimensions=384")
        if not isinstance(enable_deletes, bool):
            raise ValueError("langmem_official enable_deletes must be a boolean")
        self.enable_deletes = enable_deletes
        if not isinstance(custom_instructions, str):
            raise ValueError("langmem_official custom_instructions must be a string")
        self.custom_instructions = custom_instructions.strip()
        if len(self.custom_instructions) > 4_000:
            raise ValueError(
                "langmem_official custom_instructions cannot exceed 4000 characters"
            )
        self._memory_schema_explicit = memory_schema is not None
        self.memory_schema = _normalize_memory_schema(
            LANGMEM_DEFAULT_MEMORY_SCHEMA if memory_schema is None else memory_schema
        )
        self._snapshot_schema_version = LANGMEM_SNAPSHOT_SCHEMA_VERSION

        runtime_values = _runtime_values(runtime)
        self._api_base = runtime_values.get("api_base")
        self._api_key = runtime_values.get("api_key")
        self._model = runtime_values.get("model")
        self._run_id = runtime_values.get("run_id")
        self._request_timeout_seconds = runtime_values.get(
            "request_timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS
        )
        self._max_retries = runtime_values.get("max_retries", 2)
        self._memory_max_tokens = runtime_values.get("memory_max_tokens", 8_000)
        self._manager_factory: ManagerFactory | None = runtime_values.get(
            "manager_factory"
        )
        self._scope_user_id = (
            _scope_user_id(self._run_id, self.agent_id)
            if self._run_id is not None
            else None
        )
        self._memories: list[dict[str, Any]] = []
        self._manager: LangMemManager | None = None
        self._retired_managers: list[LangMemManager] = []
        self._operation_lock = asyncio.Lock()

    async def initialize(self, events: list[MemoryEvent]) -> None:
        if self.revision == 0:
            await self.observe(events)

    async def observe(self, events: list[MemoryEvent]) -> None:
        async with self._operation_lock:
            unseen: list[MemoryEvent] = []
            seen = set(self._event_ids)
            for event in events:
                if event.event_id in seen:
                    continue
                seen.add(event.event_id)
                unseen.append(event)
            if not unseen:
                return
            manager = await self._ensure_manager()
            previous = copy.deepcopy(self._memories)
            failures: list[LangMemOfficialError] = []
            failure_token = _PATCH_FAILURES.set(failures)
            try:
                manager_result = await _maybe_await(manager.ainvoke(
                    {
                        "messages": [_event_message(event) for event in unseen],
                        "max_steps": self.max_steps,
                    },
                    config=self._manager_config(),
                ))
                # Trustcall catches model exceptions internally. Preserve this
                # invocation's terminal validation error across its child tasks.
                if failures:
                    raise failures[0]
                updated = await self._read_all_memories(manager, previous=previous)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                await self._discard_manager(manager)
                raise
            except Exception as exc:
                # The official manager commits store writes at the end of an update,
                # but a multi-put can still fail partway through. Discard the runtime
                # store and rebuild from the last durable snapshot on the next call.
                await self._discard_manager(manager)
                cause = failures[0] if failures else exc
                raise _provider_error("update", cause, self._api_key) from cause
            finally:
                _PATCH_FAILURES.reset(failure_token)
            self._memories = updated
            self._remember_events(unseen)
            from .memory_attribution_probe import probe_receipt
            probe_receipt(self.llm, "manager_result", result=manager_result)

    async def retrieve(self, query: MemoryQuery) -> MemoryContext:
        query_text = str(query.text or "").strip()
        if not query_text:
            raise ValueError("langmem_official retrieval query cannot be empty")
        limit = self.retrieval_top_k
        if query.max_items is not None:
            if isinstance(query.max_items, bool) or not isinstance(query.max_items, int):
                raise ValueError("langmem_official query max_items must be an integer")
            if query.max_items < 1:
                return MemoryContext(self.strategy, (), self.revision)
            limit = min(limit, query.max_items)
        if not self._memories:
            return MemoryContext(self.strategy, (), self.revision)

        async with self._operation_lock:
            manager = await self._ensure_manager()
            try:
                results = await _maybe_await(manager.asearch(
                    query=query_text,
                    limit=limit,
                    config=self._manager_config(),
                ))
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except Exception as exc:
                raise _provider_error("search", exc, self._api_key) from exc
        if not isinstance(results, list):
            raise LangMemOfficialError("LangMem official search did not return a list")

        from langmem.utils import dumps

        durable = {record["key"]: record for record in self._memories}
        items: list[MemoryEvent] = []
        for index, result in enumerate(results):
            key = _item_key(result, index=index)
            value = _item_value(result, index=index)
            wrapped = _snapshot_value(
                value,
                index=index,
                memory_schema=self.memory_schema,
            )
            # Match the official search tool's payload: serialize the entire
            # SearchItem, rather than projecting value.content.important_fact.
            # The platform's existing ordering and context budget still apply.
            content = dumps(result.dict())
            score = _optional_score(_item_attribute(result, "score"), index=index)
            record = durable.get(key) or {}
            created_at = str(
                record.get("updated_at")
                or record.get("created_at")
                or _item_timestamp(result, "updated_at")
                or _item_timestamp(result, "created_at")
                or "memory-derived"
            )
            items.append(MemoryEvent(
                event_id=f"langmem_official:{key}",
                thread_id=query.thread_id,
                kind="langmem_memory",
                actor_id=self.agent_id,
                content=content,
                created_at=created_at,
                provenance="langmem_official",
                visibility="retrieved",
                metadata={
                    "memory_id": key,
                    "memory_kind": str(wrapped["kind"]),
                    "score": score,
                    "provider": "langmem",
                    "sdk_version": LANGMEM_SDK_VERSION,
                    "memory_schema": self.memory_schema,
                    "output_field": "search_item",
                    "delivery_format": "langmem_search_item_json_v1",
                },
            ))
        # MemoryContext.render() truncates from the end. LangMem returns best-first,
        # so reverse the response and keep the strongest result at the preferred end.
        items.reverse()
        return MemoryContext(self.strategy, tuple(items), self.revision)

    async def aclose(self) -> None:
        async with self._operation_lock:
            manager = self._manager
            if manager is not None:
                self._retire_manager(manager)
            if self._manager_factory is None:
                await self._drain_retired_managers(raise_on_error=True)

    def restore(self, snapshot: MemorySnapshot) -> None:
        if self._operation_lock.locked():
            raise RuntimeError(
                "langmem_official cannot restore while another memory operation is active"
            )
        before_events = list(self._events)
        before_event_ids = set(self._event_ids)
        before_revision = self.revision
        before_memories = copy.deepcopy(self._memories)
        before_run_id = self._run_id
        before_scope = self._scope_user_id
        before_manager = self._manager
        before_embedding = (self.embedding_model, self.embedding_dimensions)
        before_memory_schema = self.memory_schema
        before_snapshot_schema_version = self._snapshot_schema_version
        try:
            if (
                isinstance(snapshot.revision, bool)
                or not isinstance(snapshot.revision, int)
                or snapshot.revision < 0
                or snapshot.revision != len(snapshot.events)
            ):
                raise ValueError(
                    "langmem_official snapshot revision must equal its event count"
                )
            super().restore(snapshot)
            if self.snapshot().as_dict() != snapshot.as_dict():
                raise ValueError(
                    "langmem_official snapshot is not in canonical round-trip form"
                )
        except BaseException:
            self._events = before_events
            self._event_ids = before_event_ids
            self.revision = before_revision
            self._memories = before_memories
            self._run_id = before_run_id
            self._scope_user_id = before_scope
            self._manager = before_manager
            self.embedding_model, self.embedding_dimensions = before_embedding
            self.memory_schema = before_memory_schema
            self._snapshot_schema_version = before_snapshot_schema_version
            raise
        if (
            before_manager is not None
            and self._manager is not before_manager
            and self._manager_factory is None
        ):
            # restore() is synchronous, so defer async HTTP-client cleanup until
            # the next operation or aclose().
            self._retire_manager(before_manager)

    def _snapshot_options(self) -> dict[str, Any]:
        options = {
            "schema_version": self._snapshot_schema_version,
            "provider": "langmem",
            "sdk_version": LANGMEM_SDK_VERSION,
            "manager_api": _MANAGER_API,
            "store_provider": _STORE_PROVIDER,
            "state_frozen": True,
            "embedding_recomputed_on_restore": True,
            "namespace_template": list(_NAMESPACE_TEMPLATE),
            "scope": {
                "scheme": _SCOPE_SCHEME,
                "run_id": self._run_id,
                "agent_id": self.agent_id,
                "user_id": self._scope_user_id,
            },
            "retrieval_top_k": self.retrieval_top_k,
            "query_limit": self.query_limit,
            "max_steps": self.max_steps,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "enable_deletes": self.enable_deletes,
            "custom_instructions": self.custom_instructions,
            "memories": copy.deepcopy(self._memories),
        }
        if self._snapshot_schema_version >= LANGMEM_SNAPSHOT_SCHEMA_VERSION:
            options["memory_schema"] = self.memory_schema
        for name, default in (("patch_target_max_retries", _PATCH_TARGET_MAX_RETRIES),
                              ("trustcall_max_attempts", 3)):
            if getattr(self, name) != default:
                options[name] = getattr(self, name)
        return options

    def _restore_options(self, options: dict[str, Any]) -> None:
        base_keys = {
            "schema_version",
            "provider",
            "sdk_version",
            "manager_api",
            "store_provider",
            "state_frozen",
            "embedding_recomputed_on_restore",
            "namespace_template",
            "scope",
            "retrieval_top_k",
            "query_limit",
            "max_steps",
            "embedding_model",
            "embedding_dimensions",
            "enable_deletes",
            "custom_instructions",
            "memories",
        }
        if not isinstance(options, dict):
            raise ValueError("langmem_official snapshot options must be an object")
        saved_schema_version = options.get("schema_version")
        if saved_schema_version == LANGMEM_LEGACY_SNAPSHOT_SCHEMA_VERSION:
            expected_keys = base_keys
            saved_memory_schema = LANGMEM_MEMORY_SCHEMA_LEGACY
        elif saved_schema_version == LANGMEM_SNAPSHOT_SCHEMA_VERSION:
            expected_keys = base_keys | {"memory_schema"}
            saved_memory_schema = _normalize_memory_schema(options.get("memory_schema"))
        else:
            raise ValueError(
                "langmem_official snapshot schema_version must be 1 or "
                f"{LANGMEM_SNAPSHOT_SCHEMA_VERSION}"
            )
        retry_defaults = {"patch_target_max_retries": _PATCH_TARGET_MAX_RETRIES,
                          "trustcall_max_attempts": 3}
        expected_keys |= set(options) & retry_defaults.keys()
        for name, default in retry_defaults.items():
            if type(options.get(name, default)) is not int or options.get(name, default) != getattr(self, name):
                raise ValueError(f"langmem_official snapshot {name} does not match configured value")
        if set(options) != expected_keys:
            missing = sorted(expected_keys - set(options))
            unexpected = sorted(str(key) for key in set(options) - expected_keys)
            raise ValueError(
                "langmem_official snapshot options do not match schema "
                f"(missing={missing}, unexpected={unexpected})"
            )
        expected_constants = {
            "schema_version": saved_schema_version,
            "provider": "langmem",
            "sdk_version": LANGMEM_SDK_VERSION,
            "manager_api": _MANAGER_API,
            "store_provider": _STORE_PROVIDER,
            "state_frozen": True,
            "embedding_recomputed_on_restore": True,
            "namespace_template": list(_NAMESPACE_TEMPLATE),
        }
        for name, expected in expected_constants.items():
            if options.get(name) != expected:
                raise ValueError(
                    f"langmem_official snapshot {name} does not match {expected!r}"
                )
        for name in (
            "retrieval_top_k",
            "query_limit",
            "max_steps",
            "embedding_model",
            "embedding_dimensions",
            "enable_deletes",
            "custom_instructions",
        ):
            if name == "embedding_model" and not self._embedding_model_explicit:
                continue
            if name == "embedding_dimensions" and not self._embedding_dimensions_explicit:
                continue
            if options.get(name) != getattr(self, name):
                raise ValueError(
                    f"langmem_official snapshot {name} does not match configured "
                    f"{getattr(self, name)!r}"
                )
        if self._memory_schema_explicit and saved_memory_schema != self.memory_schema:
            raise ValueError(
                "langmem_official snapshot memory_schema does not match configured "
                f"{self.memory_schema!r}"
            )

        scope = options.get("scope")
        if not isinstance(scope, dict) or set(scope) != {
            "scheme", "run_id", "agent_id", "user_id"
        }:
            raise ValueError("langmem_official snapshot scope is invalid")
        if scope.get("scheme") != _SCOPE_SCHEME:
            raise ValueError("langmem_official snapshot scope scheme is invalid")
        if scope.get("agent_id") != self.agent_id:
            raise ValueError("langmem_official snapshot belongs to another agent scope")
        run_id = scope.get("run_id")
        user_id = scope.get("user_id")
        if run_id is None:
            if user_id is not None:
                raise ValueError("langmem_official snapshot scope is incomplete")
            if options.get("memories"):
                raise ValueError(
                    "langmem_official snapshot with memories must include a run scope"
                )
        else:
            run_id = _bounded_string(run_id, "langmem_official snapshot run_id", 512)
            expected_user_id = _scope_user_id(run_id, self.agent_id)
            if user_id != expected_user_id:
                raise ValueError("langmem_official snapshot user scope is invalid")
            if self._run_id is not None and self._run_id != run_id:
                raise ValueError("langmem_official snapshot belongs to another run scope")

        saved_model = _bounded_string(options["embedding_model"], "embedding_model", 256)
        saved_dimensions = _bounded_int(
            options["embedding_dimensions"], "embedding_dimensions", minimum=1, maximum=65_536
        )
        if uses_local_embeddings(saved_model) and saved_dimensions != LOCAL_EMBEDDING_DIMENSIONS:
            raise ValueError("all-MiniLM-L6-v2 requires embedding_dimensions=384")
        memories = _validated_memories(
            options.get("memories"), memory_schema=saved_memory_schema
        )
        self.embedding_model, self.embedding_dimensions = saved_model, saved_dimensions
        self.memory_schema = saved_memory_schema
        self._snapshot_schema_version = saved_schema_version
        self._run_id = run_id
        self._scope_user_id = user_id
        self._memories = memories
        self._manager = None

    async def _ensure_manager(self) -> LangMemManager:
        if self._manager_factory is None and self._retired_managers:
            await self._drain_retired_managers(raise_on_error=False)
        if self._manager is not None:
            return self._manager
        self._require_scope()
        manager: LangMemManager | None = None
        try:
            manager = (
                await _maybe_await(self._manager_factory(
                    llm=self.llm,
                    agent_id=self.agent_id,
                    api_base=self._api_base,
                    api_key=self._api_key,
                    model=self._model,
                    request_timeout_seconds=self._request_timeout_seconds,
                    max_retries=self._max_retries,
                    memory_max_tokens=self._memory_max_tokens,
                    embedding_model=self.embedding_model,
                    embedding_dimensions=self.embedding_dimensions,
                    enable_deletes=self.enable_deletes,
                    query_limit=self.query_limit,
                    custom_instructions=self.custom_instructions,
                    memory_schema=self.memory_schema,
                    patch_target_max_retries=self.patch_target_max_retries,
                    trustcall_max_attempts=self.trustcall_max_attempts,
                    namespace=_NAMESPACE_TEMPLATE,
                ))
                if self._manager_factory is not None
                else _create_official_manager(
                    llm=self.llm,
                    agent_id=self.agent_id,
                    api_base=self._api_base,
                    api_key=self._api_key,
                    model=self._model,
                    request_timeout_seconds=self._request_timeout_seconds,
                    max_retries=self._max_retries,
                    memory_max_tokens=self._memory_max_tokens,
                    embedding_model=self.embedding_model,
                    embedding_dimensions=self.embedding_dimensions,
                    enable_deletes=self.enable_deletes,
                    query_limit=self.query_limit,
                    custom_instructions=self.custom_instructions,
                    memory_schema=self.memory_schema,
                    patch_target_max_retries=self.patch_target_max_retries,
                    trustcall_max_attempts=self.trustcall_max_attempts,
                )
            )
            _validate_manager(manager)
            for record in self._memories:
                await _maybe_await(manager.aput(
                    record["key"],
                    copy.deepcopy(record["value"]),
                    config=self._manager_config(),
                ))
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            if manager is not None and self._manager_factory is None:
                self._retire_manager(manager)
                await self._drain_retired_managers(raise_on_error=False)
            raise
        except Exception as exc:
            if manager is not None and self._manager_factory is None:
                self._retire_manager(manager)
                await self._drain_retired_managers(raise_on_error=False)
            raise _provider_error("initialization", exc, self._api_key) from exc
        self._manager = manager
        return manager

    async def _discard_manager(self, manager: LangMemManager) -> None:
        self._retire_manager(manager)
        if self._manager_factory is None:
            await self._drain_retired_managers(raise_on_error=False)

    def _retire_manager(self, manager: LangMemManager) -> None:
        if self._manager is manager:
            self._manager = None
        if self._manager_factory is None and all(
            manager is not existing for existing in self._retired_managers
        ):
            self._retired_managers.append(manager)

    async def _drain_retired_managers(self, *, raise_on_error: bool) -> None:
        failed = 0
        index = 0
        while index < len(self._retired_managers):
            manager = self._retired_managers[index]
            closed = await _close_official_manager(manager)
            if closed:
                self._retired_managers.pop(index)
            else:
                failed += 1
                index += 1
        if failed and raise_on_error:
            raise LangMemOfficialError(
                f"failed to close {failed} LangMem embedding client set(s)"
            )

    async def _read_all_memories(
        self,
        manager: LangMemManager,
        *,
        previous: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        previous_by_key = {record["key"]: record for record in previous}
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await _maybe_await(manager.asearch(
                query=None,
                limit=_SNAPSHOT_PAGE_SIZE,
                offset=offset,
                config=self._manager_config(),
            ))
            if not isinstance(page, list):
                raise LangMemOfficialError(
                    "LangMem official store enumeration did not return a list"
                )
            for index, item in enumerate(page, start=offset):
                key = _item_key(item, index=index)
                value = _snapshot_value(
                    _item_value(item, index=index),
                    index=index,
                    memory_schema=self.memory_schema,
                )
                old = previous_by_key.get(key)
                unchanged = old is not None and old.get("value") == value
                created_at = (
                    str(old["created_at"])
                    if old is not None
                    else _item_timestamp(item, "created_at") or "memory-derived"
                )
                updated_at = (
                    str(old["updated_at"])
                    if unchanged
                    else _item_timestamp(item, "updated_at") or created_at
                )
                records.append({
                    "key": key,
                    "value": value,
                    "created_at": created_at,
                    "updated_at": updated_at,
                })
                if len(records) > _MAX_MEMORY_RECORDS:
                    raise LangMemOfficialError(
                        "LangMem official store exceeds the 10000-record snapshot limit"
                    )
            if len(page) < _SNAPSHOT_PAGE_SIZE:
                break
            offset += len(page)
        return _validated_memories(
            sorted(records, key=lambda item: item["key"]),
            memory_schema=self.memory_schema,
        )

    def _manager_config(self) -> dict[str, Any]:
        return {"configurable": {"user_id": self._require_scope(),
                                 "max_attempts": self.trustcall_max_attempts}}

    def _require_scope(self) -> str:
        if self._scope_user_id is None:
            raise ValueError("langmem_official runtime requires a non-empty run_id")
        return self._scope_user_id


def build_langmem_official_memory(
    *,
    agent_id: str,
    llm: "LLMClient | None" = None,
    options: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> LangMemOfficialMemory:
    values = dict(options or {})
    retrieval_top_k = _option_int(
        values, "retrieval_top_k", alias="top_k", default=10, maximum=1_000
    )
    query_limit = _option_int(
        values, "query_limit", default=5, minimum=4, maximum=1_000
    )
    max_steps = _option_int(values, "max_steps", default=1, maximum=10)
    patch_target_max_retries = _option_int(
        values, "patch_target_max_retries", default=_PATCH_TARGET_MAX_RETRIES,
        minimum=0, maximum=100,
    )
    trustcall_max_attempts = _option_int(
        values, "trustcall_max_attempts", default=3, maximum=100,
    )
    embedding_model = _option_string(
        values, "embedding_model", default=LOCAL_EMBEDDING_MODEL, maximum=256
    )
    embedding_dimensions = _option_int(
        values, "embedding_dimensions",
        default=LOCAL_EMBEDDING_DIMENSIONS if uses_local_embeddings(embedding_model) else 1_536,
        maximum=65_536
    )
    enable_deletes = values.pop("enable_deletes", False)
    custom_instructions = values.pop("custom_instructions", "")
    memory_schema = values.pop("memory_schema", None)
    if values:
        raise ValueError(
            f"Unknown langmem_official memory options: {sorted(values)}"
        )
    memory = LangMemOfficialMemory(
        agent_id=agent_id,
        llm=llm,
        retrieval_top_k=retrieval_top_k,
        query_limit=query_limit,
        max_steps=max_steps,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        enable_deletes=enable_deletes,
        custom_instructions=custom_instructions,
        memory_schema=memory_schema,
        patch_target_max_retries=patch_target_max_retries,
        trustcall_max_attempts=trustcall_max_attempts,
        runtime=runtime,
    )
    # Old snapshots may have relied on the former remote defaults. Restore their
    # saved embedding settings only when the caller did not explicitly select them.
    memory._embedding_model_explicit = "embedding_model" in (options or {})
    memory._embedding_dimensions_explicit = "embedding_dimensions" in (options or {})
    return memory


def _categorized_memory_schema() -> type[Any]:
    """Build the official Pydantic schema lazily with the LangMem dependency."""
    try:
        from pydantic import BaseModel, ConfigDict, Field
    except ImportError as exc:
        raise LangMemOfficialDependencyError(
            "langmem_official categorized memory requires pydantic"
        ) from exc

    class CategorizedMemory(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        important_fact: str = Field(
            min_length=1,
            max_length=_MAX_IMPORTANT_FACT_CHARS,
            description=(
                "One atomic, durable, important fact indexed for semantic retrieval "
                "and shown to the downstream agent together with all supporting fields."
            ),
        )
        relationships: list[str] = Field(
            default_factory=list,
            max_length=_MAX_DETAIL_ITEMS,
            description="Concise relationship details supporting the important fact.",
        )
        preferences: list[str] = Field(
            default_factory=list,
            max_length=_MAX_DETAIL_ITEMS,
            description="Concise preference details supporting the important fact.",
        )
        reasoning_process: str = Field(
            default="",
            max_length=_MAX_REASONING_PROCESS_CHARS,
            description=(
                "Concise explicit evidence or rationale; never hidden chain-of-thought."
            ),
        )
        background: str = Field(
            default="",
            max_length=_MAX_BACKGROUND_CHARS,
            description="Minimum background needed to interpret the important fact.",
        )

    return CategorizedMemory


def _categorized_memory_instructions(custom_instructions: str) -> str:
    if not custom_instructions:
        return _CATEGORIZED_MEMORY_INSTRUCTIONS
    return (
        _CATEGORIZED_MEMORY_INSTRUCTIONS
        + "\n\nAdditional application instructions:\n"
        + custom_instructions
    )


def _create_official_manager(
    *,
    llm: "LLMClient | None",
    agent_id: str,
    api_base: str | None,
    api_key: str | None,
    model: str | None,
    request_timeout_seconds: float,
    max_retries: int,
    memory_max_tokens: int,
    embedding_model: str,
    embedding_dimensions: int,
    enable_deletes: bool,
    query_limit: int,
    custom_instructions: str,
    memory_schema: str,
    patch_target_max_retries: int = _PATCH_TARGET_MAX_RETRIES,
    trustcall_max_attempts: int = 3,
) -> LangMemManager:
    local = uses_local_embeddings(embedding_model)
    if not local:
        api_base = _bounded_string(api_base, "langmem_official runtime api_base", 2_048)
        api_key = _bounded_string(api_key, "langmem_official runtime api_key", 16_384)
    # Frozen local retrieval needs no chat endpoint or credentials.
    model = model or "platform-model"
    try:
        from importlib.metadata import version

        from langchain_openai import OpenAIEmbeddings
        from langgraph.store.memory import InMemoryStore
        from langmem import create_memory_store_manager
    except (ImportError, ModuleNotFoundError) as exc:
        raise LangMemOfficialDependencyError(
            "langmem_official requires the pinned langmem==0.0.30 dependency"
        ) from exc
    installed = version("langmem")
    if installed != LANGMEM_SDK_VERSION:
        raise LangMemOfficialDependencyError(
            "langmem_official requires langmem==0.0.30; "
            f"installed version is {installed}"
        )

    chat_model = _platform_chat_model(
        llm=llm,
        agent_id=agent_id,
        model_name=model,
        max_tokens=memory_max_tokens,
        patch_target_max_retries=patch_target_max_retries,
        trustcall_max_attempts=trustcall_max_attempts,
    )
    embedding_kwargs: dict[str, Any] = {
        "model": embedding_model,
        "api_key": api_key,
        "base_url": api_base.rstrip("/") if api_base else None,
        "request_timeout": request_timeout_seconds,
        "max_retries": max_retries,
        "check_embedding_ctx_length": False,
    }
    # OpenAI only accepts the optional `dimensions` request parameter for the
    # text-embedding-3 family. Keep the configured value as the LangGraph index
    # dimension for every provider, but do not send an unsupported parameter to
    # older or third-party embedding models.
    if _embedding_api_supports_dimensions(embedding_model):
        embedding_kwargs["dimensions"] = embedding_dimensions
    embeddings = (
        _local_embeddings() if local else OpenAIEmbeddings(**embedding_kwargs)
    )
    index_config: dict[str, Any] = {
        "dims": embedding_dimensions,
        "embed": embeddings,
    }
    if memory_schema == LANGMEM_MEMORY_SCHEMA_CATEGORIZED:
        # Only the durable fact affects semantic retrieval. The remaining fields
        # remain available to LangMem while it consolidates and updates records.
        index_config["fields"] = ["content.important_fact"]
    store = InMemoryStore(index=index_config)
    kwargs: dict[str, Any] = {
        "store": store,
        "namespace": _NAMESPACE_TEMPLATE,
        "query_limit": query_limit,
        "enable_inserts": True,
        "enable_deletes": enable_deletes,
    }
    if memory_schema == LANGMEM_MEMORY_SCHEMA_CATEGORIZED:
        kwargs["schemas"] = [_categorized_memory_schema()]
        kwargs["instructions"] = _categorized_memory_instructions(custom_instructions)
    elif custom_instructions:
        kwargs["instructions"] = custom_instructions
    return create_memory_store_manager(chat_model, **kwargs)


def _platform_chat_model(
    *,
    llm: "LLMClient | None",
    agent_id: str,
    model_name: str,
    max_tokens: int,
    patch_target_max_retries: int = _PATCH_TARGET_MAX_RETRIES,
    trustcall_max_attempts: int = 3,
) -> Any:
    try:
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain_core.utils.function_calling import convert_to_openai_tool
    except (ImportError, ModuleNotFoundError) as exc:
        raise LangMemOfficialDependencyError(
            "langmem_official requires LangChain's official chat model contract"
        ) from exc

    class PlatformChatModel(BaseChatModel):
        platform_client: Any = None
        platform_agent_id: str
        platform_model_name: str
        platform_max_tokens: int
        platform_patch_target_max_retries: int = _PATCH_TARGET_MAX_RETRIES
        platform_trustcall_max_attempts: int = 3

        @property
        def _llm_type(self) -> str:
            return "agent_community_managed_llm"

        @property
        def _identifying_params(self) -> dict[str, Any]:
            return {"model_name": self.platform_model_name}

        def bind_tools(
            self,
            tools: Any,
            *,
            tool_choice: Any = None,
            strict: bool | None = None,
            parallel_tool_calls: bool | None = None,
            **kwargs: Any,
        ) -> Any:
            formatted = []
            for tool in tools:
                if strict is None:
                    formatted.append(convert_to_openai_tool(tool))
                else:
                    formatted.append(convert_to_openai_tool(tool, strict=strict))
            bound: dict[str, Any] = {"tools": formatted}
            if tool_choice is not None:
                bound["tool_choice"] = _normalize_tool_choice(tool_choice)
            if parallel_tool_calls is not None:
                bound["parallel_tool_calls"] = parallel_tool_calls
            bound.update(kwargs)
            return self.bind(**bound)

        def _generate(
            self,
            messages: list[Any],
            stop: list[str] | None = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(self._agenerate(
                    messages, stop=stop, run_manager=None, **kwargs
                ))
            raise RuntimeError(
                "synchronous LangMem model execution is unavailable inside an event loop"
            )

        async def _agenerate(
            self,
            messages: list[Any],
            stop: list[str] | None = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> Any:
            failures = _PATCH_FAILURES.get()
            if failures:
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])
            if self.platform_client is None:
                raise ValueError(
                    "langmem_official memory updates require the platform LLM client"
                )
            tools = [_project_tool(tool) for tool in kwargs.get("tools") or []]
            request_messages = [_openai_message(message) for message in messages]
            try:
                patch_ids = _constrain_patch_targets(request_messages, tools)
            except LangMemOfficialError:
                if failures is None:
                    raise
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])
            for attempt in range(self.platform_patch_target_max_retries + 1):
                request = LLMRequest(
                    messages=copy.deepcopy(request_messages),
                    tools=tools,
                    tool_choice=_compatible_tool_choice(
                        kwargs.get("tool_choice"), self.platform_model_name,
                    ),
                    parallel_tool_calls=kwargs.get("parallel_tool_calls"),
                    purpose="memory",
                    agent_id=self.platform_agent_id,
                    temperature=0.0,
                    max_tokens=self.platform_max_tokens,
                )
                from .memory_attribution_probe import probe_receipt
                probe_receipt(self.platform_client, "call_context", request=request,
                              stage="langmem_model_call", attempt=attempt + 1,
                              tool_names=[tool["name"] for tool in tools])
                response = await self.platform_client.complete(request)
                sdk_tool_calls = []
                patch_compatibility = []
                for call in response.tool_calls:
                    arguments, adjusted = _trustcall_patch_arguments(call.name, call.arguments)
                    sdk_tool_calls.append({
                        "name": call.name, "args": arguments,
                        "id": call.call_id, "type": "tool_call",
                    })
                    if adjusted:
                        patch_compatibility.append({
                            "tool_call_id": call.call_id, "tool_name": call.name,
                            "patch_indexes": adjusted,
                            "original_arguments": copy.deepcopy(call.arguments),
                            "adjustment": "remove_missing_value_to_null",
                        })
                invalid = [
                    call for call in response.tool_calls
                    if call.name == "PatchDoc"
                    and (
                        not isinstance(call.arguments.get("json_doc_id"), str)
                        or call.arguments["json_doc_id"] not in patch_ids
                    )
                ]
                if not invalid:
                    if self.platform_trustcall_max_attempts == 1:
                        # Trustcall drops invalid calls when its repair budget is
                        # exhausted. In one-attempt runs this is a failed update,
                        # not a successful no-change result. Keep the raw output.
                        from jsonschema import ValidationError, validate

                        schemas = {tool["name"]: tool["input_schema"] for tool in tools}
                        try:
                            for call in sdk_tool_calls:
                                if call["name"] not in schemas:
                                    raise ValueError(f"unknown memory tool {call['name']!r}")
                                validate(call["args"], schemas[call["name"]])
                            if patch_compatibility:
                                _validate_compatible_patch_updates(
                                    request_messages, sdk_tool_calls, patch_compatibility, schemas,
                                )
                        except (ValueError, ValidationError) as exc:
                            error = LangMemOfficialResponseError(
                                "LangMem response failed schema validation with repairs disabled",
                                request=request, response=response,
                                parsed_content=json.dumps([
                                    {"name": call.name, "arguments": call.arguments}
                                    for call in response.tool_calls
                                ], ensure_ascii=False),
                            )
                            error.diagnostics.update(
                                strategy="langmem_official", attempt=1, max_attempts=1,
                                validation_error={"type": type(exc).__name__, "message": str(exc)},
                            )
                            if patch_compatibility:
                                error.diagnostics["trustcall_patch_compatibility"] = copy.deepcopy(patch_compatibility)
                            if failures is None:
                                raise error from exc
                            failures.append(error)
                            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])
                    break
                # Trustcall 0.0.39 logs and drops unknown PatchDoc targets. Reject
                # them before its teardown so no event is silently acknowledged.
                if attempt == self.platform_patch_target_max_retries:
                    message = (
                        "LangMem PatchDoc returned an unknown json_doc_id after "
                        f"{attempt + 1} attempts; no memory update was accepted"
                    )
                    if self.platform_trustcall_max_attempts == 1:
                        error = LangMemOfficialResponseError(
                            message, request=request, response=response,
                            parsed_content=json.dumps([
                                {"name": call.name, "arguments": call.arguments}
                                for call in response.tool_calls
                            ], ensure_ascii=False),
                        )
                        error.diagnostics.update(
                            strategy="langmem_official", attempt=attempt + 1,
                            max_attempts=self.platform_patch_target_max_retries + 1,
                            validation_error={"type": "UnknownPatchTarget", "message": message},
                        )
                        if patch_compatibility:
                            error.diagnostics["trustcall_patch_compatibility"] = copy.deepcopy(patch_compatibility)
                        if failures is not None:
                            failures.append(error)
                    else:
                        error = _patch_validation_error(message)
                    if failures is None:
                        raise error
                    # Let Trustcall finish without its exception-retry loop.
                    # observe() raises the recorded error and discards the store.
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])
                _LOGGER.warning(
                    "langmem_patch_target_retry agent_id=%s attempt=%s invalid_calls=%s",
                    self.platform_agent_id, attempt + 1, len(invalid),
                )
                assistant_message = {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
                # DashScope requires the prior thinking trace to be sent back
                # separately when a tool-call turn is retried.
                if response.reasoning_content:
                    assistant_message["reasoning_content"] = response.reasoning_content
                request_messages.append(assistant_message)
                # No call in this response has executed, including valid inserts.
                # Pair every call with a result so mixed batches remain valid chat.
                for call in response.tool_calls:
                    request_messages.append({
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": (
                            "This entire batch was rejected before execution because "
                            "a PatchDoc json_doc_id was not in the existing instances. "
                            "Resubmit all intended changes. For PatchDoc, copy an ID "
                            "exactly from its json_doc_id enum; to insert a new memory, "
                            "use Memory instead. Allowed patch IDs: "
                            + json.dumps(sorted(patch_ids))
                        ),
                    })
            response_metadata = {
                "model_name": self.platform_model_name,
                "finish_reason": response_finish_reason(response),
            }
            if patch_compatibility:
                # The platform response and its persisted HTTP/model-call audit
                # retain the exact model arguments. Only the SDK-facing copy is
                # adapted; record that deterministic compatibility step as well.
                response_metadata["trustcall_patch_compatibility"] = patch_compatibility
                # Workers preserve WARNING-level records even without explicit
                # logging configuration; this is an audit event, not a failure.
                _LOGGER.warning(
                    "langmem_patch_compatibility agent_id=%s adjustments=%s",
                    self.platform_agent_id,
                    json.dumps(patch_compatibility, ensure_ascii=False),
                )
            message = AIMessage(
                content=response.content,
                tool_calls=sdk_tool_calls,
                response_metadata=response_metadata,
                additional_kwargs=(
                    {"reasoning_content": response.reasoning_content}
                    if response.reasoning_content else {}
                ),
            )
            probe_receipt(self.platform_client, "validated", request=request)
            return ChatResult(
                generations=[ChatGeneration(message=message)],
                llm_output=response.raw if isinstance(response.raw, dict) else {},
            )

    return PlatformChatModel(
        platform_client=llm,
        platform_agent_id=agent_id,
        platform_model_name=model_name,
        platform_max_tokens=max_tokens,
        platform_patch_target_max_retries=patch_target_max_retries,
        platform_trustcall_max_attempts=trustcall_max_attempts,
    )


def _trustcall_patch_arguments(
    tool_name: str, arguments: dict[str, Any],
) -> tuple[dict[str, Any], list[int]]:
    """Adapt only remove's absent, unused value for Trustcall 0.0.39.

    Its JsonPatch model requires value even for remove, contrary to its own
    description/examples and JSON Patch semantics. An SDK-only null placeholder
    satisfies that model and is ignored by jsonpatch's remove operation. Keep
    add/replace and all other fields unchanged, and never mutate model output.
    """
    result = copy.deepcopy(arguments)
    adjusted = []
    if tool_name in {"PatchDoc", "PatchFunctionErrors"}:
        patches = result.get("patches")
        if isinstance(patches, list):
            for index, patch in enumerate(patches):
                if isinstance(patch, dict) and patch.get("op") == "remove" and "value" not in patch:
                    patch["value"] = None
                    adjusted.append(index)
    return result, adjusted


def _validate_compatible_patch_updates(
    messages: list[dict[str, Any]], calls: list[dict[str, Any]],
    adjustments: list[dict[str, Any]], schemas: dict[str, dict[str, Any]],
) -> None:
    """Do not let newly accepted remove patches be silently dropped by the SDK.

    Trustcall catches patch-application errors and filters invalid updated docs.
    Dry-run the adjusted update against its SDK-provided target and schema so
    invalid paths or removal of required fields still fail with raw diagnostics.
    """
    from jsonschema import validate
    from trustcall._base import _apply_patch

    targets = {}
    for message in messages:
        content = message.get("content")
        if message.get("role") != "system" or not isinstance(content, str):
            continue
        _, marker, block = content.rpartition("\n<existing>\n")
        if marker and block.rstrip().endswith("</existing>"):
            for target_id, schema_name, value in re.findall(
                r'^<instance id=([^\s>]+) schema_type="([^"\n]+)">\n(.*?)\n</instance>$',
                block, flags=re.MULTILINE | re.DOTALL,
            ):
                try:
                    targets[target_id] = (schema_name, ast.literal_eval(value))
                except (ValueError, SyntaxError) as exc:
                    raise ValueError("LangMem adjusted remove target cannot be decoded") from exc
    adjusted_ids = {item["tool_call_id"] for item in adjustments if item["tool_name"] == "PatchDoc"}
    for call in calls:
        if call["id"] not in adjusted_ids:
            continue
        arguments = call["args"]
        if arguments["json_doc_id"] not in targets:
            raise ValueError("LangMem adjusted remove target is unavailable")
        schema_name, target = targets[arguments["json_doc_id"]]
        if schema_name not in schemas:
            raise ValueError("LangMem adjusted remove target schema is unavailable")
        try:
            patched = _apply_patch(copy.deepcopy(target), arguments["patches"])
        except Exception as exc:
            raise ValueError(f"LangMem adjusted remove patch cannot be applied: {exc}") from exc
        validate(patched, schemas[schema_name])


def _embedding_api_supports_dimensions(model: str) -> bool:
    model_name = model.rsplit("/", 1)[-1].lower()
    return model_name.startswith("text-embedding-3")


async def _close_official_manager(manager: Any) -> bool:
    """Close only HTTP clients owned by this adapter's OpenAIEmbeddings."""
    try:
        store = getattr(manager, "store", None)
        embeddings = getattr(store, "embeddings", None)
    except Exception:
        return False
    if embeddings is None:
        return False
    if getattr(embeddings, "_langmem_local_embeddings", False) is True:
        # The process-wide SentenceTransformer cache has no owned HTTP clients.
        return True
    roots: list[Any] = []
    for resource_name in ("client", "async_client"):
        try:
            resource = getattr(embeddings, resource_name, None)
            root = getattr(resource, "_client", None)
        except Exception:
            continue
        if root is not None and all(root is not existing for existing in roots):
            roots.append(root)
    if not roots:
        return False
    succeeded = True
    for root in roots:
        try:
            close = getattr(root, "close", None)
            if not callable(close):
                succeeded = False
                continue
            result = close()
            if inspect.isawaitable(result):
                await result
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except Exception:
            # Cleanup must never hide the provider or cancellation error which
            # caused the manager to be discarded.
            succeeded = False
    return succeeded


def _openai_message(message: Any) -> dict[str, Any]:
    message_type = str(getattr(message, "type", ""))
    content = getattr(message, "content", "")
    if message_type == "human":
        return {"role": "user", "content": content}
    if message_type == "system":
        return {"role": "system", "content": content}
    if message_type == "tool":
        return {
            "role": "tool",
            "content": content,
            "tool_call_id": str(getattr(message, "tool_call_id", "")),
        }
    if message_type == "function":
        return {
            "role": "function",
            "name": str(getattr(message, "name", "")),
            "content": content,
        }
    if message_type == "chat":
        return {"role": str(getattr(message, "role", "user")), "content": content}
    value: dict[str, Any] = {"role": "assistant", "content": content}
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, Mapping) and additional_kwargs.get("reasoning_content"):
        value["reasoning_content"] = additional_kwargs["reasoning_content"]
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        value["tool_calls"] = [
            {
                "id": str(call.get("id") or f"langmem-call-{index + 1}"),
                "type": "function",
                "function": {
                    "name": str(call.get("name") or ""),
                    "arguments": json.dumps(
                        call.get("args") or {}, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            }
            for index, call in enumerate(tool_calls)
            if isinstance(call, dict)
        ]
    return value


def _project_tool(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("LangMem bound tool must be an OpenAI function schema")
    function = value.get("function") if value.get("type") == "function" else value
    if not isinstance(function, Mapping):
        raise TypeError("LangMem bound tool function must be an object")
    name = function.get("name")
    parameters = function.get("parameters", function.get("input_schema", {}))
    if not isinstance(name, str) or not name.strip() or not isinstance(parameters, dict):
        raise ValueError("LangMem bound tool schema is invalid")
    result = {
        "name": name,
        "description": str(function.get("description") or ""),
        "input_schema": copy.deepcopy(parameters),
    }
    strict = function.get("strict", value.get("strict"))
    if isinstance(strict, bool):
        result["strict"] = strict
    return result


def _constrain_patch_targets(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]],
) -> set[str]:
    """Constrain PatchDoc using the pinned SDK's generated existing-instance block.

    Trustcall appends this block to the system message. Instance bodies are dict
    reprs (embedded newlines are escaped); only standalone headers are IDs. Do
    not infer targets from user/assistant text or the full, unfiltered store.
    """
    patch_tools = [tool for tool in tools if tool["name"] == "PatchDoc"]
    if not patch_tools:
        return set()
    ids: set[str] = set()
    for message in messages:
        content = message.get("content")
        if message.get("role") != "system" or not isinstance(content, str):
            continue
        _, marker, block = content.rpartition("\n<existing>\n")
        if marker and block.rstrip().endswith("</existing>"):
            ids.update(re.findall(
                r'^<instance id=([^\s>]+) schema_type="[^"\n]+">$',
                block, flags=re.MULTILINE,
            ))
    if not ids:
        raise _patch_validation_error(
            "LangMem PatchDoc existing-instance IDs are unavailable; "
            "refusing an unvalidated memory update"
        )
    for tool in patch_tools:
        tool["input_schema"]["properties"]["json_doc_id"]["enum"] = sorted(ids)
    return ids


def _normalize_tool_choice(value: Any) -> Any:
    if value is False:
        return None
    if value is True or value == "any" or value == "required":
        return "auto"
    if isinstance(value, str):
        if value in {"auto", "none"}:
            return value
        if not value.strip():
            raise ValueError("LangMem tool_choice cannot be empty")
        return {"type": "function", "function": {"name": value}}
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    raise TypeError("LangMem tool_choice must be a string, mapping, or true")


def _compatible_tool_choice(value: Any, model_name: str) -> Any:
    if value is None:
        return None
    choice = _normalize_tool_choice(value)
    if (model_name.strip().lower() in _AUTO_TOOL_CHOICE_MODELS
            and isinstance(choice, Mapping) and choice.get("type") == "function"):
        # Thinking mode accepts auto, but not a forced function.
        # Keep the SDK's tools and response validation intact; only change the
        # provider's selection policy on the request sent to the platform LLM.
        return "auto"
    return choice


def _event_message(event: MemoryEvent) -> dict[str, str]:
    return {
        "role": "assistant",
        "content": event.content,
    }


def _snapshot_value(
    value: Any,
    *,
    index: int,
    memory_schema: str,
) -> dict[str, Any]:
    if hasattr(value, "model_dump") and callable(value.model_dump):
        kind_method = getattr(value, "__repr_name__", None)
        kind = kind_method() if callable(kind_method) else type(value).__name__
        content = value.model_dump(mode="json")
        result = {"kind": kind, "content": content}
    elif isinstance(value, dict):
        result = copy.deepcopy(value)
    else:
        raise LangMemOfficialError(
            f"LangMem official memory {index} has an unsupported value type"
        )
    _validate_memory_value(result, index=index, memory_schema=memory_schema)
    return result


def _validated_memories(
    value: Any,
    *,
    memory_schema: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("langmem_official snapshot memories must be a list")
    if len(value) > _MAX_MEMORY_RECORDS:
        raise ValueError(
            "langmem_official snapshot memories exceed the 10000-record limit"
        )
    result: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, record in enumerate(value):
        if not isinstance(record, dict) or set(record) != {
            "key", "value", "created_at", "updated_at"
        }:
            raise ValueError(
                f"langmem_official snapshot memory {index} has an invalid schema"
            )
        key = _bounded_string(
            record.get("key"),
            f"langmem_official snapshot memory {index} key",
            _MAX_MEMORY_KEY_CHARS,
        )
        if key in keys:
            raise ValueError("langmem_official snapshot contains duplicate memory keys")
        keys.add(key)
        memory_value = copy.deepcopy(record.get("value"))
        _validate_memory_value(
            memory_value, index=index, memory_schema=memory_schema
        )
        created_at = _bounded_string(
            record.get("created_at"),
            f"langmem_official snapshot memory {index} created_at",
            256,
        )
        updated_at = _bounded_string(
            record.get("updated_at"),
            f"langmem_official snapshot memory {index} updated_at",
            256,
        )
        result.append({
            "key": key,
            "value": memory_value,
            "created_at": created_at,
            "updated_at": updated_at,
        })
    if [item["key"] for item in result] != sorted(item["key"] for item in result):
        raise ValueError("langmem_official snapshot memories must be sorted by key")
    return result


def _validate_memory_value(
    value: Any,
    *,
    index: int,
    memory_schema: str,
) -> None:
    if not isinstance(value, dict) or set(value) != {"kind", "content"}:
        raise ValueError(
            f"langmem_official snapshot memory {index} value must contain kind and content"
        )
    content = value.get("content")
    if memory_schema == LANGMEM_MEMORY_SCHEMA_LEGACY:
        if value.get("kind") != "Memory":
            raise ValueError(
                f"langmem_official snapshot memory {index} must use the default Memory schema"
            )
        if not isinstance(content, dict) or set(content) != {"content"}:
            raise ValueError(
                f"langmem_official snapshot memory {index} content schema is invalid"
            )
        text = content.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                f"langmem_official snapshot memory {index} content must be non-empty"
            )
        if len(text) > _MAX_MEMORY_TEXT_CHARS:
            raise ValueError(
                f"langmem_official snapshot memory {index} content exceeds 100000 characters"
            )
    elif memory_schema == LANGMEM_MEMORY_SCHEMA_CATEGORIZED:
        _validate_categorized_memory_value(value, content=content, index=index)
    else:  # Defensive: callers normalize the mode before reaching this boundary.
        raise ValueError(f"Unsupported langmem_official memory_schema: {memory_schema!r}")
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"langmem_official snapshot memory {index} is not JSON-compatible"
        ) from exc


def _validate_categorized_memory_value(
    value: dict[str, Any],
    *,
    content: Any,
    index: int,
) -> None:
    if value.get("kind") != "CategorizedMemory":
        raise ValueError(
            f"langmem_official snapshot memory {index} must use CategorizedMemory"
        )
    expected_fields = {
        "important_fact",
        "relationships",
        "preferences",
        "reasoning_process",
        "background",
    }
    if not isinstance(content, dict) or set(content) != expected_fields:
        raise ValueError(
            f"langmem_official snapshot memory {index} categorized content schema is invalid"
        )
    _bounded_string(
        content.get("important_fact"),
        f"langmem_official snapshot memory {index} important_fact",
        _MAX_IMPORTANT_FACT_CHARS,
    )
    for field_name in ("relationships", "preferences"):
        values = content.get(field_name)
        if not isinstance(values, list) or len(values) > _MAX_DETAIL_ITEMS:
            raise ValueError(
                f"langmem_official snapshot memory {index} {field_name} must be a list "
                f"with at most {_MAX_DETAIL_ITEMS} items"
            )
        for item_index, item in enumerate(values):
            _bounded_string(
                item,
                (
                    f"langmem_official snapshot memory {index} {field_name} "
                    f"item {item_index}"
                ),
                _MAX_DETAIL_ITEM_CHARS,
            )
    for field_name, maximum in (
        ("reasoning_process", _MAX_REASONING_PROCESS_CHARS),
        ("background", _MAX_BACKGROUND_CHARS),
    ):
        text = content.get(field_name)
        if not isinstance(text, str):
            raise ValueError(
                f"langmem_official snapshot memory {index} {field_name} must be a string"
            )
        if len(text) > maximum:
            raise ValueError(
                f"langmem_official snapshot memory {index} {field_name} cannot exceed "
                f"{maximum} characters"
            )


def _item_attribute(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _item_key(item: Any, *, index: int) -> str:
    return _bounded_string(
        _item_attribute(item, "key"),
        f"LangMem official result {index} key",
        _MAX_MEMORY_KEY_CHARS,
    )


def _item_value(item: Any, *, index: int) -> Any:
    value = _item_attribute(item, "value")
    if value is None:
        raise LangMemOfficialError(
            f"LangMem official result {index} is missing its value"
        )
    return value


def _item_timestamp(item: Any, name: str) -> str | None:
    value = _item_attribute(item, name)
    if value is None:
        return None
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return str(value.isoformat())
    text = str(value).strip()
    return text or None


def _optional_score(value: Any, *, index: int) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise LangMemOfficialError(
            f"LangMem official search result {index} score must be numeric"
        )
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise LangMemOfficialError(
            f"LangMem official search result {index} score must be numeric"
        ) from exc
    if not math.isfinite(score):
        raise LangMemOfficialError(
            f"LangMem official search result {index} score must be finite"
        )
    return score


def _validate_manager(manager: Any) -> None:
    for name in ("ainvoke", "asearch", "aput"):
        if not callable(getattr(manager, name, None)):
            raise TypeError(f"LangMem manager must provide {name}()")


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _runtime_values(runtime: dict[str, Any] | None) -> dict[str, Any]:
    if runtime is None:
        return {}
    if not isinstance(runtime, dict):
        raise ValueError("langmem_official runtime must be an object")
    allowed = {
        "api_base",
        "api_key",
        "model",
        "run_id",
        "request_timeout_seconds",
        "max_retries",
        "memory_max_tokens",
        "manager_factory",
    }
    unknown = sorted(str(key) for key in set(runtime) - allowed)
    if unknown:
        raise ValueError(f"Unknown langmem_official runtime values: {unknown}")
    values = dict(runtime)
    for name, maximum in (
        ("api_base", 2_048),
        ("api_key", 16_384),
        ("model", 512),
        ("run_id", 512),
    ):
        if name in values:
            values[name] = _bounded_string(
                values[name], f"langmem_official runtime {name}", maximum
            )
    if "request_timeout_seconds" in values:
        values["request_timeout_seconds"] = _positive_float(
            values["request_timeout_seconds"],
            "langmem_official runtime request_timeout_seconds",
        )
    if "max_retries" in values:
        values["max_retries"] = _bounded_int(
            values["max_retries"],
            "langmem_official runtime max_retries",
            minimum=0,
            maximum=100,
        )
    if "memory_max_tokens" in values:
        values["memory_max_tokens"] = _bounded_int(
            values["memory_max_tokens"],
            "langmem_official runtime memory_max_tokens",
            minimum=1,
            maximum=1_000_000,
        )
    if "manager_factory" in values and not callable(values["manager_factory"]):
        raise ValueError("langmem_official runtime manager_factory must be callable")
    return values


def _scope_user_id(run_id: str, agent_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0{agent_id}".encode("utf-8")).hexdigest()
    return f"agent-community-{digest}"


def _provider_error(
    operation: str,
    error: Exception,
    api_key: str | None,
) -> LangMemOfficialError:
    if isinstance(error, LangMemOfficialError):
        return error
    detail = str(error).strip() or type(error).__name__
    if api_key:
        detail = detail.replace(api_key, "[REDACTED]")
    if len(detail) > 512:
        detail = detail[:512] + "..."
    return LangMemOfficialError(
        f"LangMem official {operation} failed: {detail}"
    )


def _option_int(
    options: dict[str, Any],
    name: str,
    *,
    default: int,
    minimum: int = 1,
    maximum: int,
    alias: str | None = None,
) -> int:
    if alias and name in options and alias in options:
        raise ValueError(f"memory options cannot contain both {name} and {alias}")
    value = options.pop(name, options.pop(alias, default) if alias else default)
    return _bounded_int(value, name, minimum=minimum, maximum=maximum)


def _option_string(
    options: dict[str, Any],
    name: str,
    *,
    default: str,
    maximum: int,
) -> str:
    return _bounded_string(options.pop(name, default), name, maximum)


def _bounded_int(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result != value and not isinstance(value, str):
        raise ValueError(f"{name} must be an integer")
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _positive_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive number")
    return result


def _bounded_string(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{name} cannot exceed {maximum} characters")
    return result


def _normalize_memory_schema(value: Any) -> str:
    allowed = {
        LANGMEM_MEMORY_SCHEMA_LEGACY,
        LANGMEM_MEMORY_SCHEMA_CATEGORIZED,
    }
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(
            "langmem_official memory_schema must be one of "
            f"{sorted(allowed)}"
        )
    return value


__all__ = [
    "LANGMEM_DEFAULT_MEMORY_SCHEMA",
    "LANGMEM_LEGACY_SNAPSHOT_SCHEMA_VERSION",
    "LANGMEM_MEMORY_SCHEMA_CATEGORIZED",
    "LANGMEM_MEMORY_SCHEMA_LEGACY",
    "LANGMEM_SDK_VERSION",
    "LANGMEM_SNAPSHOT_SCHEMA_VERSION",
    "LangMemOfficialDependencyError",
    "LangMemOfficialError",
    "LangMemOfficialMemory",
    "build_langmem_official_memory",
]
