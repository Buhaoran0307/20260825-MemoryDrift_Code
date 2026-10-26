from __future__ import annotations
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from .llm import LLMClient, LLMRequest, LLMResponse
from .memory import Memory, MemoryEvent, MemoryQuery, MemorySnapshot
from .memory_progress import MemoryUpdateProgress
from .prompting import PromptBuilder
from .simulation import AgentProfile, CREATE_COMMENT_SCHEMA, CREATE_POST_SCHEMA, GET_THREAD_SCHEMA, LIST_RECENT_POSTS_SCHEMA, ToolCall
ACTION_SCHEMAS = {schema['name']: schema for schema in (GET_THREAD_SCHEMA, CREATE_POST_SCHEMA, CREATE_COMMENT_SCHEMA, LIST_RECENT_POSTS_SCHEMA)}

@dataclass(frozen=True)
class PlannedAction:
    call: ToolCall
    request: dict[str, Any]
    raw_response: dict[str, Any]
    audit: dict[str, Any]

class LLMPostingAgent:
    """One behavior-model request per action, composed with replaceable memory."""

    def __init__(self, *, profile: AgentProfile, llm: LLMClient, memory: Memory, seed_events: list[MemoryEvent] | None=None, prompt_builder: PromptBuilder | None=None) -> None:
        if memory.agent_id != profile.agent_id:
            raise ValueError('memory and profile agent ids must match')
        self.profile = profile
        self.llm = llm
        self.memory = memory
        self.seed_events = list(seed_events or [])
        self.prompt_builder = prompt_builder or PromptBuilder()
        self._initialized = False
        self.last_request: dict[str, Any] | None = None
        self.on_memory_event: Callable[[MemoryEvent, MemorySnapshot, str], None] | None = None
        self.on_memory_progress: Callable[[dict[str, Any]], None] | None = None
        self._memory_progress: MemoryUpdateProgress | None = None
        self._last_memory_progress: MemoryUpdateProgress | None = None

    @contextmanager
    def memory_update_scope(self, events: list[MemoryEvent], *, phase: str) -> Iterator[None]:
        """Optionally combine initialization and observation into one progress bar."""
        if self.on_memory_progress is None or self._memory_progress is not None:
            yield
            return
        snapshot = self.memory.snapshot()
        snapshot_events = snapshot.events
        pending_ids = self.memory.pending_update_event_ids
        known_event_ids = {str(event['event_id']) for event in snapshot_events} - pending_ids
        known_event_ids.update((item['source_event']['event_id'] for item in self.memory.skipped_update_events))
        progress = MemoryUpdateProgress(events=[*(MemoryEvent.from_dict(event) for event in snapshot_events if event['event_id'] in pending_ids), *events], known_event_ids=known_event_ids, strategy=self.memory.strategy, phase=phase, callback=self.on_memory_progress, remote_live=snapshot.options.get('remote_live') is True)
        if not progress.event_ids:
            yield
            return
        self._memory_progress = progress
        self._last_memory_progress = progress
        try:
            progress.start()
            yield
        except BaseException:
            progress.fail()
            raise
        else:
            progress.finish()
        finally:
            self._memory_progress = None

    def record_memory_rollback(self) -> None:
        """Do not report reverted read-only updates as successfully committed."""
        if self._last_memory_progress is not None:
            self._last_memory_progress.fail(rolled_back=True)

    async def _update_memory(self, events: list[MemoryEvent], *, initialize: bool=False) -> None:
        progress = self._memory_progress
        buffered_before = self.memory.pending_update_event_ids
        if progress is not None:
            progress.begin_batch(events, buffered_ids=buffered_before)
        if initialize:
            await self.memory.initialize(events)
        else:
            await self.memory.observe(events)
        if progress is not None:
            progress.complete_batch(events, buffered_before=buffered_before, buffered_after=self.memory.pending_update_event_ids, skipped=self.memory.skipped_update_events)

    async def initialize_memory(self) -> None:
        if self._initialized:
            return
        progress_events = self.seed_events if self.memory.revision == 0 else []
        with self.memory_update_scope(progress_events, phase='initialize'):
            if self.on_memory_event is None and self.memory.strategy != 'mem0_platform_v3' or self.memory.revision != 0:
                await self._update_memory(self.seed_events, initialize=True)
            else:
                for index, event in enumerate(self.seed_events):
                    before = self.memory.revision
                    await self._update_memory([event], initialize=index == 0)
                    if self.memory.revision != before and self.on_memory_event is not None:
                        self.on_memory_event(event, self.memory.snapshot(), 'initialize')
                if not self.seed_events:
                    await self._update_memory([], initialize=True)
        self._initialized = True

    async def plan_comment(self, *, thread: dict[str, Any], environment_events: list[MemoryEvent], round_index: int) -> PlannedAction:
        await self.initialize_memory()
        await self.remember_events(environment_events)
        query_text = ' '.join([str(thread['post']['title']), str(thread['post']['body']), *(str(item['body']) for item in thread['comments'][-5:])])
        memory_context = await self.memory.retrieve(MemoryQuery(text=query_text, thread_id=str(thread['post']['post_id']), round_index=round_index))
        prompt = self.prompt_builder.build_comment_prompt(profile=self.profile, memory=memory_context, thread=thread, round_index=round_index)
        request = LLMRequest(messages=prompt.messages, tools=[CREATE_COMMENT_SCHEMA], purpose='action', agent_id=self.profile.agent_id)
        self.last_request = {'messages': prompt.messages, 'tools': [CREATE_COMMENT_SCHEMA], 'purpose': 'action', 'agent_id': self.profile.agent_id}
        response = await self.llm.complete(request)
        call = self._parse_action(response)
        return PlannedAction(call=call, request=dict(self.last_request), raw_response=response.raw, audit={**prompt.audit, 'agent_id': self.profile.agent_id, 'behavior_llm_calls': 1})

    async def remember_created_comment(self, event: MemoryEvent) -> None:
        await self.remember_events([event])

    async def plan_forum_action(self, *, feed: dict[str, Any], environment_events: list[MemoryEvent], round_index: int, scope_id: str, tool_names: tuple[str, ...]) -> PlannedAction:
        await self.initialize_memory()
        await self.remember_events(environment_events)
        query_text = ' '.join((f"{post.get('title', '')} {post.get('body', '')}" for post in feed.get('posts') or []))
        memory_context = await self.memory.retrieve(MemoryQuery(text=query_text, thread_id=scope_id, round_index=round_index))
        schemas: list[dict[str, Any]] = []
        for name in tool_names:
            if name not in ACTION_SCHEMAS:
                raise ValueError(f'Unsupported forum action schema: {name}')
            schemas.append(ACTION_SCHEMAS[name])
        prompt = self.prompt_builder.build_forum_prompt(profile=self.profile, memory=memory_context, feed=feed, round_index=round_index, tool_names=tool_names)
        request = LLMRequest(messages=prompt.messages, tools=schemas, purpose='action', agent_id=self.profile.agent_id)
        self.last_request = {'messages': prompt.messages, 'tools': schemas, 'purpose': 'action', 'agent_id': self.profile.agent_id}
        response = await self.llm.complete(request)
        call = self._parse_action(response)
        return PlannedAction(call=call, request=dict(self.last_request), raw_response=response.raw, audit={**prompt.audit, 'agent_id': self.profile.agent_id, 'behavior_llm_calls': 1})

    async def remember_events(self, events: list[MemoryEvent]) -> None:
        with self.memory_update_scope(events, phase='observe'):
            if self.on_memory_event is None and self.memory.strategy not in {'mem0_platform_v3'}:
                await self._update_memory(events)
                return
            for event in events:
                before = self.memory.revision
                await self._update_memory([event])
                if self.memory.revision != before and self.on_memory_event is not None:
                    self.on_memory_event(event, self.memory.snapshot(), 'observe')

    async def finalize_memory(self) -> None:
        pending = self.memory.pending_update_event_ids
        await self.memory.finalize_ingestion()
        if self._memory_progress is not None and pending:
            self._memory_progress.complete_batch([], buffered_before=pending, buffered_after=self.memory.pending_update_event_ids)

    @staticmethod
    def _parse_action(response: LLMResponse) -> ToolCall:
        if len(response.tool_calls) == 1:
            return response.tool_calls[0]
        if len(response.tool_calls) > 1:
            raise ValueError('agent must return exactly one tool call')
        try:
            value = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise ValueError('agent returned neither a tool call nor valid JSON') from exc
        if not isinstance(value, dict):
            raise ValueError('agent action JSON must be an object')
        name = str(value.get('name') or value.get('action') or '')
        arguments = value.get('arguments')
        if not isinstance(arguments, dict):
            raise ValueError('agent action arguments must be an object')
        return ToolCall(call_id=str(value.get('call_id') or 'llm-action'), name=name, arguments=dict(arguments))
__all__ = ['LLMPostingAgent', 'PlannedAction']
