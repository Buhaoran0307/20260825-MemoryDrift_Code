"""Platform contract for the pinned agiresearch/A-mem implementation.

SDK imports and embedding initialization are lazy. Snapshots preserve notes AND
document vectors; second-stage retrieval never observes events or calls the LLM.
See docs/AMEM_OFFICIAL.md for the explicit upstream compatibility fixes.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .memory import (
    MemoryContext, MemoryEvent, MemoryProviderResponseError, MemoryQuery,
    MemorySnapshot, _EventMemory,
)

AMEM_COMMIT = "ceffb860f0712bbae97b184d440df62bc910ca8d"
AMEM_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
AMEM_EMBEDDING_DIMENSIONS = 384
AMEM_ADAPTER_VERSION = 1

# Keep synchronous SDK work out of asyncio's HTTP executor, avoiding starvation
# when many agents are waiting for their async platform LLM calls.
_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="amem")


class AMemOfficialError(RuntimeError):
    """An A-Mem operation failed without committing durable memory."""


class AMemOfficialDependencyError(AMemOfficialError):
    """The pinned SDK or local embedding runtime is unavailable."""


class AMemOfficialResponseError(MemoryProviderResponseError, AMemOfficialError):
    """Expose A-Mem output diagnostics through the platform's reading results."""


class AMemOfficialTransportError(MemoryProviderResponseError, AMemOfficialError):
    """Use the existing reading-artifact diagnostic channel for HTTP failures."""

    def __init__(self, message: str, diagnostics: dict[str, Any]) -> None:
        self.diagnostics = copy.deepcopy(diagnostics)
        # No successful LLMResponse exists; bypass the response-validation initializer.
        Exception.__init__(self, message)


def _integer(value: Any, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"a_mem_official {name} must be an integer from 1 to {maximum}")
    return value


def _make_runtime(**kwargs: Any) -> Any:
    from .amem_runtime import AMemRuntime

    return AMemRuntime(**kwargs)


class AMemOfficialMemory(_EventMemory):
    strategy = "a_mem_official"

    def __init__(
        self, *, agent_id: str, llm: Any = None, retrieval_top_k: int = 5,
        evo_threshold: int = 100, evolution_neighbor_k: int = 5,
        response_validation_max_attempts: int = 3,
        response_tolerance_enabled: bool = True,
        on_event_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(agent_id=agent_id, llm=llm)
        self.retrieval_top_k = _integer(retrieval_top_k, "retrieval_top_k", 1000)
        self.evo_threshold = _integer(evo_threshold, "evo_threshold", 10000)
        self.evolution_neighbor_k = _integer(
            evolution_neighbor_k, "evolution_neighbor_k", 1000
        )
        self.response_validation_max_attempts = _integer(
            response_validation_max_attempts, "response_validation_max_attempts", 100
        )
        if type(response_tolerance_enabled) is not bool:
            raise ValueError("a_mem_official response_tolerance_enabled must be true or false")
        self.response_tolerance_enabled = response_tolerance_enabled
        # None preserves the absence of this policy in a historical snapshot.
        self._snapshot_response_tolerance_enabled: bool | None = response_tolerance_enabled
        self._records: list[dict[str, Any]] = []
        self._evo_cnt = 0
        self._runtime: Any = None
        self._retired: list[Any] = []
        self._operation_lock = asyncio.Lock()
        self._on_event_progress = on_event_progress

    def _event_progress(self, **values: Any) -> None:
        if self._on_event_progress is not None:
            self._on_event_progress(dict(values))

    async def _worker(self, operation: Any) -> Any:
        future = asyncio.get_running_loop().run_in_executor(_WORKERS, operation)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A worker cannot be killed. Keep the operation lock until it stops,
            # then discard its uncommitted state in observe().
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not future.cancelled():
                future.exception()
            raise

    async def _ensure_runtime(self) -> Any:
        await self._close_retired()
        if self._runtime is None:
            records = copy.deepcopy(self._records)
            loop = asyncio.get_running_loop()

            def build() -> None:
                # Assign inside the worker so cancellation cannot leak an index.
                self._runtime = _make_runtime(
                    llm=self.llm, agent_id=self.agent_id, loop=loop,
                    records=records, evo_cnt=self._evo_cnt,
                    evo_threshold=self.evo_threshold,
                    evolution_neighbor_k=self.evolution_neighbor_k,
                    response_validation_max_attempts=self.response_validation_max_attempts,
                    response_tolerance_enabled=self.response_tolerance_enabled,
                    on_event_progress=self._on_event_progress,
                )

            await self._worker(build)
        self._runtime.set_loop(asyncio.get_running_loop())
        return self._runtime

    def _discard(self) -> None:
        if self._runtime is not None:
            self._retired.append(self._runtime)
            self._runtime = None

    async def _close_retired(self) -> None:
        while self._retired:
            retired = self._retired[0]
            await self._worker(retired.close)
            self._retired.pop(0)

    async def observe(self, events: list[MemoryEvent]) -> None:
        async with self._operation_lock:
            seen = set(self._event_ids)
            unseen = []
            for event in events:
                if event.event_id not in seen:
                    seen.add(event.event_id)
                    unseen.append(event)
            if not unseen:
                return
            if self.llm is None:
                raise AMemOfficialError("a_mem_official writes require the platform LLM")
            total = len(unseen)
            self._event_progress(
                status="running", completed_events=0, total_events=total,
                current_event_id=unseen[0].event_id,
            )
            try:
                runtime = await self._ensure_runtime()
                records, evo_cnt = await self._worker(lambda: runtime.observe(unseen))
                records = _validated_records(records)
                _validate_sources(records, [*self._events, *unseen])
            except BaseException:
                self._discard()
                try:
                    self._event_progress(
                        status="failed", completed_events=0,
                        total_events=total, current_event_id=None,
                    )
                except Exception:
                    pass
                raise
            self._records = records
            self._evo_cnt = evo_cnt
            self._snapshot_response_tolerance_enabled = self.response_tolerance_enabled
            self._remember_events(unseen)
            self._event_progress(
                status="completed", completed_events=total,
                total_events=total, current_event_id=None,
                last_completed_event_id=unseen[-1].event_id,
            )

    async def retrieve(self, query: MemoryQuery) -> MemoryContext:
        if not isinstance(query.text, str) or not query.text.strip():
            raise ValueError("a_mem_official retrieval query cannot be empty")
        limit = self.retrieval_top_k
        if query.max_items is not None:
            if type(query.max_items) is not int:
                raise ValueError("a_mem_official max_items must be an integer")
            limit = min(limit, query.max_items)
        async with self._operation_lock:
            if not self._records or limit < 1:
                return MemoryContext(self.strategy, (), self.revision)
            try:
                runtime = await self._ensure_runtime()
                results = await self._worker(lambda: runtime.search(query.text, limit))
            except BaseException:
                self._discard()
                raise
            durable = {record["note"]["id"]: record for record in self._records}
            items = []
            for result in results:
                record = durable[result["id"]]
                note = record["note"]
                items.append(MemoryEvent(
                    event_id=f"a_mem_official:{note['id']}", thread_id=query.thread_id,
                    kind="atomic_note", actor_id=self.agent_id,
                    content=(f"{note['content']}\nContext: {note['context']}\n"
                             f"Keywords: {', '.join(note['keywords'])}"),
                    created_at=note["timestamp"], provenance="memory",
                    metadata={
                        "note_id": note["id"], "source_event_id": record["source_event_id"],
                        "tags": list(note["tags"]), "links": list(note["links"]),
                        "distance": result.get("score"),
                        "is_neighbor": result.get("is_neighbor", False),
                    },
                ))
            return MemoryContext(self.strategy, tuple(items), self.revision)

    async def aclose(self) -> None:
        async with self._operation_lock:
            self._discard()
            await self._close_retired()

    def _snapshot_options(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "adapter_version": AMEM_ADAPTER_VERSION,
            "provider": "agiresearch/A-mem", "upstream_commit": AMEM_COMMIT,
            "embedding_model": AMEM_EMBEDDING_MODEL,
            "embedding_dimensions": AMEM_EMBEDDING_DIMENSIONS,
            "state_frozen": True, "document_embeddings_frozen": True,
            "agent_id": self.agent_id,
            "retrieval_top_k": self.retrieval_top_k,
            "evolution_neighbor_k": self.evolution_neighbor_k,
            "evo_threshold": self.evo_threshold, "evo_cnt": self._evo_cnt,
            "records": copy.deepcopy(self._records),
            **({"response_validation_max_attempts": self.response_validation_max_attempts}
               if self.response_validation_max_attempts != 3 else {}),
            **({"response_tolerance_enabled": self._snapshot_response_tolerance_enabled}
               if self._snapshot_response_tolerance_enabled is not None else {}),
        }

    def restore(self, snapshot: MemorySnapshot) -> None:
        if self._operation_lock.locked():
            raise ValueError("Cannot restore A-Mem during an active operation")
        if snapshot.strategy != self.strategy:
            raise ValueError("a_mem_official snapshot strategy mismatch")
        options = snapshot.options
        expected = self._snapshot_options()
        if not isinstance(options, dict):
            raise ValueError("a_mem_official snapshot schema mismatch")
        saved_response_tolerance = options.get("response_tolerance_enabled")
        if "response_tolerance_enabled" in options:
            if (type(saved_response_tolerance) is not bool
                    or saved_response_tolerance != self.response_tolerance_enabled):
                raise ValueError("a_mem_official snapshot response_tolerance_enabled mismatch")
        # Historical snapshots predate response tolerance. Do not infer that the
        # new protocol produced their notes or alter their serialized options.
        options = {key: value for key, value in options.items()
                   if key != "response_tolerance_enabled"}
        expected.pop("response_tolerance_enabled", None)
        # Snapshots written before evolution_neighbor_k became configurable used
        # the pinned upstream default of five.  Accept them only when restoring
        # into that same effective configuration.
        if set(options) == set(expected) - {"evolution_neighbor_k"}:
            if self.evolution_neighbor_k != 5:
                raise ValueError("a_mem_official snapshot evolution_neighbor_k mismatch")
            options = {**options, "evolution_neighbor_k": 5}
        elif set(options) != set(expected):
            raise ValueError("a_mem_official snapshot schema mismatch")
        for name, value in expected.items():
            if name not in {"records", "evo_cnt"} and (
                type(options[name]) is not type(value) or options[name] != value
            ):
                raise ValueError(f"a_mem_official snapshot {name} mismatch")
        if type(snapshot.revision) is not int or snapshot.revision < 0:
            raise ValueError("a_mem_official snapshot revision is invalid")
        if type(options["evo_cnt"]) is not int or not 0 <= options["evo_cnt"] <= snapshot.revision:
            raise ValueError("a_mem_official snapshot evolution count is invalid")
        events = [MemoryEvent.from_dict(item) for item in snapshot.events]
        if len({e.event_id for e in events}) != len(events) or len(events) != snapshot.revision:
            raise ValueError("a_mem_official snapshot events/revision mismatch")
        records = _validated_records(options["records"])
        _validate_sources(records, events)
        # Commit only after all validation, leaving the previous state usable.
        self._discard()
        self._events, self._event_ids = events, {e.event_id for e in events}
        self.revision = snapshot.revision
        self._records, self._evo_cnt = records, options["evo_cnt"]
        self._snapshot_response_tolerance_enabled = saved_response_tolerance


def _validated_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("a_mem_official snapshot records must be a list")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("a_mem_official records must contain finite JSON data") from exc
    identifiers = set()
    for record in value:
        if not isinstance(record, dict) or set(record) != {"note", "source_event_id", "embedding"}:
            raise ValueError("a_mem_official snapshot record schema is invalid")
        note = record["note"]
        fields = {"id", "content", "keywords", "links", "context", "category", "tags",
                  "timestamp", "last_accessed", "retrieval_count", "evolution_history"}
        if not isinstance(note, dict) or set(note) != fields:
            raise ValueError("a_mem_official snapshot note schema is invalid")
        for name in ("id", "content", "context", "category", "timestamp", "last_accessed"):
            if not isinstance(note[name], str) or not note[name].strip():
                raise ValueError(f"a_mem_official snapshot note {name} is invalid")
        for name in ("keywords", "links", "tags"):
            if not isinstance(note[name], list) or not all(isinstance(s, str) for s in note[name]):
                raise ValueError(f"a_mem_official snapshot note {name} is invalid")
        if type(note["retrieval_count"]) is not int or note["retrieval_count"] < 0:
            raise ValueError("a_mem_official retrieval count is invalid")
        if not isinstance(note["evolution_history"], list):
            raise ValueError("a_mem_official evolution history is invalid")
        if note["id"] in identifiers:
            raise ValueError("a_mem_official snapshot contains duplicate note ids")
        identifiers.add(note["id"])
        vector = record["embedding"]
        if not isinstance(vector, list) or len(vector) != AMEM_EMBEDDING_DIMENSIONS or not all(
            type(n) in (int, float) and math.isfinite(n) for n in vector
        ):
            raise ValueError("a_mem_official snapshot embedding is invalid")
        if not isinstance(record["source_event_id"], str):
            raise ValueError("a_mem_official snapshot source_event_id is invalid")
    for record in value:
        if any(link not in identifiers for link in record["note"]["links"]):
            raise ValueError("a_mem_official snapshot has a dangling memory link")
    return copy.deepcopy(value)


def _validate_sources(records: list[dict[str, Any]], events: list[MemoryEvent]) -> None:
    # New writes use the verbatim event body. Accept the old speaker-prefixed
    # format only for snapshot compatibility; never rewrite frozen note vectors.
    sources = {
        event.event_id: (event.content, _legacy_speaker_content(event)) for event in events
    }
    if len(records) != len(events) or {r["source_event_id"] for r in records} != set(sources):
        raise ValueError("a_mem_official notes do not match observed events")
    if any(r["note"]["content"] not in sources[r["source_event_id"]] for r in records):
        raise ValueError("a_mem_official note content does not match its source event")


def _legacy_speaker_content(event: MemoryEvent) -> str:
    """Recognize historical snapshots; not an input formatter for new writes."""
    return f"Speaker {event.actor_id or 'unknown'} says:\n{event.content}"


def note_id(agent_id: str, event_id: str) -> str:
    return hashlib.sha256(f"{len(agent_id)}:{agent_id}{event_id}".encode()).hexdigest()


def build_amem_official_memory(
    *, agent_id: str, llm: Any = None, options: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> AMemOfficialMemory:
    values = dict(options or {})
    if set(values) - {"retrieval_top_k", "evo_threshold", "evolution_neighbor_k",
                      "response_validation_max_attempts", "response_tolerance_enabled"}:
        raise ValueError(f"Unknown a_mem_official memory options: {sorted(values)}")
    runtime_values = dict(runtime or {})
    on_event_progress = runtime_values.pop("on_event_progress", None)
    if runtime_values:
        raise ValueError(
            "a_mem_official uses the platform LLM and local embeddings; "
            f"unknown runtime values: {sorted(runtime_values)}"
        )
    if on_event_progress is not None and not callable(on_event_progress):
        raise ValueError("a_mem_official on_event_progress must be callable")
    return AMemOfficialMemory(
        agent_id=agent_id, llm=llm,
        on_event_progress=on_event_progress, **values,
    )
