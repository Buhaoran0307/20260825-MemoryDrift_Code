from __future__ import annotations
from agent_posting_core.cloud_memory import CLOUD_STRATEGIES
from .cloud_config import cloud_runtime
from .memory_results import TracingLLMClient, export_remote_memories
import asyncio
import hashlib
import json
import re
import shutil
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import yaml
from agent_posting_core import ForumStore, LLMCallManager, LLMClient, ManagedLLMClient, OpenAICompatibleTransport, create_memory, forum_snapshot
from agent_posting_core.memory import MemoryEvent, MemorySnapshot
from agent_posting_core.post_selection_file import import_post_dataset, select_file_reader
from agent_posting_core.reading_simulation import ReadOnlySimulation
from .experiment_config import ExperimentConfig, MEMORY_MODEL_STRATEGIES

@dataclass(frozen=True)
class ExperimentRuntime:
    config: ExperimentConfig
    run_id: str
    run_dir: Path
    platform_db: Path
    environment_type: str
    post_ids: tuple[int, ...]
    source_post_ids: tuple[str, ...]
    communities: tuple[str, ...]
    selected_agent_ids: tuple[str, ...]

    @property
    def post_id(self) -> int:
        return self.post_ids[0]

    @property
    def source_post_id(self) -> str:
        return self.source_post_ids[0]
PreparedCallback = Callable[[ExperimentRuntime], None]
ProgressCallback = Callable[[dict[str, Any]], None]

class ExperimentRunner:
    """Prepare and execute one fully persisted MoltNet experiment run."""

    def __init__(self, config: ExperimentConfig, *, llm: LLMClient | None=None, on_progress: ProgressCallback | None=None, memory_account_registry_db_path: Path | None=None):
        if config.environment.kind not in {'moltnet_thread', 'moltnet_forum'} or config.environment.selection != 'file' or config.environment.imported_file is None or (config.simulation.mode != 'read_only') or (config.simulation.rounds != 1) or (not config.simulation.read_only_agent_id) or (config.agents.count != 1) or config.simulation.tools:
            raise ValueError('first-stage execution requires one file-based, single-agent read_only pass')
        self.config = config
        self.memory_account_registry_db_path = memory_account_registry_db_path
        self.injected_llm = llm
        self.store: ForumStore | None = None
        self.simulation: ReadOnlySimulation | None = None
        self.llm_manager: LLMCallManager | None = None
        self.llm = llm
        self.memory_llm = llm
        self.on_progress = on_progress
        self.runtime: ExperimentRuntime | None = None
        self._manifest: dict[str, Any] = {}
        self._events_path: Path | None = None
        self._memory_progress_lock = threading.Lock()

    def run(self, *, on_prepared: PreparedCallback | None=None, resume: bool=False) -> dict[str, Any]:
        if self.config.memory.strategy == 'agent_memory_preview':
            from .agent_memory_preview import AgentMemoryPreviewRunner
            if on_prepared is not None:
                raise ValueError('agent memory preview does not support an interactive prepared callback')
            return AgentMemoryPreviewRunner(self.config, llm=self.injected_llm, on_progress=self.on_progress).run(resume=resume)
        try:
            if resume:
                self._resume()
            else:
                self._prepare()
            assert self.runtime is not None
            assert self.store is not None
            assert self.simulation is not None
            if self._manifest.get('completed_rounds'):
                raise ValueError('completed read-only runs cannot be resumed')
            self._update_manifest(status='running', completed_at=None, error=None)
            if on_prepared is not None:
                on_prepared(self.runtime)
            record = self._run_one_round(1)
            failures = [item for item in record['executions'] if not item.get('ok', False)]
            if failures and self.config.memory.strategy in CLOUD_STRATEGIES:
                self._update_manifest(status='paused', completed_at=_now(), error='Cloud memory processing paused; accepted events are checkpointed')
                self._event('run_paused', {'round_index': 1})
                return dict(self._manifest)
            if self._manifest.get('successful_actions', 0) == 0:
                final_status = 'failed'
            elif self._manifest.get('error_rounds') or self._manifest.get('memory_write_errors'):
                final_status = 'completed_with_errors'
            else:
                final_status = 'completed'
            self._update_manifest(status=final_status, completed_at=_now())
            self._event('run_failed' if final_status == 'failed' else 'run_completed', {'status': final_status, 'completed_rounds': list(self._manifest['completed_rounds'])})
            return dict(self._manifest)
        except KeyboardInterrupt:
            if self._manifest:
                self._update_manifest(status='interrupted', completed_at=_now())
            self._event('run_interrupted', {})
            raise
        except Exception as exc:
            if self._manifest and self._manifest.get('status') != 'failed':
                self._update_manifest(status='failed', error=str(exc), completed_at=_now())
            self._event('run_failed', {'error': str(exc)})
            raise
        finally:
            if self.simulation is not None:
                asyncio.run(self.simulation.aclose())
            if self.store is not None:
                self.store.close()

    def _prepare(self) -> None:
        run_id = self._make_run_id()
        run_dir = self.config.output.root_dir / run_id
        if run_dir.exists():
            raise FileExistsError(f'experiment run directory already exists: {run_dir}')
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / 'rounds').mkdir()
        (run_dir / 'memory').mkdir()
        (run_dir / 'snapshots').mkdir()
        self._events_path = run_dir / 'events.jsonl'
        shutil.copy2(self.config.config_path, run_dir / 'config.yaml')
        _write_json(run_dir / 'config.resolved.json', self.config.public_dict())
        self._manifest = {'schema_version': 1, 'run_id': run_id, 'run_name': self.config.run.name, 'status': 'preparing', 'environment_type': self.config.environment.kind, 'simulation_mode': self.config.simulation.mode, 'memory_strategy': self.config.memory.strategy, 'memory_model': self.config.memory.model, 'created_at': _now(), 'target_rounds': self.config.simulation.rounds, 'activation_mode': self.config.simulation.activation.mode, 'population_agent_count': self.config.agents.count, 'active_agents_per_round': self.config.simulation.activation.agents_per_round or self.config.agents.count, 'completed_rounds': [], 'memory_event_progress': None, 'error_rounds': [], 'successful_actions': 0, 'failed_actions': 0, 'run_dir': str(run_dir), 'files': {'config': 'config.yaml', 'resolved_config': 'config.resolved.json', 'events': 'events.jsonl', 'database': 'simulation.sqlite'}}
        self._write_manifest()
        self._event('run_preparing', {'run_id': run_id})
        platform_db = run_dir / 'simulation.sqlite'
        self.store = ForumStore(platform_db)
        runtime = self._prepare_file_environment(platform_db=platform_db, run_id=run_id, run_dir=run_dir)
        self.runtime = runtime
        self._write_manifest()
        self._event('environment_prepared', {'environment_type': runtime.environment_type, 'post_ids': list(runtime.post_ids), 'source_post_ids': list(runtime.source_post_ids), 'communities': list(runtime.communities), 'selected_agent_ids': list(runtime.selected_agent_ids)})
        self._initialize_simulation(runtime)

    def _prepare_file_environment(self, *, platform_db: Path, run_id: str, run_dir: Path) -> ExperimentRuntime:
        assert self.store is not None
        imported_file = self.config.environment.imported_file
        assert imported_file is not None
        dataset = imported_file['dataset']
        assert self.config.simulation.read_only_agent_id is not None
        selected = select_file_reader(dataset, agent_id=self.config.simulation.read_only_agent_id, require_identity=self.config.agents.require_identity)
        restored = import_post_dataset(self.store, dataset, selected_ids=selected, history_limit=self.config.agents.history_limit)
        post_ids = tuple((item['post_id'] for item in restored['imports']))
        source_ids = tuple((post['id'] for post in dataset['posts']))
        communities = tuple(dict.fromkeys((post['community'] for post in dataset['posts'])))
        selected_ids = tuple((item['agent_id'] for item in restored['agents']['agents']))
        is_thread = self.config.environment.kind == 'moltnet_thread'
        selection = {'source': 'file', 'filename': imported_file['filename'], 'posts': dataset['posts'], **restored}
        if is_thread:
            selection['post'] = dataset['posts'][0]
            selection['import'] = restored['imports'][0]
        _write_json(run_dir / 'selection.json', selection)
        _write_json(run_dir / 'input-posts.json', dataset)
        initial = self.store.get_thread(post_ids[0]) if is_thread else forum_snapshot(self.store, communities, post_limit=self.config.environment.feed_limit)
        snapshot_name = 'thread-initial.json' if is_thread else 'forum-initial.json'
        _write_json(run_dir / 'snapshots' / snapshot_name, initial)
        self._manifest.update({'data_source': 'file', 'input_file': imported_file['filename'], 'post_ids': list(post_ids), 'source_post_ids': list(source_ids), 'communities': list(communities), 'selected_agent_ids': list(selected_ids), 'selection': 'selection.json', 'initial_post_count': len(post_ids), 'initial_comment_count': sum((len(p['comments']) for p in dataset['posts'])), 'action_counts': {}})
        self._manifest['files']['input_posts'] = 'input-posts.json'
        if is_thread:
            self._manifest.update(post_id=post_ids[0], source_post_id=source_ids[0])
        return ExperimentRuntime(config=self.config, run_id=run_id, run_dir=run_dir, platform_db=platform_db, environment_type=self.config.environment.kind, post_ids=post_ids, source_post_ids=source_ids, communities=communities, selected_agent_ids=selected_ids)

    def _resume(self) -> None:
        if self.config.memory.strategy not in CLOUD_STRATEGIES:
            raise ValueError('read_only runs read once and cannot add rounds; launch a new run')
        run_id = self._make_run_id()
        run_dir = self.config.output.root_dir / run_id
        manifest_path = run_dir / 'manifest.json'
        if not manifest_path.is_file():
            raise FileNotFoundError(f'experiment manifest does not exist: {manifest_path}')
        previous_config_path = run_dir / 'config.yaml'
        previous = _read_yaml(previous_config_path)
        current = _read_yaml(self.config.config_path)
        previous_rounds = current_rounds = 1
        if _resume_identity(previous) != _resume_identity(current):
            raise ValueError('cloud resume must keep the original experiment configuration')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if not isinstance(manifest, dict) or manifest.get('run_id') != run_id:
            raise ValueError('run manifest identity does not match the resumed config')
        completed = [int(value) for value in manifest.get('completed_rounds') or []]
        if completed or manifest.get('status') not in {'paused', 'failed', 'interrupted', 'queued'}:
            raise ValueError('only an unfinished cloud memory build can be resumed')
        platform_db = run_dir / 'simulation.sqlite'
        if not platform_db.is_file():
            raise FileNotFoundError(f'simulation database does not exist: {platform_db}')
        selected_ids = tuple((str(value) for value in manifest.get('selected_agent_ids') or []))
        if not selected_ids:
            raise ValueError('resumed manifest has no selected agents')
        environment_type = str(manifest.get('environment_type') or 'moltnet_thread')
        post_ids = tuple((int(value) for value in manifest.get('post_ids') or []))
        if not post_ids and manifest.get('post_id') is not None:
            post_ids = (int(manifest['post_id']),)
        if not post_ids:
            raise ValueError('resumed manifest has no environment posts')
        source_post_ids = tuple((str(value) for value in manifest.get('source_post_ids') or []))
        if not source_post_ids and manifest.get('source_post_id'):
            source_post_ids = (str(manifest['source_post_id']),)
        self.store = ForumStore(platform_db)
        communities = tuple((str(value) for value in manifest.get('communities') or []))
        if not communities:
            communities = tuple(dict.fromkeys((str(self.store.get_post(post_id)['community']) for post_id in post_ids)))
        self._manifest = manifest
        self._manifest['target_rounds'] = current_rounds
        self._events_path = run_dir / 'events.jsonl'
        _write_json(run_dir / 'config.resolved.json', self.config.public_dict())
        self.runtime = ExperimentRuntime(config=self.config, run_id=run_id, run_dir=run_dir, platform_db=platform_db, environment_type=environment_type, post_ids=post_ids, source_post_ids=source_post_ids, communities=communities, selected_agent_ids=selected_ids)
        self._initialize_simulation(self.runtime)
        self._write_manifest()
        self._event('run_resumed', {'completed_rounds': completed, 'previous_target_rounds': previous_rounds, 'target_rounds': current_rounds})

    def _managed_llm(self, model: str) -> LLMClient:
        assert self.llm_manager is not None
        client = ManagedLLMClient(OpenAICompatibleTransport(api_base=self.config.backend.api_base, api_key=self.config.backend.api_key, model=model, timeout=self.config.backend.timeout_seconds, temperature=self.config.backend.temperature, max_tokens=self.config.backend.max_tokens, max_transient_retries=self.config.backend.max_transient_retries, retry_backoff_seconds=self.config.backend.retry_backoff_seconds, max_retry_wait_seconds=self.config.backend.max_retry_wait_seconds, min_request_interval_seconds=self.config.backend.min_request_interval_seconds), self.llm_manager)
        if self.runtime is None:
            return client
        return TracingLLMClient(client, self.runtime.run_dir / 'memory' / 'model-calls.jsonl')

    def _initialize_simulation(self, runtime: ExperimentRuntime) -> None:
        assert self.store is not None
        strategy = self.config.memory.strategy
        memory_needs_llm = strategy not in {'mem0_platform_v3', *CLOUD_STRATEGIES}
        if self.injected_llm is None and memory_needs_llm:
            if not self.config.backend.api_key:
                raise ValueError(f'LLM API key is empty; set {self.config.backend.api_key_env}')
            self.llm_manager = LLMCallManager(self.config.backend.concurrency)
            if strategy not in MEMORY_MODEL_STRATEGIES:
                if not self.config.backend.model:
                    raise ValueError('the behavior/backend model is not configured')
                self.llm = self._managed_llm(self.config.backend.model)
            if strategy in MEMORY_MODEL_STRATEGIES:
                if self.config.memory.model is None:
                    raise ValueError(f'memory.model is required for {strategy}')
                self.memory_llm = self._managed_llm(self.config.memory.model)
            else:
                self.memory_llm = self.llm
        elif self.injected_llm is not None:
            self.memory_llm = TracingLLMClient(self.injected_llm, runtime.run_dir / 'memory' / 'model-calls.jsonl')
            self.llm = self.memory_llm
        simulation_llm = self.llm
        memory_factory = None
        if self.config.memory.strategy in CLOUD_STRATEGIES:

            def hosted_memory_factory(profile):
                return create_memory(self.config.memory.strategy, agent_id=profile.agent_id, options=self.config.memory.options, runtime=cloud_runtime(self.config.memory.strategy, self._manifest.get('memory_scope_run_id', runtime.run_id), api_keys=self.config.backend.cloud_api_keys, run_dir=runtime.run_dir, agent_id=profile.agent_id, registry_db_path=self.memory_account_registry_db_path))
            memory_factory = hosted_memory_factory
        elif self.config.memory.strategy == 'mem0_platform_v3':
            if not self.config.backend.mem0_api_key:
                raise ValueError(f'Mem0 API key is empty; set {self.config.backend.mem0_api_key_env}')

            def mem0_platform_memory_factory(profile):
                return create_memory(self.config.memory.strategy, agent_id=profile.agent_id, llm=self.llm, options=self.config.memory.options, runtime={'api_key': self.config.backend.mem0_api_key, 'run_id': runtime.run_id})
            memory_factory = mem0_platform_memory_factory
        elif self.config.memory.strategy == 'langmem_official':
            if self.injected_llm is None and (not self.config.backend.api_key):
                raise ValueError(f'LangMem memory model API key is empty; set {self.config.backend.api_key_env}')

            def langmem_official_memory_factory(profile):
                return create_memory(self.config.memory.strategy, agent_id=profile.agent_id, llm=self.memory_llm, options=self.config.memory.options, runtime={'api_base': self.config.backend.api_base, 'api_key': self.config.backend.api_key, 'model': self.config.memory.model, 'run_id': runtime.run_id, 'request_timeout_seconds': self.config.backend.timeout_seconds, 'max_retries': self.config.backend.max_transient_retries, 'memory_max_tokens': self.config.backend.max_tokens})
            memory_factory = langmem_official_memory_factory
        elif self.config.memory.strategy == 'a_mem_official':
            if self.injected_llm is None and (not self.config.backend.api_key):
                raise ValueError(f'A-Mem memory model API key is empty; set {self.config.backend.api_key_env}')

            def amem_official_memory_factory(profile):
                return create_memory(self.config.memory.strategy, agent_id=profile.agent_id, llm=self.memory_llm, options=self.config.memory.options)
            memory_factory = amem_official_memory_factory
        if strategy in MEMORY_MODEL_STRATEGIES and self.memory_llm is not None:
            simulation_llm = self.memory_llm
        self.simulation = ReadOnlySimulation(store=self.store, llm=simulation_llm, memory_strategy=self.config.memory.strategy, memory_options=self.config.memory.options, hide_speaker=self.config.environment.hide_speaker, speaker_as_json=self.config.environment.speaker_as_json, agent_ids=list(runtime.selected_agent_ids), environment_type=runtime.environment_type, post_ids=runtime.post_ids, scope_id=runtime.run_id, communities=runtime.communities, memory_factory=memory_factory)

    def _run_one_round(self, round_index: int) -> dict[str, Any]:
        assert self.runtime is not None
        assert self.store is not None
        assert self.simulation is not None
        started_at = _now()
        self._event('round_started', {'round_index': round_index})
        process_dir = self.runtime.run_dir / 'memory' / f'memory-round-{round_index:03d}'
        process_dir.mkdir(exist_ok=True)
        sequence = max((int(path.stem.removeprefix('memory-event-')) for path in process_dir.glob('memory-event-*.json') if path.stem.removeprefix('memory-event-').isdigit()), default=0)

        def save_event(event: MemoryEvent, snapshot: MemorySnapshot, phase: str, *, agent_id: str) -> None:
            nonlocal sequence
            sequence += 1
            checkpoint_path = process_dir / f'memory-event-{sequence:06d}.json'
            _write_json(checkpoint_path, {'run_id': self.runtime.run_id, 'round_index': round_index, 'sequence': sequence, 'saved_at': _now(), 'agent_id': agent_id, 'phase': phase, 'source_event': event.as_dict(), 'snapshot': snapshot.as_dict()})
        previous_callbacks = [agent.on_memory_event for agent in self.simulation.agents]
        previous_progress_callbacks = [agent.on_memory_progress for agent in self.simulation.agents]
        for agent in self.simulation.agents:
            agent.on_memory_event = lambda event, snapshot, phase, agent_id=agent.profile.agent_id: save_event(event, snapshot, phase, agent_id=agent_id)
            agent.on_memory_progress = lambda payload, agent_id=agent.profile.agent_id: self._record_memory_event_progress(agent_id, {**payload, 'round_index': round_index})
        try:
            record = asyncio.run(self.simulation.run_round(round_index))
            if self.config.memory.strategy in CLOUD_STRATEGIES and all((e.get('ok') for e in record['executions'])):
                try:
                    for agent in self.simulation.agents:
                        if agent.memory.snapshot().options.get('ingestion_status') != 'ready':
                            asyncio.run(agent.memory.finalize_ingestion())
                except Exception as exc:
                    for execution in record['executions']:
                        execution.update(ok=False, cloud_memory_paused=True, error=str(exc))
                        if getattr(exc, 'diagnostics', None):
                            execution['memory_provider_response'] = deepcopy(exc.diagnostics)
        finally:
            for agent, callback in zip(self.simulation.agents, previous_callbacks):
                agent.on_memory_event = callback
            for agent, callback in zip(self.simulation.agents, previous_progress_callbacks):
                agent.on_memory_progress = callback
        completed_at = _now()
        failures = [item for item in record['executions'] if not item.get('ok', False)]
        successes = [item for item in record['executions'] if item.get('ok', False)]
        memory_write_errors = [item for item in self._manifest.get('memory_write_errors', []) if item.get('round_index') == round_index]
        memory_export_errors: list[dict[str, str]] = []
        if not failures and self.config.memory.strategy in {'memos_cloud'}:
            exports: dict[str, Any] = {}
            for agent in self.simulation.agents:
                try:
                    exports[agent.profile.agent_id] = asyncio.run(export_remote_memories(agent.memory))
                except Exception as exc:
                    error = {'agent_id': agent.profile.agent_id, 'error': f'{type(exc).__name__}: {exc}'}
                    memory_export_errors.append(error)
                    exports[agent.profile.agent_id] = {'error': error['error']}
            _write_json(self.runtime.run_dir / 'memory' / f'remote-export-round-{round_index:03d}.json', exports)
        status = 'completed_with_errors' if failures or memory_write_errors else 'completed'
        if memory_write_errors:
            record['memory_write_errors'] = memory_write_errors
        if memory_export_errors:
            record['memory_export_errors'] = memory_export_errors
            self._manifest.setdefault('memory_export_errors', []).extend(memory_export_errors)
        record.update({'run_id': self.runtime.run_id, 'started_at': started_at, 'completed_at': completed_at, 'status': status, 'llm_calls': self._llm_stats()})
        self.store.save_simulation_round(run_id=self.runtime.run_id, round_index=round_index, post_id=self.runtime.post_id if self.runtime.environment_type == 'moltnet_thread' else None, environment_type=self.runtime.environment_type, scope_id=self.runtime.run_id if self.runtime.environment_type == 'moltnet_forum' else None, memory_strategy=self.config.memory.strategy, status=status, started_at=started_at, completed_at=completed_at, record=record)
        rounds_path = self.runtime.run_dir / 'rounds' / f'round-{round_index:03d}.json'
        _write_json(rounds_path, record)
        if self.config.output.save_thread_after_each_round:
            snapshot_name = f'thread-round-{round_index:03d}.json' if self.runtime.environment_type == 'moltnet_thread' else f'forum-round-{round_index:03d}.json'
            _write_json(self.runtime.run_dir / 'snapshots' / snapshot_name, record['thread' if self.runtime.environment_type == 'moltnet_thread' else 'forum'])
        if self.config.simulation.mode == 'read_only':
            if self.runtime.environment_type == 'moltnet_thread':
                snapshots = {agent.profile.agent_id: self.store.get_memory_snapshot(post_id=self.runtime.post_id, agent_id=agent.profile.agent_id, strategy=agent.memory.strategy) for agent in self.simulation.agents}
            else:
                snapshots = {agent.profile.agent_id: self.store.get_environment_memory_snapshot(scope_id=self.runtime.run_id, agent_id=agent.profile.agent_id, strategy=agent.memory.strategy) for agent in self.simulation.agents}
            if self.config.memory.strategy in CLOUD_STRATEGIES:
                snapshots = {agent.profile.agent_id: {'strategy': agent.memory.strategy, 'revision': agent.memory.revision, 'snapshot': agent.memory.snapshot().as_dict()} for agent in self.simulation.agents}
            _write_json(self.runtime.run_dir / 'memory' / f'memory-round-{round_index:03d}.json', snapshots)
        cloud_paused = failures and self.config.memory.strategy in CLOUD_STRATEGIES
        if not cloud_paused:
            self._manifest.setdefault('completed_rounds', []).append(round_index)
        self._manifest['successful_actions'] += len(successes)
        self._manifest['failed_actions'] += len(failures)
        if failures:
            self._manifest.setdefault('error_rounds', []).append(round_index)
        latest: dict[str, Any] = {'latest_round': round_index, 'latest_status': status, 'latest_active_agent_ids': record['active_agent_ids'], 'latest_round_file': str(rounds_path.relative_to(self.runtime.run_dir))}
        if self.runtime.environment_type == 'moltnet_thread':
            latest['latest_post_count'] = 1
            latest['latest_comment_count'] = len(record['thread']['comments'])
        else:
            latest['latest_post_count'] = record['forum']['total_post_count']
            latest['latest_comment_count'] = record['forum']['total_comment_count']
        action_counts = self._manifest.setdefault('action_counts', {})
        for name, count in record.get('action_counts', {}).items():
            action_counts[name] = int(action_counts.get(name) or 0) + int(count)
        self._manifest.update(latest)
        self._write_manifest()
        self._event('round_completed', {'round_index': round_index, 'status': status, 'post_count': latest['latest_post_count'], 'comment_count': latest['latest_comment_count'], 'active_agent_ids': record['active_agent_ids'], 'failure_count': len(failures)})
        return record

    def _llm_stats(self) -> dict[str, Any]:
        if self.llm_manager is None:
            return {}
        return self.llm_manager.stats()

    def _record_memory_event_progress(self, agent_id: str, payload: dict[str, Any]) -> None:
        """Persist observable source-event update progress for every strategy."""
        progress = {'agent_id': agent_id, 'strategy': str(payload.get('strategy') or self.config.memory.strategy), 'phase': str(payload.get('phase') or 'observe'), 'round_index': payload.get('round_index'), 'status': str(payload.get('status') or 'running'), 'completed_events': max(0, int(payload.get('completed_events') or 0)), 'processed_events': max(0, int(payload.get('processed_events') or 0)), 'skipped_events': max(0, int(payload.get('skipped_events') or 0)), 'total_events': max(0, int(payload.get('total_events') or 0)), 'progress_granularity': str(payload.get('progress_granularity') or 'event'), 'batch_events': max(0, int(payload.get('batch_events') or 0)), 'rolled_back': bool(payload.get('rolled_back', False)), 'rollback_scope': payload.get('rollback_scope'), 'remote_state_may_have_changed': bool(payload.get('remote_state_may_have_changed', False)), 'current_event_id': payload.get('current_event_id'), 'last_completed_event_id': payload.get('last_completed_event_id'), 'updated_at': _now()}
        with self._memory_progress_lock:
            for item in payload.get('skipped_event_details') or []:
                errors = self._manifest.setdefault('memory_write_errors', [])
                event_id = item['source_event']['event_id']
                if not any((error['agent_id'] == agent_id and error['source_event']['event_id'] == event_id for error in errors)):
                    error = {**item, 'agent_id': agent_id, 'round_index': progress['round_index'], 'phase': progress['phase'], 'recorded_at': progress['updated_at']}
                    errors.append(error)
                    self._event('memory_write_skipped', error)
            self._manifest['memory_event_progress'] = progress
            self._manifest.setdefault('memory_event_progress_by_agent', {})[agent_id] = progress
            self._write_manifest()
            self._event('memory_event_progress', progress)

    def _make_run_id(self) -> str:
        if self.config.run.run_id:
            return _safe_name(self.config.run.run_id)
        stamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
        base = f'{_safe_name(self.config.run.name)}-{stamp}'
        candidate = base
        index = 2
        while (self.config.output.root_dir / candidate).exists():
            candidate = f'{base}-{index:02d}'
            index += 1
        return candidate

    def _event(self, name: str, payload: dict[str, Any]) -> None:
        if self._events_path is None:
            return
        event = {'at': _now(), 'event': name, **payload}
        with self._events_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n')
            handle.flush()

    def _write_manifest(self) -> None:
        assert self.runtime is None or self.runtime.run_dir.is_dir()
        run_dir = Path(self._manifest['run_dir'])
        _write_json(run_dir / 'manifest.json', self._manifest)
        if self.on_progress is not None:
            self.on_progress(dict(self._manifest))

    def _update_manifest(self, **updates: Any) -> None:
        self._manifest.update(updates)
        self._write_manifest()

def _now() -> str:
    return datetime.now().astimezone().isoformat()

def _safe_name(value: str) -> str:
    result = re.sub('[^A-Za-z0-9._-]+', '-', value.strip()).strip('-.')
    return result or 'run'

def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    temporary.replace(path)

def _read_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'experiment config must be a mapping: {path}')
    return value

def _resume_identity(value: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(value))
    simulation = result.get('simulation')
    if not isinstance(simulation, dict):
        raise ValueError('experiment config simulation must be a mapping')
    result.pop('data', None)
    result['simulation'] = {'mode': simulation.get('mode', 'read_only'), 'read_only_agent_id': simulation.get('read_only_agent_id')}
    return result
__all__ = ['ExperimentRunner', 'ExperimentRuntime']
