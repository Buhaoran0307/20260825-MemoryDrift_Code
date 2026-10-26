from __future__ import annotations
import asyncio
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import Any
from .agents import LLMPostingAgent
from .cloud_memory import CLOUD_STRATEGIES
from .forum_simulation import forum_snapshot
from .llm import LLMClient
from .memory import DEFAULT_MEMORY_STRATEGY, Memory, MemoryProviderResponseError, MemorySnapshot, create_memory
from .simulation import AgentProfile, ForumStore, ToolCall
from .thread_simulation import profile_seed_events, thread_memory_events

class ReadOnlySimulation:
    """Update one reader's memory from complete threads in a single pass."""

    def __init__(self, *, store: ForumStore, llm: LLMClient | None, agent_ids: list[str], environment_type: str, post_ids: list[int] | tuple[int, ...], scope_id: str, communities: list[str] | tuple[str, ...]=(), memory_strategy: str=DEFAULT_MEMORY_STRATEGY, memory_options: dict[str, Any] | None=None, hide_speaker: bool=True, speaker_as_json: bool=False, memory_factory: Callable[[AgentProfile], Memory] | None=None, run_lock: threading.Lock | None=None) -> None:
        if environment_type not in {'moltnet_thread', 'moltnet_forum'}:
            raise ValueError('unsupported read-only environment type')
        if len(agent_ids) != 1:
            raise ValueError('read-only simulation requires exactly one reader')
        selected_post_ids = tuple(dict.fromkeys((int(value) for value in post_ids)))
        if not selected_post_ids:
            raise ValueError('read-only simulation requires at least one post')
        if environment_type == 'moltnet_thread' and len(selected_post_ids) != 1:
            raise ValueError('thread reading requires exactly one post')
        if not scope_id.strip():
            raise ValueError('read-only scope_id cannot be empty')
        self.store = store
        self.llm = llm
        self.post_ids = selected_post_ids
        self.post_id = selected_post_ids[0]
        self.scope_id = scope_id
        self.environment_type = environment_type
        self.communities = tuple(dict.fromkeys(communities))
        self.memory_strategy = memory_strategy
        self.memory_options = dict(memory_options or {})
        self.hide_speaker = hide_speaker
        self.speaker_as_json = speaker_as_json
        self._run_lock = run_lock or threading.Lock()
        self._has_run = False
        threads = [store.get_thread(post_id) for post_id in self.post_ids]
        if environment_type == 'moltnet_forum':
            if not self.communities:
                raise ValueError('forum reading requires at least one community')
            if any((thread['post']['community'] not in self.communities for thread in threads)):
                raise ValueError('read-only post is outside the forum environment')
        row = store.get_agent(agent_ids[0])
        profile = AgentProfile(agent_id=row['agent_id'], display_name=row['display_name'], persona=row['persona'], topic=row['topic'], metadata=row['metadata'])
        memory = create_memory(memory_strategy, agent_id=profile.agent_id, llm=llm, options=self.memory_options) if memory_factory is None else memory_factory(profile)
        if llm is None and memory.strategy not in {'mem0_platform_v3', *CLOUD_STRATEGIES}:
            raise ValueError('this read-only memory strategy requires an LLM')
        initial_events = [event for thread in threads for event in thread_memory_events(thread, hide_speaker=self.hide_speaker, speaker_as_json=self.speaker_as_json)]
        self.agents = [LLMPostingAgent(profile=profile, llm=llm, memory=memory, seed_events=profile_seed_events(profile, initial_events, thread_id=str(self.post_id) if environment_type == 'moltnet_thread' else scope_id, hide_speaker=self.hide_speaker, speaker_as_json=self.speaker_as_json))]
        self._refresh_memory_snapshot()

    async def run_round(self, round_index: int | None=None) -> dict[str, Any]:
        if round_index is not None and (isinstance(round_index, bool) or round_index != 1):
            raise ValueError('read-only simulation runs exactly one round (round 1)')
        await asyncio.to_thread(self._run_lock.acquire)
        try:
            if self._has_run:
                raise ValueError('read-only simulation has already read its threads once')
            self._has_run = True
            self._refresh_memory_snapshot()
            threads = [self.store.get_thread(post_id) for post_id in self.post_ids]
            started = threads[0] if self.environment_type == 'moltnet_thread' else self._forum_snapshot()
            agent = self.agents[0]
            events = self.read_events()
            before = deepcopy(agent.memory.snapshot())
            initialized_before = agent._initialized
            call = ToolCall(call_id=f'{agent.profile.agent_id}-r1-read', name='read_thread', arguments={'agent_id': agent.profile.agent_id, 'post_ids': list(self.post_ids)})
            audit = {'read_post_count': len(threads), 'read_comment_count': sum((len(thread['comments']) for thread in threads)), 'read_event_count': len(events), 'read_character_count': sum((len(event.content) for event in events)), 'memory_revision_before': before.revision, 'memory_revision_after': before.revision}
            execution: dict[str, Any] = {'agent_id': agent.profile.agent_id, 'action': 'read_thread', 'tool_call': call.as_openai_like(), 'execution': {'tool': 'read_thread', 'result': {'simulation_mode': 'read_only', 'post_ids': list(self.post_ids), **audit}}, 'context_audit': {'agent_id': agent.profile.agent_id, 'behavior_llm_calls': 0, 'complete_thread_read': True}}
            try:
                progress_events = [*(agent.seed_events if not agent._initialized and agent.memory.revision == 0 else []), *events]
                with agent.memory_update_scope(progress_events, phase='read'):
                    await agent.initialize_memory()
                    await agent.remember_events(events)
                    await agent.finalize_memory()
                    self._save_memory_snapshot()
            except BaseException as exc:
                if getattr(agent.memory, 'preserve_progress_on_failure', False):
                    self._save_memory_snapshot()
                    execution['cloud_memory_paused'] = True
                else:
                    agent.memory.restore(before)
                    agent._initialized = initialized_before
                    agent.record_memory_rollback()
                if not isinstance(exc, Exception):
                    raise
                execution.update({'ok': False, 'error': str(exc)})
                if isinstance(exc, MemoryProviderResponseError):
                    execution['memory_provider_response'] = deepcopy(exc.diagnostics)
                elif getattr(exc, 'diagnostics', None):
                    execution['memory_provider_response'] = deepcopy(exc.diagnostics)
                execution['execution']['result'].update(memory_revision_after=agent.memory.revision, memory_updated=False, memory_build_complete=False)
            else:
                execution['ok'] = True
                execution['execution']['result'].update({'memory_revision_after': agent.memory.revision, 'memory_updated': agent.memory.revision != before.revision, 'skipped_memory_events': list(agent.memory.skipped_update_events)})
            record = {'round_index': 1, 'environment_type': self.environment_type, 'simulation_mode': 'read_only', 'scope_id': self.scope_id, 'post_ids': list(self.post_ids), 'memory_strategy': self.memory_strategy, 'activation_mode': 'all', 'population_agent_count': 1, 'active_agent_count': 1, 'active_agent_ids': [agent.profile.agent_id], 'action_counts': {'read_thread': len(threads)} if execution['ok'] else {}, 'executions': [execution]}
            if self.environment_type == 'moltnet_thread':
                record.update({'post_id': self.post_id, 'started_thread': started, 'thread': self.store.get_thread(self.post_id)})
            else:
                record.update({'started_forum': started, 'forum': self._forum_snapshot()})
            return record
        finally:
            self._run_lock.release()

    async def aclose(self) -> None:
        await asyncio.gather(*(agent.memory.aclose() for agent in self.agents))

    def read_events(self):
        agent = self.agents[0]
        return [replace(event, visibility='authored' if event.actor_id == agent.profile.agent_id else 'observed_read') for post_id in self.post_ids for event in thread_memory_events(self.store.get_thread(post_id), hide_speaker=self.hide_speaker, speaker_as_json=self.speaker_as_json)]

    def planned_memory_events(self):
        """Canonical event order, including seed events and first-occurrence dedup."""
        events = {}
        for event in [*self.agents[0].seed_events, *self.read_events()]:
            events.setdefault(event.event_id, event)
        return list(events.values())

    def _forum_snapshot(self) -> dict[str, Any]:
        counts = self.store.forum_counts(self.communities)
        return forum_snapshot(self.store, self.communities, post_limit=max(1, counts['posts']), comments_per_post=max(1, counts['comments']))

    def _refresh_memory_snapshot(self) -> None:
        agent = self.agents[0]
        if self.environment_type == 'moltnet_thread':
            persisted = self.store.get_memory_snapshot(post_id=self.post_id, agent_id=agent.profile.agent_id, strategy=agent.memory.strategy)
        else:
            persisted = self.store.get_environment_memory_snapshot(scope_id=self.scope_id, agent_id=agent.profile.agent_id, strategy=agent.memory.strategy)
        if persisted is not None and int(persisted['revision']) > agent.memory.revision:
            agent.memory.restore(MemorySnapshot.from_dict(persisted['snapshot']))

    def _save_memory_snapshot(self) -> None:
        agent = self.agents[0]
        snapshot = agent.memory.snapshot()
        fields = {'agent_id': agent.profile.agent_id, 'strategy': snapshot.strategy, 'revision': snapshot.revision, 'snapshot': snapshot.as_dict()}
        if self.environment_type == 'moltnet_thread':
            self.store.save_memory_snapshot(post_id=self.post_id, **fields)
        else:
            self.store.save_environment_memory_snapshot(scope_id=self.scope_id, **fields)
__all__ = ['ReadOnlySimulation']
