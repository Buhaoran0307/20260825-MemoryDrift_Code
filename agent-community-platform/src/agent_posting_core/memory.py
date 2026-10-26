from __future__ import annotations
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, TYPE_CHECKING
from .llm import LLMRequest, LLMResponse, response_finish_reason
if TYPE_CHECKING:
    from .llm import LLMClient

class MemoryProviderResponseError(ValueError):
    """Keep failed model output available after a memory update is rolled back."""

    def __init__(self, message: str, *, request: LLMRequest, response: LLMResponse, parsed_content: str, parse_error: json.JSONDecodeError | None=None) -> None:
        self.diagnostics = {'agent_id': request.agent_id, 'error': message, 'response_content': response.content, 'response_character_count': len(response.content), 'parsed_content': parsed_content, 'finish_reason': response_finish_reason(response), 'requested_max_tokens': request.max_tokens, 'usage': response.raw.get('usage'), 'raw_response': response.raw, 'parse_error': {'message': parse_error.msg, 'line': parse_error.lineno, 'column': parse_error.colno, 'position': parse_error.pos} if parse_error is not None else None}
        if parse_error is not None:
            message += f': {parse_error.msg} (line {parse_error.lineno}, column {parse_error.colno}, char {parse_error.pos})'
        super().__init__(message)

@dataclass(frozen=True)
class MemoryEvent:
    event_id: str
    thread_id: str
    kind: str
    actor_id: str
    content: str
    created_at: str
    parent_event_id: str | None = None
    provenance: str = 'platform'
    visibility: str = 'observed'
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError('memory event_id cannot be empty')
        if not self.thread_id.strip():
            raise ValueError('memory thread_id cannot be empty')
        if not self.kind.strip():
            raise ValueError('memory kind cannot be empty')
        if not self.content.strip():
            raise ValueError('memory content cannot be empty')

    def as_dict(self) -> dict[str, Any]:
        return {'event_id': self.event_id, 'thread_id': self.thread_id, 'kind': self.kind, 'actor_id': self.actor_id, 'content': self.content, 'created_at': self.created_at, 'parent_event_id': self.parent_event_id, 'provenance': self.provenance, 'visibility': self.visibility, 'metadata': dict(self.metadata)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> 'MemoryEvent':
        return cls(event_id=str(value['event_id']), thread_id=str(value['thread_id']), kind=str(value['kind']), actor_id=str(value.get('actor_id') or ''), content=str(value['content']), created_at=str(value.get('created_at') or ''), parent_event_id=str(value['parent_event_id']) if value.get('parent_event_id') is not None else None, provenance=str(value.get('provenance') or 'platform'), visibility=str(value.get('visibility') or 'observed'), metadata=dict(value.get('metadata') or {}))

@dataclass(frozen=True)
class MemoryQuery:
    text: str
    thread_id: str
    round_index: int
    max_items: int | None = None

@dataclass(frozen=True)
class MemoryContext:
    strategy: str
    items: tuple[MemoryEvent, ...]
    revision: int

    def render(self, *, max_chars: int=12000) -> str:
        if max_chars < 1:
            return ''
        lines: list[str] = []
        used = 0
        ranked = self.strategy in {'hindsight_cloud', 'memos_cloud'}
        for event in self.items if ranked else reversed(self.items):
            line = f'- [{event.kind}; {event.created_at}; {event.visibility}] {event.content}'
            remaining = max_chars - used
            if remaining <= 0:
                break
            if lines and len(line) > remaining:
                break
            if not lines and len(line) > remaining:
                if self.strategy == 'langmem_official':
                    break
                line = line[:remaining]
            lines.append(line)
            used += len(line) + 1
        if not ranked:
            lines.reverse()
        return '\n'.join(lines)

@dataclass(frozen=True)
class MemorySnapshot:
    strategy: str
    revision: int
    events: tuple[dict[str, Any], ...]
    options: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {'strategy': self.strategy, 'revision': self.revision, 'events': [dict(event) for event in self.events], 'options': dict(self.options)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> 'MemorySnapshot':
        return cls(strategy=str(value['strategy']), revision=int(value.get('revision') or 0), events=tuple((dict(item) for item in value.get('events') or [])), options=dict(value.get('options') or {}))

class Memory(ABC):
    """Replaceable memory boundary used by every hosted agent."""
    strategy: str

    def __init__(self, *, agent_id: str, llm: 'LLMClient | None'=None) -> None:
        if not agent_id.strip():
            raise ValueError('memory agent_id cannot be empty')
        self.agent_id = agent_id
        self.llm = llm
        self.revision = 0

    @abstractmethod
    async def initialize(self, events: list[MemoryEvent]) -> None:
        """Initialize durable memory from canonical imported events."""

    @abstractmethod
    async def retrieve(self, query: MemoryQuery) -> MemoryContext:
        """Return model-ready memory context for one action."""

    @abstractmethod
    async def observe(self, events: list[MemoryEvent]) -> None:
        """Ingest platform observations or completed actions."""

    @abstractmethod
    def snapshot(self) -> MemorySnapshot:
        """Return JSON-compatible state for resume."""

    @abstractmethod
    def restore(self, snapshot: MemorySnapshot) -> None:
        """Restore state previously returned by snapshot."""

    async def aclose(self) -> None:
        """Release resources owned by an adapter."""

    async def finalize_ingestion(self) -> None:
        """Wait for accepted remote events to become searchable, when applicable."""

    @property
    def pending_update_event_ids(self) -> frozenset[str]:
        """Buffered source events whose memory update has not completed yet."""
        return frozenset()

    @property
    def skipped_update_events(self) -> tuple[dict[str, Any], ...]:
        """Source updates abandoned after retries, separate from retained memory."""
        return ()

class _EventMemory(Memory):

    def __init__(self, *, agent_id: str, llm: 'LLMClient | None'=None) -> None:
        super().__init__(agent_id=agent_id, llm=llm)
        self._events: list[MemoryEvent] = []
        self._event_ids: set[str] = set()

    async def initialize(self, events: list[MemoryEvent]) -> None:
        if self.revision == 0:
            await self.observe(events)

    async def observe(self, events: list[MemoryEvent]) -> None:
        self._remember_events(events)

    def _remember_events(self, events: list[MemoryEvent]) -> list[MemoryEvent]:
        remembered: list[MemoryEvent] = []
        for event in events:
            if event.event_id in self._event_ids:
                continue
            self._events.append(event)
            self._event_ids.add(event.event_id)
            self.revision += 1
            remembered.append(event)
        return remembered

    def snapshot(self) -> MemorySnapshot:
        return MemorySnapshot(strategy=self.strategy, revision=self.revision, events=tuple((event.as_dict() for event in self._events)), options=self._snapshot_options())

    def restore(self, snapshot: MemorySnapshot) -> None:
        if snapshot.strategy != self.strategy:
            raise ValueError(f'Cannot restore {snapshot.strategy} snapshot into {self.strategy} memory')
        events = [MemoryEvent.from_dict(value) for value in snapshot.events]
        event_ids = [event.event_id for event in events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError('memory snapshot contains duplicate event ids')
        self._events = events
        self._event_ids = set(event_ids)
        self.revision = snapshot.revision
        self._restore_options(snapshot.options)

    def _snapshot_options(self) -> dict[str, Any]:
        return {}

    def _restore_options(self, options: dict[str, Any]) -> None:
        if options:
            raise ValueError(f'Unexpected memory snapshot options: {sorted(options)}')
MemoryConstructor = Callable[..., Memory]
_MEMORY_CONSTRUCTORS: dict[str, MemoryConstructor] = {}

def register_memory_strategy(name: str, constructor: MemoryConstructor, *, replace: bool=False) -> None:
    normalized = name.strip().lower().replace('-', '_')
    if not normalized:
        raise ValueError('memory strategy name cannot be empty')
    if not callable(constructor):
        raise TypeError('memory strategy constructor must be callable')
    if normalized in _MEMORY_CONSTRUCTORS and (not replace):
        raise ValueError(f'Memory strategy already registered: {name}')
    _MEMORY_CONSTRUCTORS[normalized] = constructor

def registered_memory_strategies() -> tuple[str, ...]:
    """Return every strategy currently registered with the factory."""
    return tuple(_MEMORY_CONSTRUCTORS)

def create_memory(strategy: str, *, agent_id: str, llm: 'LLMClient | None'=None, options: dict[str, Any] | None=None, runtime: dict[str, Any] | None=None) -> Memory:
    options = dict(options or {})
    normalized = strategy.strip().lower().replace('-', '_')
    constructor = _MEMORY_CONSTRUCTORS.get(normalized)
    if constructor is None:
        raise ValueError(f'Unsupported memory strategy: {strategy}')
    kwargs: dict[str, Any] = {'agent_id': agent_id, 'llm': llm, 'options': options}
    if runtime is not None:
        kwargs['runtime'] = dict(runtime)
    return constructor(**kwargs)

def _create_mem0_platform_v3(**kwargs: Any) -> Memory:
    from .mem0_platform_v3 import build_mem0_platform_v3_memory
    return build_mem0_platform_v3_memory(**kwargs)

def _create_cloud(strategy: str, **kwargs: Any) -> Memory:
    from .cloud_memory import build_cloud_memory
    return build_cloud_memory(strategy, **kwargs)

def _create_langmem_official(**kwargs: Any) -> Memory:
    from .langmem_official import build_langmem_official_memory
    return build_langmem_official_memory(**kwargs)

def _create_amem_official(**kwargs: Any) -> Memory:
    from .amem_official import build_amem_official_memory
    return build_amem_official_memory(**kwargs)
DEFAULT_MEMORY_STRATEGY = 'langmem_official'
_BUILTIN_MEMORY_STRATEGIES: tuple[tuple[str, MemoryConstructor], ...] = (('langmem_official', _create_langmem_official), ('mem0_platform_v3', _create_mem0_platform_v3), ('a_mem_official', _create_amem_official), ('hindsight_cloud', partial(_create_cloud, 'hindsight_cloud')), ('memos_cloud', partial(_create_cloud, 'memos_cloud')))
SUPPORTED_MEMORY_STRATEGIES = tuple((name for name, _constructor in _BUILTIN_MEMORY_STRATEGIES))
for _strategy_name, _constructor in _BUILTIN_MEMORY_STRATEGIES:
    register_memory_strategy(_strategy_name, _constructor)
__all__ = ['DEFAULT_MEMORY_STRATEGY', 'Memory', 'MemoryContext', 'MemoryEvent', 'MemoryQuery', 'MemorySnapshot', 'SUPPORTED_MEMORY_STRATEGIES', 'create_memory', 'registered_memory_strategies', 'register_memory_strategy']
del _constructor, _strategy_name
