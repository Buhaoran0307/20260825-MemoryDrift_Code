from __future__ import annotations
from agent_posting_core.cloud_memory import CLOUD_STRATEGIES
from .cloud_config import cloud_runtime, memory_scope_run_id
from .memory_results import inventory as memory_inventory, langmem_versions, amem_versions
import asyncio
import copy
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol
import yaml
from agent_posting_core.post_selection_file import parse_post_selection_file
from agent_posting_core.specified_memory import build_specified_memory_source
from agent_posting_core.llm import DEFAULT_LLM_TIMEOUT_SECONDS
from agent_posting_core.tool_selection import normalize_mcp_dataset, normalize_query_dataset, parse_mcp_file, parse_query_file, render_frozen_memory_for_query, validate_selection_inputs
from .config import PROJECT_ROOT, read_env_file
from .experiment_config import MEMORY_MODEL_STRATEGIES, SELECTABLE_EXPERIMENT_MODELS, ExperimentConfig, load_experiment_config
from .experiment_worker import _manifest_metrics
from .experiment import _resume_identity
from .platform_store import LEGACY_EXPERIMENT_GROUP_ID, PlatformStore, TERMINAL_RUN_STATUSES
from .validation_config import apply_query_target_overrides, resolve_tool_selection_memory_settings
from .platform_defaults import FIXED_LOCAL_MEMORY_MODEL, FIXED_LOCAL_MEMORY_STRATEGIES, output_root, read_platform_settings, validation_connection_for_model, write_platform_settings
from .validation_metrics import EXACT_LEAK_METRIC, validation_statistics
from .validation_sources import describe_memory_source, read_memory_source, read_source_config
from .run_display import read_run_display
DEFAULT_PLATFORM_DB = PROJECT_ROOT / 'output' / 'platform' / 'platform.sqlite'
DEFAULT_EXPERIMENT_CONFIG = PROJECT_ROOT / 'config' / 'experiment-default.yaml'
EXPERIMENT_ID_PATTERN = re.compile('^[a-z0-9][a-z0-9_-]{0,63}$')
RESOURCE_KINDS = {'posts', 'queries', 'mcp'}
ACTIVE_RUN_STATUSES = {'queued', 'starting', 'running'}

class JobLauncher(Protocol):

    def launch(self, *, run_id: str, platform_db: Path, worker_config: Path, log_path: Path) -> str:
        ...

    def is_active(self, session_name: str) -> bool:
        ...

    def stop(self, session_name: str) -> bool:
        ...

class ScreenJobLauncher:
    """Launch one experiment worker in a detached GNU screen session."""

    def __init__(self, *, project_root: Path=PROJECT_ROOT, python_executable: str | None=None, screen_executable: str='screen') -> None:
        self.project_root = project_root.resolve()
        self.python_executable = python_executable or sys.executable
        self.screen_executable = screen_executable

    def launch(self, *, run_id: str, platform_db: Path, worker_config: Path, log_path: Path) -> str:
        session_name = _screen_name(run_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [self.screen_executable, '-L', '-Logfile', str(log_path.resolve()), '-dmS', session_name, 'env', 'PYTHONUNBUFFERED=1', self.python_executable, str(self.project_root / 'script' / 'experiment_worker.py'), '--platform-db', str(platform_db.resolve()), '--run-id', run_id, '--config', str(worker_config.resolve())]
        completed = subprocess.run(command, cwd=self.project_root, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f'screen failed to start experiment worker ({completed.returncode}): {detail or shlex.join(command)}')
        return session_name

    def is_active(self, session_name: str) -> bool:
        completed = subprocess.run([self.screen_executable, '-ls', session_name], cwd=self.project_root, capture_output=True, text=True, check=False)
        output = f'{completed.stdout}\n{completed.stderr}'
        return session_name in output and 'No Sockets found' not in output

    def stop(self, session_name: str) -> bool:
        if not self.is_active(session_name):
            return True
        completed = subprocess.run([self.screen_executable, '-S', session_name, '-X', 'quit'], cwd=self.project_root, capture_output=True, text=True, check=False)
        if completed.returncode != 0 and self.is_active(session_name):
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f'screen failed to stop experiment worker ({completed.returncode}): {detail or session_name}')
        deadline = time.monotonic() + 5
        while self.is_active(session_name) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self.is_active(session_name)

class ProcessJobLauncher:
    """Detached Windows workers with file logs and restart-safe process identity."""

    def __init__(self, *, project_root: Path=PROJECT_ROOT, python_executable: str | None=None) -> None:
        self.project_root = project_root.resolve()
        self.python_executable = python_executable or sys.executable
        self._children: dict[str, subprocess.Popen] = {}

    def launch(self, *, run_id: str, platform_db: Path, worker_config: Path, log_path: Path) -> str:
        if os.name != 'nt':
            raise RuntimeError('ProcessJobLauncher is for Windows; use ScreenJobLauncher on this system')
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [self.python_executable, '-u', str(self.project_root / 'script' / 'experiment_worker.py'), '--platform-db', str(platform_db.resolve()), '--run-id', run_id, '--config', str(worker_config.resolve())]
        with log_path.open('ab') as log:
            process = subprocess.Popen(command, cwd=self.project_root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        state = _windows_process_state(process.pid)
        session = f'process-{process.pid}-{(state[1] if state else 0)}'
        self._children[session] = process
        return session

    def is_active(self, session_name: str) -> bool:
        child = self._children.get(session_name)
        if child is not None:
            active = child.poll() is None
            if not active:
                self._children.pop(session_name, None)
            return active
        match = re.fullmatch('process-(\\d+)-(\\d+)', session_name)
        if not match or os.name != 'nt':
            return False
        pid, expected_creation = (int(value) for value in match.groups())
        state = _windows_process_state(pid)
        return bool(state and state[0] and expected_creation and (state[1] == expected_creation))

    def stop(self, session_name: str) -> bool:
        child = self._children.get(session_name)
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            self._children.pop(session_name, None)
            return True
        match = re.fullmatch('process-(\\d+)-(\\d+)', session_name)
        if not match or os.name != 'nt':
            return not self.is_active(session_name)
        pid, expected_creation = (int(value) for value in match.groups())
        state = _windows_process_state(pid)
        if not state or not state[0]:
            return True
        if not expected_creation or state[1] != expected_creation:
            raise RuntimeError('worker process identity changed; refusing to stop it')
        _terminate_windows_process(pid)
        deadline = time.monotonic() + 5
        while self.is_active(session_name) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self.is_active(session_name)

def _windows_process_state(pid: int) -> tuple[bool, int] | None:
    """Check liveness and creation time without signalling or terminating a process."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(4096, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        created = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
        return (exit_code.value == 259, created)
    finally:
        kernel.CloseHandle(handle)

def _terminate_windows_process(pid: int) -> None:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(1, False, pid)
    if not handle:
        raise RuntimeError(f'cannot open worker process {pid} for termination')
    try:
        if not kernel.TerminateProcess(handle, 1):
            raise RuntimeError(f'cannot terminate worker process {pid}')
    finally:
        kernel.CloseHandle(handle)

class ExperimentPlatform:
    """Application service for experiment definitions and detached runs."""

    @property
    def memory_workspace(self):
        from .memory_workspace import MemoryWorkspace
        with self._launch_lock:
            if not hasattr(self, '_memory_workspace'):
                self._memory_workspace = MemoryWorkspace(self)
            return self._memory_workspace

    def prepare_memory_account_pool(self, source, requested):
        """Freeze one memory version and preserve an explicit account allowlist."""
        from agent_posting_core.memory_read_router import is_default_account
        if source['memory_strategy'] not in {'hindsight_cloud', 'mem0_platform_v3', 'memos_cloud'}:
            if requested and requested.get('enabled'):
                raise ValueError('account switching only supports Hindsight, Mem0 and MemOS')
            return None
        requested = {} if requested is None else requested
        if not isinstance(requested, dict) or type(requested.get('enabled', False)) is not bool:
            raise ValueError('memory_account_pool must contain a boolean enabled field')
        shared = requested.get('shared', True)
        if type(shared) is not bool:
            raise ValueError('memory_account_pool.shared must be boolean')
        ids = requested.get('account_ids', [])
        if not isinstance(ids, list) or any((not isinstance(item, str) for item in ids)):
            raise ValueError('memory account IDs must be a list of strings')
        ids = list(dict.fromkeys(ids))
        if not shared and (not ids):
            raise ValueError('non-shared memory account pools require at least one account ID')
        store = self.memory_workspace.store
        accounts = {}
        required_ids = set(ids) if not shared or (requested.get('enabled') and 'shared' not in requested) else set()
        for account_id in ids:
            account = store.resolve_account(account_id, source['memory_strategy'])
            if is_default_account(account):
                raise ValueError('The default account is export-only. Stage two can retrieve memory only from experiment accounts.')
            if account_id in required_ids and (not account.get('enabled')) or account['provider'] != source['memory_strategy']:
                raise ValueError('selected memory account is disabled or belongs to another provider')
        for public in store.list_accounts(provider=source['memory_strategy']):
            if not shared and public['account_id'] not in required_ids:
                continue
            if is_default_account(public) or not public.get('enabled'):
                continue
            try:
                account = store.resolve_account(public['account_id'], source['memory_strategy'])
            except (KeyError, ValueError):
                continue
            if not is_default_account(account) and account.get('enabled'):
                accounts[account['account_id']] = account
        replicas = store.list_replicas(source['run_id'], source['agent_id'], source['snapshot_sha256'])
        groups = {}
        for replica in replicas:
            if replica.get('verified') and replica.get('complete') and (replica.get('status') == 'ready') and (replica.get('provider') in {source['memory_strategy'], {'hindsight_cloud': 'hindsight', 'mem0_platform_v3': 'mem0', 'memos_cloud': 'memos'}[source['memory_strategy']]}) and (replica.get('account_id') in accounts) and (replica.get('credential_revision') == accounts[replica['account_id']].get('credential_revision')):
                key = (replica['memory_set_id'], replica['revision_hash'])
                groups.setdefault(key, []).append(replica)
        candidates = [(key, rows) for key, rows in groups.items() if required_ids <= {row['account_id'] for row in rows}]
        requested_revision = requested.get('revision_hash')
        requested_set = requested.get('memory_set_id')
        if bool(requested_revision) != bool(requested_set):
            raise ValueError('memory account pool must bind both memory_set_id and revision_hash')
        if requested_revision and requested_set:
            candidates = [(key, rows) for key, rows in candidates if key == (requested_set, requested_revision)]
        if not candidates:
            raise ValueError('Experiment accounts must contain verified complete replicas of the same memory version. Import the selected memory from the default account first.')
        key, _ = max(candidates, key=lambda candidate: max((row.get('updated_at', '') for row in candidate[1])))
        selected_ids = {row['account_id'] for row in groups[key]}
        ids = sorted(selected_ids, key=lambda account_id: (accounts[account_id].get('priority', 100), account_id))
        return {'enabled': True, 'shared': shared, 'account_ids': ids, 'memory_set_id': key[0], 'revision_hash': key[1], 'registry_db_path': str(self.store.db_path)}

    def export_experiments(self, destination: Path, **options) -> dict[str, Any]:
        from .experiment_bundle import export_bundle
        return export_bundle(self, destination, **options)

    def import_experiments(self, source: Path, **options) -> dict[str, Any]:
        from .experiment_bundle import import_bundle
        return import_bundle(self, source, **options)

    def transfer_experiments(self, **options) -> dict[str, Any]:
        from .experiment_group_operations import transfer_experiments
        return transfer_experiments(self, **options)

    def merge_experiment_groups(self, **options) -> dict[str, Any]:
        from .experiment_group_operations import merge_experiment_groups
        return merge_experiment_groups(self, **options)

    def __init__(self, *, project_root: Path=PROJECT_ROOT, platform_db: Path=DEFAULT_PLATFORM_DB, default_config_path: Path | None=None, launcher: JobLauncher | None=None) -> None:
        self.project_root = project_root.resolve()
        self.exp_dir = self.project_root / 'exp'
        config_path = default_config_path or Path('config/experiment-default.yaml')
        if not config_path.is_absolute():
            config_path = self.project_root / config_path
        self.default_config_path = config_path.resolve()
        self.platform_settings_path = self.project_root / 'config' / 'platform-defaults.json'
        self.runtime_dir = self.project_root / 'output' / 'platform'
        self.jobs_dir = self.runtime_dir / 'jobs'
        self.logs_dir = self.runtime_dir / 'logs'
        self.store = PlatformStore(platform_db)
        launcher_type = ProcessJobLauncher if os.name == 'nt' else ScreenJobLauncher
        self.launcher = launcher or launcher_type(project_root=self.project_root)
        self._launch_lock = threading.RLock()
        self.exp_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.store.delete_experiment_if_unused('demo')
        self.sync_exported_experiments()

    def create_resource(self, *, resource_id: str, kind: str, name: str, description: str, filename: str, content: str, environment_type: str | None=None) -> dict[str, Any]:
        resource_id = resource_id.strip().lower()
        if not EXPERIMENT_ID_PATTERN.fullmatch(resource_id):
            raise ValueError("resource_id must use 1-64 lowercase letters, numbers, '_' or '-'")
        kind = kind.strip().lower()
        if kind not in RESOURCE_KINDS:
            raise ValueError('resource kind must be posts, queries or mcp')
        if not name.strip():
            raise ValueError('resource name cannot be empty')
        if self.store.get_resource(resource_id) is not None:
            raise FileExistsError(f'Platform resource already exists: {resource_id}')
        normalized = _normalize_registered_resource(kind=kind, filename=filename, content=content, environment_type=environment_type)
        revision_id = f'{resource_id}-r0001'
        return self.store.create_resource(resource_id=resource_id, kind=kind, name=name.strip(), description=description.strip(), revision_id=revision_id, filename=normalized['filename'], environment_type=normalized.get('environment_type'), content=normalized['file'], content_sha256=normalized['content_sha256'], summary=normalized['summary'])

    def create_target_mapping(self, *, mapping_id: str, name: str, filename: str, content: str) -> dict[str, Any]:
        mapping_id = mapping_id.strip().lower()
        if not EXPERIMENT_ID_PATTERN.fullmatch(mapping_id):
            raise ValueError("mapping_id must use 1-64 lowercase letters, numbers, '_' or '-'")
        if not isinstance(name, str) or not name.strip():
            raise ValueError('target mapping name cannot be empty')
        mapping = _normalize_target_mapping(filename, content)
        return self.store.create_target_mapping(mapping_id=mapping_id, name=name.strip(), filename=filename.strip(), mapping=mapping, content_sha256=_resource_content_sha256(mapping))

    def preview_target_mapping(self, *, filename: str, content: str) -> dict[str, Any]:
        mapping = _normalize_target_mapping(filename, content)
        return {'filename': filename.strip(), 'summary': {'category_count': len(mapping)}, 'samples': [f'{category} → {server}'[:160] for category, server in list(mapping.items())[:3]]}

    def update_target_mapping_name(self, mapping_id: str, *, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise ValueError('target mapping name cannot be empty')
        return self.store.update_target_mapping_name(mapping_id.strip().lower(), name=name.strip())

    def preview_resource(self, *, kind: str, filename: str, content: str, environment_type: str | None=None) -> dict[str, Any]:
        if kind not in RESOURCE_KINDS:
            raise ValueError('resource kind must be posts, queries or mcp')
        normalized = _normalize_registered_resource(kind=kind, filename=filename, content=content, environment_type=environment_type)
        dataset = normalized['file']['dataset']
        rows = dataset.get({'posts': 'posts', 'queries': 'queries', 'mcp': 'servers'}[kind], [])
        samples = []
        for row in rows[:3]:
            if kind == 'posts':
                value = row.get('title') or row.get('body') or row.get('id')
            elif kind == 'queries':
                value = row.get('query') or row.get('query_id')
            else:
                value = row.get('name') or row.get('slug')
            samples.append(str(value or '')[:160])
        categories = sorted({str(row['functional_group']) for row in rows if row.get('functional_group')})
        return {'kind': kind, 'filename': normalized['filename'], 'environment_type': normalized['environment_type'], 'summary': normalized['summary'], 'samples': samples, 'categories': categories[:20], 'category_count': len(categories)}

    def list_target_mappings(self) -> list[dict[str, Any]]:
        return self.store.list_target_mappings()

    def set_group_target_mapping(self, group_id: str, mapping_id: str | None) -> dict[str, Any]:
        group_id = group_id.strip().lower()
        with self._launch_lock:
            group = self.get_experiment_group(group_id)
            if group.get('archived') or group.get('legacy'):
                raise PermissionError('this group cannot change its target mapping')
            if mapping_id != group.get('target_mapping_id') and any(((experiment.get('config') or {}).get('validation', {}).get('target_mode') == 'mapping' for experiment in group.get('experiments') or [])):
                raise PermissionError('group target mapping is locked by configured second-stage experiments')
            if mapping_id:
                mapping = self.store.get_target_mapping(mapping_id)
                if mapping is None:
                    raise ValueError('unknown target mapping')
                if mapping['content_sha256'] != _resource_content_sha256(mapping['mapping']):
                    raise ValueError('target mapping is missing or has changed')
                if group.get('mcp'):
                    _resolve_category_target_mapping(group['mcp']['dataset'], mapping['mapping'])
                if group.get('queries') and group.get('mcp'):
                    _apply_category_target_mapping(copy.deepcopy(group['queries']['dataset']), group['mcp']['dataset'], mapping['mapping'])
            self.store.set_group_target_mapping(group_id, mapping_id or None)
        return self.get_experiment_group(group_id)

    def add_resource_revision(self, resource_id: str, *, filename: str, content: str, environment_type: str | None=None) -> dict[str, Any]:
        resource_id = resource_id.strip().lower()
        resource = self.store.get_resource(resource_id)
        if resource is None:
            raise KeyError(resource_id)
        normalized = _normalize_registered_resource(kind=str(resource['kind']), filename=filename, content=content, environment_type=environment_type)
        next_number = int(resource.get('latest_revision_number') or 0) + 1
        revision = self.store.add_resource_revision(resource_id=resource_id, revision_id=f'{resource_id}-r{next_number:04d}', filename=normalized['filename'], environment_type=normalized.get('environment_type'), content=normalized['file'], content_sha256=normalized['content_sha256'], summary=normalized['summary'])
        return revision

    def list_resources(self, *, kind: str | None=None, archived: bool | None=False, include_content: bool=False) -> list[dict[str, Any]]:
        if kind is not None:
            kind = kind.strip().lower()
            if kind not in RESOURCE_KINDS:
                raise ValueError('resource kind must be posts, queries or mcp')
        resources = self.store.list_resources(kind=kind, archived=archived, include_content=include_content)
        return [_public_resource(resource, include_content=include_content) for resource in resources]

    def get_resource(self, resource_id: str, *, include_content: bool=True) -> dict[str, Any]:
        resource = self.store.get_resource(resource_id.strip().lower(), include_content=include_content)
        if resource is None:
            raise KeyError(resource_id)
        return _public_resource(resource, include_content=include_content)

    def update_resource_metadata(self, resource_id: str, *, name: str, description: str) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise ValueError('resource name cannot be empty')
        if not isinstance(description, str):
            raise ValueError('resource description must be text')
        return _public_resource(self.store.update_resource_metadata(resource_id.strip().lower(), name=name.strip(), description=description.strip()), include_content=False)

    def set_resource_archived(self, resource_id: str, *, archived: bool) -> dict[str, Any]:
        return _public_resource(self.store.set_resource_archived(resource_id.strip().lower(), archived=archived), include_content=False)

    def _resource_revision(self, revision_id: Any, *, expected_kind: str, allow_archived: bool) -> dict[str, Any]:
        if not isinstance(revision_id, str) or not revision_id.strip():
            raise ValueError(f'select a registered {expected_kind} resource revision')
        revision = self.store.get_resource_revision(revision_id.strip())
        if revision is None:
            raise ValueError(f'unknown registered resource revision: {revision_id}')
        if revision.get('kind') != expected_kind:
            raise ValueError(f"resource revision {revision_id} is {revision.get('kind')}, not {expected_kind}")
        if revision.get('resource_archived') and (not allow_archived):
            raise PermissionError('archived resources cannot be selected for new configurations')
        expected_hash = _resource_content_sha256(revision['content'])
        if expected_hash != revision.get('content_sha256'):
            raise ValueError(f'registered resource revision failed integrity check: {revision_id}')
        return revision

    def _group_with_registered_resources(self, group: dict[str, Any], *, include_content: bool=True, refs: dict[str, dict[str, Any]] | None=None) -> dict[str, Any]:
        result = dict(group)
        mapping_id = result.get('target_mapping_id')
        result['target_mapping'] = self.store.get_target_mapping(mapping_id) if mapping_id else None
        if refs is None:
            refs = self.store.get_experiment_group_resource_refs(str(group['group_id']), include_content=include_content)
        result['resource_refs'] = refs
        result['query_revision_id'] = (refs.get('queries') or {}).get('revision_id')
        result['mcp_revision_id'] = (refs.get('mcp') or {}).get('revision_id')
        for role in ('queries', 'mcp'):
            revision = refs.get(role)
            if revision is not None:
                key = 'query_revision_id' if role == 'queries' else 'mcp_revision_id'
                result[key] = revision['revision_id']
                public_resource = copy.deepcopy(revision['content']) if include_content else {'filename': revision.get('filename'), 'dataset': copy.deepcopy(revision.get('summary') or {})}
                result[role] = {**public_resource, 'resource_revision_id': revision['revision_id'], 'resource_id': revision['resource_id'], 'resource_name': revision.get('resource_name'), 'content_sha256': revision['content_sha256']}
        registered_policy = result.get('group_id') != LEGACY_EXPERIMENT_GROUP_ID and (bool(refs) or not (result.get('queries') or result.get('mcp')))
        result['resource_policy'] = 'registry' if registered_policy else 'legacy'
        registered_ready = registered_policy and all((not bool(revision.get('resource_archived')) for role, revision in refs.items() if role in {'queries', 'mcp'}))
        result['registered_resources_ready'] = registered_ready
        result['resources_ready'] = registered_ready or bool(not refs and result.get('queries') and result.get('mcp'))
        return result

    def assert_registered_resource_config(self, config: dict[str, Any], *, group_id: str | None=None, experiment_id: str | None=None) -> None:
        """Reject embedded uploads at the HTTP/UI boundary for future configs."""
        if not isinstance(config, dict):
            raise ValueError('experiment config must be an object')
        resolved_group_id = group_id.strip().lower() if isinstance(group_id, str) else ''
        if not resolved_group_id and isinstance(experiment_id, str) and experiment_id.strip():
            experiment = self.store.get_experiment(experiment_id.strip().lower())
            if experiment is None:
                raise KeyError(experiment_id)
            resolved_group_id = str(experiment.get('group_id') or '').strip().lower()
        if resolved_group_id:
            group = self.store.get_experiment_group(resolved_group_id)
            if group is None:
                raise ValueError('the selected experiment group does not exist')
            registered_group = self._group_with_registered_resources(group)
            if not registered_group.get('registered_resources_ready'):
                raise PermissionError('new configurations require an active experiment group using registered resources')
        environment = config.get('environment')
        if not isinstance(environment, dict):
            raise ValueError('environment settings must be an object')
        runner_type = _runner_type(config)
        mode_key = 'thread' if runner_type in {'moltnet_thread'} else 'forum'
        selection = environment.get(mode_key)
        uses_file = isinstance(selection, dict) and selection.get('selection') == 'file' or environment.get('file') is not None
        if runner_type != 'tool_selection' and uses_file:
            _registered_ref_id(environment.get('file'), label='environment.file', reject_embedded=True)
        if runner_type == 'tool_selection':
            validation = config.get('validation')
            if not isinstance(validation, dict):
                raise ValueError('second-stage validation settings must be an object')
            if validation.get('target_mode') == 'mapping':
                mapping_id = validation.get('target_mapping_id')
                if not isinstance(mapping_id, str) or self.store.get_target_mapping(mapping_id) is None:
                    raise ValueError('select a registered target mapping')
            for role in ('queries', 'mcp'):
                _registered_ref_id(validation.get(role), label=f'validation.{role}', reject_embedded=True)

    @staticmethod
    def _inherit_group_validation_resources(config: dict[str, Any], group: dict[str, Any]) -> None:
        validation = config.setdefault('validation', {})
        if not isinstance(validation, dict):
            raise ValueError('second-stage validation settings must be an object')
        group_refs = group.get('resource_refs') or {}
        for role in ('queries', 'mcp'):
            if role in validation:
                continue
            revision = group_refs.get(role)
            if revision is not None:
                validation[role] = {'resource_revision_id': revision['revision_id']}
            elif group.get(role) is not None:
                validation[role] = copy.deepcopy(group[role])

    def _materialize_registered_resources(self, config: dict[str, Any], *, allow_archived: bool) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Resolve immutable registry revisions into the legacy worker schema."""
        materialized = copy.deepcopy(config)
        bindings: dict[str, dict[str, Any]] = {}
        environment = materialized.get('environment')
        if not isinstance(environment, dict):
            raise ValueError('environment settings must be an object')
        runner_type = _runner_type(materialized)
        mode_key = 'thread' if runner_type in {'moltnet_thread'} else 'forum'
        selection = environment.get(mode_key)
        uses_file = isinstance(selection, dict) and selection.get('selection') == 'file' or environment.get('file') is not None
        if runner_type != 'tool_selection' and uses_file:
            revision = self._resource_revision(_registered_ref_id(environment.get('file'), label='environment.file'), expected_kind='posts', allow_archived=allow_archived)
            expected_environment = revision.get('environment_type')
            if expected_environment and expected_environment != runner_type:
                raise ValueError(f"posts revision {revision['revision_id']} is registered for {expected_environment}, not {runner_type}")
            selected_post_ids = environment['file'].get('selected_post_ids')
            selected_content = copy.deepcopy(revision['content'])
            if selected_post_ids is not None:
                if not isinstance(selected_post_ids, list) or not selected_post_ids or any((not isinstance(item, str) for item in selected_post_ids)) or (len(selected_post_ids) != len(set(selected_post_ids))):
                    raise ValueError('environment.file.selected_post_ids must contain distinct post IDs')
                posts = selected_content['dataset']['posts']
                by_id = {post['id']: post for post in posts}
                if any((post_id not in by_id for post_id in selected_post_ids)):
                    raise ValueError('selected post is missing from the registered revision')
                selected_content['dataset']['posts'] = [by_id[post_id] for post_id in selected_post_ids]
            environment['file'] = selected_content
            bindings['posts'] = revision
        if runner_type == 'tool_selection':
            validation = materialized.get('validation')
            if not isinstance(validation, dict):
                raise ValueError('second-stage validation settings must be an object')
            for role in ('queries', 'mcp'):
                revision = self._resource_revision(_registered_ref_id(validation.get(role), label=f'validation.{role}'), expected_kind=role, allow_archived=allow_archived)
                validation[role] = copy.deepcopy(revision['content'])
                bindings[role] = revision
            target_mode = validation.get('target_mode', 'custom')
            has_target_overrides = 'target_overrides' in validation
            target_overrides = validation.pop('target_overrides', None)
            if target_mode == 'mapping':
                mapping_id = validation.pop('target_mapping_id', None)
                mapping = self.store.get_target_mapping(mapping_id) if isinstance(mapping_id, str) else None
                if mapping is None or mapping['content_sha256'] != _resource_content_sha256(mapping['mapping']):
                    raise ValueError('target mapping is missing or has changed')
                _apply_category_target_mapping(validation['queries']['dataset'], validation['mcp']['dataset'], mapping['mapping'])
            if target_mode == 'custom':
                if not has_target_overrides:
                    raise ValueError('validation.target_overrides is required for custom targets')
                apply_query_target_overrides(validation['queries']['dataset'], target_overrides)
            elif has_target_overrides:
                raise ValueError('validation.target_overrides is only valid when target_mode is custom')
        return (materialized, bindings)

    def sync_exported_experiments(self) -> list[dict[str, Any]]:
        synced: list[dict[str, Any]] = []
        existing_by_id = {str(item['experiment_id']): item for item in self.store.list_experiments(archived=None)}
        for config_path in sorted(self.exp_dir.glob('*/config.yaml')):
            experiment_id = config_path.parent.name
            if experiment_id == 'demo':
                continue
            if not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
                continue
            existing = existing_by_id.get(experiment_id)
            if existing and int(existing.get('run_count') or 0) > 0:
                synced.append(existing)
                continue
            try:
                raw = _read_yaml(config_path)
                run_config = raw.get('run')
                name = str(run_config.get('name') or experiment_id) if isinstance(run_config, dict) else experiment_id
                runner_type = _runner_type(raw)
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                continue
            if existing and existing.get('config') == raw and (str(existing.get('name') or '') == name) and (str(existing.get('runner_type') or '') == runner_type) and (Path(str(existing.get('config_path') or '')).resolve() == config_path.resolve()):
                synced.append(existing)
                continue
            try:
                self._validate_config(raw)
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                continue
            description = existing['description'] if existing else ''
            synced.append(self.store.upsert_experiment(experiment_id=experiment_id, name=name, description=description, runner_type=runner_type, config_path=config_path, config=raw))
        return synced

    def create_experiment_group(self, *, group_id: str, name: str, description: str, queries: dict[str, Any] | None=None, mcp: dict[str, Any] | None=None, query_revision_id: str | None=None, mcp_revision_id: str | None=None, target_mapping_id: str | None=None) -> dict[str, Any]:
        group_id = group_id.strip().lower()
        if not EXPERIMENT_ID_PATTERN.fullmatch(group_id):
            raise ValueError("group_id must use 1-64 lowercase letters, numbers, '_' or '-'")
        if group_id == LEGACY_EXPERIMENT_GROUP_ID:
            raise ValueError('the legacy experiment group is reserved for migrated data')
        if not name.strip():
            raise ValueError('experiment group name cannot be empty')
        registered = query_revision_id is not None or mcp_revision_id is not None or (queries is None and mcp is None)
        if registered:
            if queries is not None or mcp is not None:
                raise ValueError('choose registered revisions instead of embedded group resources')
            query_revision = self._resource_revision(query_revision_id, expected_kind='queries', allow_archived=False) if query_revision_id is not None else None
            mcp_revision = self._resource_revision(mcp_revision_id, expected_kind='mcp', allow_archived=False) if mcp_revision_id is not None else None
            normalized_queries = query_revision['content'] if query_revision is not None else None
            normalized_mcp = mcp_revision['content'] if mcp_revision is not None else None
        else:
            normalized_queries = _normalize_group_resource(queries, name='queries', normalize=normalize_query_dataset)
            normalized_mcp = _normalize_group_resource(mcp, name='mcp', normalize=normalize_mcp_dataset)
        if normalized_queries is not None and normalized_mcp is not None:
            validate_selection_inputs(normalized_queries['dataset'], normalized_mcp['dataset'])
        if target_mapping_id:
            mapping = self.store.get_target_mapping(target_mapping_id)
            if mapping is None:
                raise ValueError('unknown target mapping')
            if mapping['content_sha256'] != _resource_content_sha256(mapping['mapping']):
                raise ValueError('target mapping is missing or has changed')
            if normalized_mcp is not None:
                _resolve_category_target_mapping(normalized_mcp['dataset'], mapping['mapping'])
            if normalized_queries is not None and normalized_mcp is not None:
                _apply_category_target_mapping(copy.deepcopy(normalized_queries['dataset']), normalized_mcp['dataset'], mapping['mapping'])
        with self._launch_lock:
            if self.store.get_experiment_group(group_id) is not None:
                raise FileExistsError(f'Experiment group already exists: {group_id}')
            self.store.create_experiment_group(group_id=group_id, name=name.strip(), description=description.strip(), queries=None if registered else normalized_queries, mcp=None if registered else normalized_mcp, query_revision_id=query_revision_id if registered else None, mcp_revision_id=mcp_revision_id if registered else None, target_mapping_id=target_mapping_id or None)
        return self.get_experiment_group(group_id)

    def list_experiment_groups(self, *, archived: bool | None=False) -> list[dict[str, Any]]:
        groups = self.store.list_experiment_groups(archived=archived, include_resources=False)
        refs_by_group = self.store.get_experiment_group_resource_refs_for_groups([str(group['group_id']) for group in groups], include_content=False)
        return [self._experiment_group_lifecycle(self._group_with_registered_resources(group, include_content=False, refs=refs_by_group.get(str(group['group_id']), {}))) for group in groups]

    def get_experiment_group(self, group_id: str) -> dict[str, Any]:
        group_id = group_id.strip().lower()
        group = self.store.get_experiment_group(group_id)
        if group is None:
            raise KeyError(group_id)
        group = self._group_with_registered_resources(group)
        experiments = self.list_experiments(archived=None, group_id=group_id)
        stage_two = [experiment for experiment in experiments if experiment.get('runner_type') == 'tool_selection']
        stage_one = [experiment for experiment in experiments if experiment.get('runner_type') != 'tool_selection']
        result = self._experiment_group_lifecycle(group)
        result['stage_one_experiments'] = stage_one
        result['stage_two_experiments'] = stage_two
        result['experiments'] = [*stage_one, *stage_two]
        return result

    @staticmethod
    def _experiment_group_lifecycle(group: dict[str, Any]) -> dict[str, Any]:
        result = dict(group)
        archived = bool(result.get('archived'))
        active_run_count = int(result.get('active_run_count') or 0)
        is_legacy = result.get('group_id') == LEGACY_EXPERIMENT_GROUP_ID
        result['can_archive'] = not is_legacy and (not archived) and (active_run_count == 0)
        result['can_restore'] = not is_legacy and archived
        result['can_delete'] = not is_legacy and archived and (active_run_count == 0)
        result['can_delete_direct'] = not is_legacy and active_run_count == 0
        result['is_legacy'] = is_legacy
        result['can_create'] = not is_legacy and (not archived) and bool(result.get('registered_resources_ready'))
        return result

    def set_experiment_group_archived(self, group_id: str, *, archived: bool) -> dict[str, Any]:
        group_id = group_id.strip().lower()
        with self._launch_lock:
            group = self.store.get_experiment_group(group_id)
            if group is None:
                raise KeyError(group_id)
            if group_id == LEGACY_EXPERIMENT_GROUP_ID:
                raise PermissionError('the legacy experiment group cannot be archived or restored as a whole')
            if bool(group.get('archived')) == archived:
                return self.get_experiment_group(group_id)
            if archived:
                for experiment in self.store.list_experiments(archived=None, group_id=group_id):
                    experiment_id = str(experiment['experiment_id'])
                    for registered in self._experiment_run_records(experiment_id):
                        current = self.get_run(str(registered['run_id']))
                        if current['status'] in ACTIVE_RUN_STATUSES or current['screen_active']:
                            raise PermissionError('experiment groups with active runs cannot be archived: ' + experiment_id)
            self.store.set_experiment_group_archived(group_id, archived=archived)
        return self.get_experiment_group(group_id)

    def delete_experiment_group(self, group_id: str, *, require_archived: bool=True) -> dict[str, Any]:
        group_id = group_id.strip().lower()
        with self._launch_lock:
            group = self.store.get_experiment_group(group_id)
            if group is None:
                raise KeyError(group_id)
            if group_id == LEGACY_EXPERIMENT_GROUP_ID:
                raise PermissionError('the legacy experiment group cannot be deleted')
            if require_archived and (not group.get('archived')):
                raise PermissionError('archive the experiment group before deletion')
            members = self.store.list_experiments(archived=None, group_id=group_id)
            member_ids = [str(item['experiment_id']) for item in members]
            member_id_set = set(member_ids)
            outside_experiments = [item for item in self.store.experiment_deletion_index() if item.get('group_id') != group_id]
            outside_links = sorted({str(item['experiment_id']) for item in outside_experiments if member_id_set.intersection(_experiment_source_references(item))})
            if outside_links:
                raise PermissionError('experiments outside the group reference its members: ' + ', '.join(outside_links))
            definitions: list[tuple[Path, Path]] = []
            runs: list[tuple[dict[str, Any], tuple[Path, ...]]] = []
            for experiment_id in member_ids:
                experiment = self.get_experiment(experiment_id)
                if require_archived and (not experiment.get('archived')):
                    raise PermissionError('group members must be archived before deletion: ' + experiment_id)
                definitions.append(self._validated_experiment_definition(experiment))
                for registered in self._experiment_run_records(experiment_id):
                    run = self.get_run(str(registered['run_id']))
                    if run['status'] in ACTIVE_RUN_STATUSES or run['screen_active']:
                        raise PermissionError('experiment groups with active runs cannot be deleted: ' + experiment_id)
                    runs.append((run, self._validated_run_artifacts(run)))
            paths_to_stage = [path for _, artifacts in runs for path in artifacts]
            if not require_archived and self.exp_dir.resolve().parent != self.project_root.resolve():
                raise PermissionError('experiment definition directory is outside the project tree')
            paths_to_stage.extend((config_path if require_archived else config_dir for config_path, config_dir in definitions))
            memory_cleanup = self.store.experiment_group_memory_cleanup(group_id)
            memory_workspace = getattr(self, '_memory_workspace', None)
            if memory_workspace is not None and any((memory_workspace._threads.get(job_id) is not None and memory_workspace._threads[job_id].is_alive() for job_id in memory_cleanup['job_ids'])):
                raise PermissionError('experiment groups with active memory transfer workers cannot be deleted')
            workspace = self.store.db_path.parent / 'memory_workspace'
            package_root = workspace / 'packages'
            if memory_cleanup['package_files'] and (workspace.is_symlink() or package_root.is_symlink() or workspace.resolve().parent != self.store.db_path.parent.resolve() or (package_root.resolve().parent != workspace.resolve())):
                raise PermissionError('memory package directory is outside the platform workspace')
            protected_packages = {(workspace / relative).resolve() for relative in memory_cleanup['protected_package_files']}
            package_paths = set()
            for relative in memory_cleanup['package_files']:
                candidate = workspace / relative
                if Path(relative).is_absolute() or candidate.resolve().parent != package_root.resolve() or candidate.suffix != '.json':
                    raise PermissionError('memory package path is outside the registered package directory')
                if candidate.resolve() not in protected_packages:
                    package_paths.add(candidate)
            paths_to_stage.extend(sorted(package_paths))
            removal_paths = {path.resolve() for path in paths_to_stage}
            overlapping = []
            if removal_paths:
                protected_paths = [(owner, Path(path).resolve()) for owner, path in self.store.registered_artifact_paths(exclude_group_id=group_id)]
                overlapping = sorted({owner for owner, protected_path in protected_paths if any((protected_path == path or path in protected_path.parents or protected_path in path.parents for path in removal_paths))})
            if overlapping:
                raise PermissionError('registered artifacts outside the group overlap its deletion paths: ' + ', '.join(overlapping))
            cleanup_pending = self._commit_with_quarantined_paths(paths_to_stage, operation='experiment group deletion', stage_prefix='group-' + group_id, commit=lambda: self.store.delete_experiment_group(group_id, expected_experiment_ids=member_ids, require_archived=require_archived, expected_memory_cleanup=memory_cleanup))
            for _, config_dir in definitions:
                if config_dir.is_dir():
                    try:
                        config_dir.rmdir()
                    except OSError:
                        pass
        result = {'deleted': True, 'group_id': group_id, 'deleted_experiment_ids': member_ids, 'deleted_experiment_count': len(member_ids), 'deleted_runs': len(runs), 'deleted_memory_jobs': len(memory_cleanup['job_ids']), 'deleted_memory_replicas': len(memory_cleanup['replica_ids']), 'deleted_memory_caches': len(memory_cleanup['cache_keys']), 'deleted_memory_packages': len(package_paths)}
        if cleanup_pending is not None:
            result['cleanup_pending'] = cleanup_pending
        return result

    def delete_experiment_groups(self, group_ids: Any) -> dict[str, Any]:
        """Directly delete selected idle groups, reporting failures per group."""
        if not isinstance(group_ids, list) or not group_ids:
            raise ValueError('group_ids must be a non-empty array')
        if any((not isinstance(group_id, str) or not EXPERIMENT_ID_PATTERN.fullmatch(group_id) for group_id in group_ids)):
            raise ValueError('every group id must be a valid lowercase experiment group ID')
        ids = list(dict.fromkeys(group_ids))
        if len(ids) > 100:
            raise ValueError('delete at most 100 experiment groups at once')
        deleted: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        with self._launch_lock:
            experiments = self.store.experiment_deletion_index()
            groups_by_experiment = {str(experiment['experiment_id']): str(experiment['group_id']) for experiment in experiments}
            dependents: dict[str, set[str]] = {group_id: set() for group_id in ids}
            for experiment in experiments:
                dependent_group = str(experiment['group_id'])
                if dependent_group not in dependents:
                    continue
                for source_id in _experiment_source_references(experiment):
                    source_group = groups_by_experiment.get(source_id)
                    if source_group in dependents and source_group != dependent_group:
                        dependents[source_group].add(dependent_group)
            pending = set(ids)
            ordered: list[str] = []
            while pending:
                ready = [group_id for group_id in ids if group_id in pending and (not dependents[group_id].intersection(pending))]
                if not ready:
                    ready = [group_id for group_id in ids if group_id in pending]
                ordered.extend(ready)
                pending.difference_update(ready)
            for group_id in ordered:
                try:
                    deleted.append(self.delete_experiment_group(group_id, require_archived=False))
                except Exception as exc:
                    failures.append({'group_id': group_id, 'error': str(exc)})
        return {'requested_group_ids': ids, 'requested_count': len(ids), 'deleted': deleted, 'deleted_count': len(deleted), 'deleted_experiment_count': sum((item['deleted_experiment_count'] for item in deleted)), 'deleted_runs': sum((item['deleted_runs'] for item in deleted)), 'failures': failures, 'failed_count': len(failures)}

    def delete_experiment_from_group(self, group_id: str, experiment_id: str) -> dict[str, Any]:
        group_id = group_id.strip().lower()
        with self._launch_lock:
            experiment = self.store.get_experiment(experiment_id)
            if experiment is None:
                raise KeyError(experiment_id)
            if experiment.get('group_id') != group_id:
                raise PermissionError('experiment does not belong to this group')
            return self.delete_experiment(experiment_id)

    def list_experiments(self, *, archived: bool | None=False, group_id: str | None=None, stage: str | None=None) -> list[dict[str, Any]]:
        if group_id is not None:
            group_id = group_id.strip().lower()
        if stage is not None:
            stage = stage.strip()
            if stage not in {'interaction', 'tool_selection'}:
                raise ValueError('stage must be interaction or tool_selection')
        all_experiments = self.store.list_experiments(archived=None, include_config=False)
        dependency_index = _experiment_dependency_index(all_experiments)
        invalid_dependency_index = _invalid_experiment_dependency_index(all_experiments)
        experiments_by_id = {str(experiment['experiment_id']): experiment for experiment in all_experiments}
        if archived is None and group_id is None and (stage is None):
            experiments = all_experiments
        else:
            experiments = self.store.list_experiments(archived=archived, group_id=group_id, stage=stage, include_config=False)
        return [self._experiment_lifecycle(experiment | self._experiment_relationships(experiment, experiments_by_id=experiments_by_id, dependency_index=dependency_index, invalid_dependency_index=invalid_dependency_index)) for experiment in experiments]

    def get_experiment(self, experiment_id: str) -> dict[str, Any]:
        experiment = self.store.get_experiment(experiment_id)
        if experiment is None:
            raise KeyError(experiment_id)
        experiment['runs'] = self.list_runs(experiment_id=experiment_id, limit=100, lightweight=True)
        experiment['configuration_locked'] = bool(experiment['runs'])
        experiment['latest_run_status'] = experiment['runs'][0]['status'] if experiment['runs'] else None
        experiment['latest_run_id'] = experiment['runs'][0]['run_id'] if experiment['runs'] else None
        experiment['active_run_count'] = int(experiment.get('active_run_count') or 0)
        experiments = self.store.list_experiments(archived=None, include_config=False)
        experiments_by_id = {str(item['experiment_id']): item for item in experiments}
        experiments_by_id[experiment_id] = experiment
        dependency_index = _experiment_dependency_index(experiments)
        invalid_dependency_index = _invalid_experiment_dependency_index(experiments)
        experiment.update(self._experiment_relationships(experiment, experiments_by_id=experiments_by_id, dependency_index=dependency_index, invalid_dependency_index=invalid_dependency_index))
        return self._experiment_lifecycle(experiment)

    def _experiment_relationships(self, experiment: dict[str, Any], *, experiments_by_id: dict[str, dict[str, Any]], dependency_index: dict[str, list[str]], invalid_dependency_index: dict[str, list[str]]) -> dict[str, Any]:
        experiment_id = str(experiment['experiment_id'])
        source_references = _experiment_source_references(experiment)
        missing_source_ids = [source_id for source_id in source_references if source_id not in experiments_by_id]
        cross_group_source_ids = [source_id for source_id in source_references if source_id in experiments_by_id and experiments_by_id[source_id].get('group_id') != experiment.get('group_id')]
        direct_ids = list(dependency_index.get(experiment_id, []))
        delete_ids = [experiment_id, *direct_ids]
        delete_records = [experiments_by_id[target_id] for target_id in delete_ids if target_id in experiments_by_id]
        blocking_active = [str(item['experiment_id']) for item in delete_records if int(item.get('active_run_count') or 0) > 0]
        blocking_unarchived = [str(item['experiment_id']) for item in delete_records if not bool(item.get('archived'))]
        blocking_invalid = sorted({dependent_id for target_id in delete_ids for dependent_id in invalid_dependency_index.get(target_id, [])})
        return {'source_experiment_ids': source_references, 'missing_source_experiment_ids': missing_source_ids, 'cross_group_source_experiment_ids': cross_group_source_ids, 'is_orphaned_stage_two': bool(missing_source_ids), 'dependent_experiment_ids': direct_ids, 'dependent_experiment_count': len(direct_ids), 'delete_experiment_ids': delete_ids, 'delete_experiment_count': len(delete_ids), 'delete_run_count': sum((int(item.get('run_count') or 0) for item in delete_records)), 'delete_blocking_active_ids': blocking_active, 'delete_blocking_unarchived_ids': blocking_unarchived, 'delete_blocking_invalid_ids': blocking_invalid}

    def _experiment_lifecycle(self, experiment: dict[str, Any]) -> dict[str, Any]:
        result = dict(experiment)
        archived = bool(result.get('archived'))
        active_run_count = int(result.get('active_run_count') or 0)
        dependent_ids = list(result.get('dependent_experiment_ids') or [])
        blocking_active = list(result.get('delete_blocking_active_ids') or [])
        blocking_unarchived = list(result.get('delete_blocking_unarchived_ids') or [])
        blocking_invalid = list(result.get('delete_blocking_invalid_ids') or [])
        result['dependent_experiment_ids'] = dependent_ids
        result['dependent_experiment_count'] = len(dependent_ids)
        result['delete_experiment_ids'] = list(result.get('delete_experiment_ids') or [result['experiment_id']])
        result['delete_experiment_count'] = len(result['delete_experiment_ids'])
        result['delete_run_count'] = int(result.get('delete_run_count') or result.get('run_count') or 0)
        group_archived = bool(result.get('group_archived'))
        result['can_archive'] = not archived and active_run_count == 0
        result['can_restore'] = archived and (not group_archived)
        result['can_delete'] = archived and (not blocking_active) and (not blocking_unarchived) and (not blocking_invalid)
        if blocking_active:
            result['action_block_reason'] = 'active_runs'
        elif not archived:
            result['action_block_reason'] = 'archive_before_delete'
        elif blocking_unarchived:
            result['action_block_reason'] = 'linked_experiments_not_ready'
        elif blocking_invalid:
            result['action_block_reason'] = 'linked_experiments_invalid'
        else:
            result['action_block_reason'] = None
        return result

    def save_experiment(self, *, experiment_id: str, group_id: str | None=None, name: str, description: str, config: dict[str, Any], create_only: bool) -> dict[str, Any]:
        with self._launch_lock:
            return self._save_experiment_locked(experiment_id=experiment_id, group_id=group_id, name=name, description=description, config=config, create_only=create_only)

    def _save_experiment_locked(self, *, experiment_id: str, group_id: str | None, name: str, description: str, config: dict[str, Any], create_only: bool) -> dict[str, Any]:
        experiment_id = experiment_id.strip().lower()
        runner_type = _runner_type(config)
        validation = config.get('validation') if runner_type == 'tool_selection' else None
        requested_source_experiment_id = validation.get('source_experiment_id') if isinstance(validation, dict) else None
        if not experiment_id:
            experiment_id = self.suggest_experiment_id(stage=runner_type, source_experiment_id=requested_source_experiment_id, group_id=group_id)
        if not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
            raise ValueError("experiment_id must use 1-64 lowercase letters, numbers, '_' or '-'")
        name = name.strip() or experiment_id
        existing = self.store.get_experiment(experiment_id)
        if create_only and existing is not None:
            raise FileExistsError(f'Experiment already exists: {experiment_id}')
        requested_group_id = group_id.strip().lower() if isinstance(group_id, str) and group_id.strip() else None
        if create_only and requested_group_id is None:
            raise ValueError('group_id is required when creating an experiment')
        if existing is None:
            resolved_group_id = requested_group_id
        else:
            resolved_group_id = str(existing.get('group_id') or '').strip().lower()
            if requested_group_id is not None and requested_group_id != resolved_group_id:
                raise PermissionError('an experiment cannot be moved to another group')
        if not resolved_group_id:
            raise ValueError('the experiment must belong to an experiment group')
        group = self.store.get_experiment_group(resolved_group_id)
        if group is None:
            raise ValueError('the selected experiment group does not exist')
        group = self._group_with_registered_resources(group)
        if group.get('archived'):
            raise PermissionError('archived experiment groups cannot create or edit experiments')
        if create_only and resolved_group_id == LEGACY_EXPERIMENT_GROUP_ID:
            raise PermissionError('new experiments cannot be added to the legacy experiment group')
        if create_only and (not group.get('resources_ready')):
            raise PermissionError('the selected experiment group has inactive registered resource defaults')
        if existing is not None and existing.get('archived'):
            raise PermissionError('archived experiments must be restored before they can be edited')
        if existing is not None and self.store.list_runs(experiment_id=experiment_id, limit=1):
            raise PermissionError('experiment configuration is locked after its first run; create a new experiment to change environment or memory settings')
        normalized = copy.deepcopy(config)
        normalized.setdefault('version', 1)
        if _runner_type(normalized) != 'tool_selection':
            memory = normalized.get('memory')
            if isinstance(memory, dict) and memory.get('strategy') in FIXED_LOCAL_MEMORY_STRATEGIES:
                memory['model'] = FIXED_LOCAL_MEMORY_MODEL
        run = normalized.setdefault('run', {})
        if not isinstance(run, dict):
            raise ValueError('run config must be an object')
        run['name'] = name.strip()
        run['id'] = None
        if _runner_type(normalized) == 'tool_selection':
            if group.get('resources_ready'):
                self._inherit_group_validation_resources(normalized, group)
            normalized = self._prepare_tool_selection_config(normalized, previous=existing['config'] if existing else None, group_id=resolved_group_id)
            self._apply_default_model_connection(normalized, previous=existing)
            normalized.setdefault('output', {'root_dir': f'output/experiments/{experiment_id}'})
        registered_policy = group.get('resource_policy') == 'registry'
        if registered_policy:
            materialized, bindings = self._materialize_registered_resources(normalized, allow_archived=False)
            validated = self._validate_config(materialized)
        else:
            bindings = {}
            validated = self._validate_config(normalized)
        if validated.validation is not None and (not registered_policy):
            normalized['validation'] = copy.deepcopy(validated.validation)
            normalized['simulation'] = {'rounds': validated.simulation.rounds}
        elif validated.validation is not None:
            normalized['simulation'] = {'rounds': validated.simulation.rounds}
            if 'mcp_parameter_additions' in validated.validation:
                normalized['validation']['mcp_parameter_additions'] = copy.deepcopy(validated.validation['mcp_parameter_additions'])
        else:
            normalized.pop('data', None)
            normalized['simulation'] = validated.public_dict()['simulation']
        config_path = self.exp_dir / experiment_id / 'config.yaml'
        _write_yaml(config_path, normalized)
        return self.store.upsert_experiment(experiment_id=experiment_id, name=name.strip(), description=description.strip(), runner_type=_runner_type(normalized), config_path=config_path, config=normalized, group_id=resolved_group_id, resource_policy='registry' if registered_policy else 'legacy', resource_refs={role: str(revision['revision_id']) for role, revision in bindings.items()} if registered_policy else None) | {'resolved': validated.public_dict()}

    def set_experiment_archived(self, experiment_id: str, *, archived: bool) -> dict[str, Any]:
        if archived:
            return self.archive_experiments([experiment_id])['experiments'][0]
        with self._launch_lock:
            experiment = self.get_experiment(experiment_id)
            if not experiment.get('archived'):
                return experiment
            if experiment.get('group_archived'):
                raise PermissionError('restore the experiment group before restoring a member experiment')
            self.store.set_experiment_archived(experiment_id, archived=False)
        return self.get_experiment(experiment_id)

    def archive_experiments(self, experiment_ids: list[str]) -> dict[str, Any]:
        if not isinstance(experiment_ids, list):
            raise ValueError('experiment_ids must be an array')
        normalized_ids = []
        for value in experiment_ids:
            if not isinstance(value, str) or not value.strip():
                raise ValueError('every experiment id must be a non-empty string')
            experiment_id = value.strip().lower()
            if not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
                raise ValueError(f'invalid experiment id: {value}')
            if experiment_id not in normalized_ids:
                normalized_ids.append(experiment_id)
        if not normalized_ids:
            raise ValueError('select at least one experiment')
        if len(normalized_ids) > 200:
            raise ValueError('archive at most 200 experiments at once')
        with self._launch_lock:
            experiments = [self.get_experiment(experiment_id) for experiment_id in normalized_ids]
            targets = [experiment for experiment in experiments if not experiment.get('archived')]
            for experiment in targets:
                experiment_id = str(experiment['experiment_id'])
                for run in self._experiment_run_records(experiment_id):
                    current = self.get_run(str(run['run_id']))
                    if current['status'] in ACTIVE_RUN_STATUSES or current['screen_active']:
                        raise PermissionError('experiments with active runs cannot be archived: ' + experiment_id)
            if targets:
                self.store.set_experiments_archived([str(experiment['experiment_id']) for experiment in targets], archived=True)
            updated = [self.get_experiment(experiment_id) for experiment_id in normalized_ids]
        return {'archived_count': len(targets), 'experiments': updated}

    def delete_experiment(self, experiment_id: str) -> dict[str, Any]:
        with self._launch_lock:
            all_experiments = self.store.list_experiments(archived=None)
            dependency_index = _experiment_dependency_index(all_experiments)
            invalid_dependency_index = _invalid_experiment_dependency_index(all_experiments)
            delete_experiment_ids = [experiment_id, *dependency_index.get(experiment_id, [])]
            invalid_dependents = sorted({dependent_id for target_id in delete_experiment_ids for dependent_id in invalid_dependency_index.get(target_id, [])})
            if invalid_dependents:
                raise PermissionError('related experiment links are ambiguous or nested: ' + ', '.join(invalid_dependents))
            definitions: list[tuple[Path, Path]] = []
            runs: list[tuple[dict[str, Any], tuple[Path, ...]]] = []
            for target_id in delete_experiment_ids:
                experiment = self.get_experiment(target_id)
                if not experiment.get('archived'):
                    raise PermissionError('related experiments must be archived before deletion: ' + target_id)
                definitions.append(self._validated_experiment_definition(experiment))
                for registered in self._experiment_run_records(target_id):
                    run = self.get_run(str(registered['run_id']))
                    if run['status'] in ACTIVE_RUN_STATUSES or run['screen_active']:
                        raise PermissionError('related experiments with active runs cannot be deleted: ' + target_id)
                    runs.append((run, self._validated_run_artifacts(run)))
            paths_to_stage = [path for _, artifacts in runs for path in artifacts]
            paths_to_stage.extend((config_path for config_path, _ in definitions))

            def commit_experiment_delete() -> None:
                deleted_count = self.store.delete_experiments(delete_experiment_ids)
                if deleted_count != len(delete_experiment_ids):
                    raise RuntimeError('not every related experiment was deleted')
            cleanup_pending = self._commit_with_quarantined_paths(paths_to_stage, operation='experiment deletion', stage_prefix=experiment_id, commit=commit_experiment_delete)
            for _, config_dir in definitions:
                if not config_dir.is_dir():
                    continue
                try:
                    config_dir.rmdir()
                except OSError:
                    pass
        result = {'deleted': True, 'experiment_id': experiment_id, 'deleted_experiment_ids': delete_experiment_ids, 'deleted_experiment_count': len(delete_experiment_ids), 'deleted_runs': len(runs)}
        if cleanup_pending is not None:
            result['cleanup_pending'] = cleanup_pending
        return result

    def validate_config(self, config: dict[str, Any], *, experiment_id: str | None=None, group_id: str | None=None) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise ValueError('experiment config must be an object')
        previous = self.store.get_experiment(experiment_id) if experiment_id else None
        if experiment_id and previous is None:
            raise ValueError('the experiment being validated does not exist')
        resolved_group_id = str(previous.get('group_id')) if previous is not None else group_id.strip().lower() if isinstance(group_id, str) and group_id.strip() else None
        if previous is not None and group_id is not None and (group_id.strip().lower() != resolved_group_id):
            raise ValueError('the selected experiment belongs to another group')
        group = self.store.get_experiment_group(resolved_group_id) if resolved_group_id is not None else None
        if resolved_group_id is not None and group is None:
            raise ValueError('the selected experiment group does not exist')
        if group is not None and group.get('archived'):
            raise PermissionError('archived experiment groups cannot validate edits')
        if group is not None:
            group = self._group_with_registered_resources(group)
        normalized = copy.deepcopy(config)
        if _runner_type(normalized) == 'tool_selection':
            if group is not None and group.get('resources_ready'):
                self._inherit_group_validation_resources(normalized, group)
            normalized = self._prepare_tool_selection_config(normalized, previous=previous['config'] if previous else None, group_id=resolved_group_id)
            self._apply_default_model_connection(normalized, previous=previous)
        if group is not None and group.get('resource_policy') == 'registry':
            normalized, _ = self._materialize_registered_resources(normalized, allow_archived=False)
        return self._validate_config(normalized).public_dict()

    def memory_sources(self, *, experiment_id: str | None=None, group_id: str | None=None, limit: int=100, offset: int=0) -> dict[str, Any]:
        if limit < 1 or limit > 100 or offset < 0:
            raise ValueError('invalid memory source pagination')
        if experiment_id is None and (not (isinstance(group_id, str) and group_id.strip())):
            raise ValueError('group_id is required when selecting memory sources')
        if experiment_id is not None:
            experiment_id = experiment_id.strip().lower()
            experiment = self.store.get_experiment(experiment_id)
            if experiment is None:
                raise ValueError('the selected first-stage experiment does not exist')
            if experiment.get('archived'):
                raise ValueError('the selected first-stage experiment is archived; restore it first')
            if experiment.get('runner_type') == 'tool_selection':
                raise ValueError('memory sources must belong to a first-stage experiment')
            if group_id is not None and experiment.get('group_id') != group_id.strip().lower():
                raise ValueError('the selected first-stage experiment belongs to another group')
            group_id = str(experiment.get('group_id') or '')
        if group_id is not None:
            group_id = group_id.strip().lower()
            group = self.store.get_experiment_group(group_id)
            if group is None:
                raise ValueError('the selected experiment group does not exist')
            if group.get('archived'):
                raise ValueError('restore the experiment group before selecting memory')
        runs = self.store.list_runs(experiment_id=experiment_id, group_id=group_id, archived=False, limit=limit + 1, offset=offset)
        sources = []
        for run in runs[:limit]:
            source = describe_memory_source(run, include_invalid=True)
            if source is not None:
                sources.append(source)
        return {'sources': sources, 'limit': limit, 'offset': offset, 'has_more': len(runs) > limit}

    def preview_memory(self, *, source_experiment_id: str, source_run_id: str, source_agent_id: str, source_round: int | None, query: dict[str, Any], memory: dict[str, Any] | None=None, memory_account_pool: dict[str, Any] | None=None, experiment_id: str | None=None, group_id: str | None=None) -> dict[str, Any]:
        """Render the exact memory text one stage-two query would receive."""
        if not isinstance(query, dict):
            raise ValueError('query must be an object')
        if not isinstance(source_experiment_id, str) or not source_experiment_id.strip():
            raise ValueError('source_experiment_id must be a non-empty string')
        if not isinstance(source_run_id, str) or not source_run_id.strip():
            raise ValueError('source_run_id must be a non-empty string')
        if not isinstance(source_agent_id, str) or not source_agent_id.strip():
            raise ValueError('source_agent_id must be a non-empty string')
        if source_round is not None and (not isinstance(source_round, int) or isinstance(source_round, bool)):
            raise ValueError('source_round must be an integer')
        if experiment_id is not None and (not isinstance(experiment_id, str) or not experiment_id.strip()):
            raise ValueError('experiment_id must be a non-empty string')
        previous = self.store.get_experiment(experiment_id) if experiment_id else None
        if experiment_id and previous is None:
            raise ValueError('the experiment being previewed does not exist')
        if previous is not None and previous.get('runner_type') != 'tool_selection':
            raise ValueError('memory preview is only available for second-stage experiments')
        resolved_group_id = str(previous.get('group_id')) if previous is not None else group_id.strip().lower() if isinstance(group_id, str) and group_id.strip() else None
        if previous is not None and group_id is not None and (not isinstance(group_id, str) or group_id.strip().lower() != resolved_group_id):
            raise ValueError('the selected experiment belongs to another group')
        if resolved_group_id is None:
            raise ValueError('group_id is required for a new memory preview')
        group = self.store.get_experiment_group(resolved_group_id)
        if group is None:
            raise ValueError('the selected experiment group does not exist')
        if group.get('archived'):
            raise PermissionError('restore the experiment group before previewing memory')
        config = {'environment': {'type': 'tool_selection'}, 'validation': {'source_experiment_id': source_experiment_id.strip().lower(), 'source_run_id': source_run_id.strip(), 'source_agent_id': source_agent_id.strip(), 'source_round': source_round}}
        if memory is not None:
            config['memory'] = copy.deepcopy(memory)
        if memory_account_pool is not None:
            config['validation']['memory_account_pool'] = copy.deepcopy(memory_account_pool)
        prepared = self._prepare_tool_selection_config(config, previous=previous['config'] if previous else None, group_id=resolved_group_id, require_model_selection=False)
        source = prepared['validation']['memory_source']
        memory_runtime = None
        pool = prepared['validation'].get('memory_account_pool')
        memory_reader = None
        if source['memory_strategy'] in {'hindsight_cloud', 'mem0_platform_v3', 'memos_cloud'}:
            from .cloud_config import build_memory_read_router
            memory_reader = build_memory_read_router(source, pool, registry_db_path=self.store.db_path)
        elif source['memory_strategy'] in CLOUD_STRATEGIES:
            memory_runtime = cloud_runtime(source['memory_strategy'], memory_scope_run_id(source), read_only=True, project_root=self.project_root)
        elif source['memory_strategy'] == 'langmem_official':
            from agent_posting_core.langmem_official import uses_local_embeddings
            local_embeddings = uses_local_embeddings(source['snapshot']['options']['embedding_model'])
            backend = prepared.get('backend') or {}
            if not isinstance(backend, dict):
                raise ValueError('backend must be an object')
            env_file = read_env_file(self.project_root / '.env')
            api_key_env = backend.get('api_key_env', 'AGENT_COMMUNITY_API_KEY')
            api_base_env = backend.get('api_base_env', 'AGENT_COMMUNITY_API_BASE')
            model_env = backend.get('model_env', 'AGENT_COMMUNITY_MODEL')
            for name, value in (('backend.api_key_env', api_key_env), ('backend.api_base_env', api_base_env), ('backend.model_env', model_env)):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f'{name} must be a non-empty string')
            api_key = os.environ.get(api_key_env) or env_file.get(api_key_env) or ''
            api_base = backend.get('api_base') or os.environ.get(api_base_env) or env_file.get(api_base_env) or ''
            model = backend.get('model') or os.environ.get(model_env) or env_file.get(model_env) or ''
            if not local_embeddings and (not api_key):
                raise ValueError(f'LangMem embedding API key is empty; set {api_key_env}')
            if not local_embeddings and (not isinstance(api_base, str) or not api_base.strip()):
                raise ValueError(f'LangMem API base is empty; set {api_base_env}')
            if not local_embeddings and (not isinstance(model, str) or not model.strip()):
                raise ValueError(f'LangMem model is empty; set {model_env}')
            memory_runtime = {'api_base': api_base, 'api_key': api_key, 'model': model, 'run_id': memory_scope_run_id(source), 'request_timeout_seconds': backend.get('timeout_seconds', DEFAULT_LLM_TIMEOUT_SECONDS), 'max_retries': backend.get('max_transient_retries', 8), 'memory_max_tokens': backend.get('max_tokens', 8000)}
            if local_embeddings:
                for key in ('api_base', 'api_key', 'model'):
                    memory_runtime.pop(key, None)
        preview = asyncio.run(render_frozen_memory_for_query(agent_id=source['agent_id'], memory_snapshot=source['snapshot'], memory_options=prepared['memory']['options'], query=query, memory_runtime=memory_runtime, memory_reader=memory_reader))
        return {**preview, 'source_experiment_id': source['experiment_id'], 'source_run_id': source['run_id'], 'source_round': source['round_index'], 'source_agent_id': source['agent_id'], 'snapshot_sha256': source['snapshot_sha256']}

    def suggest_experiment_id(self, *, stage: str='interaction', source_experiment_id: str | None=None, group_id: str | None=None, memory_source_mode: str='run') -> str:
        """Return the next date-based stage-one or parent-based stage-two ID."""
        normalized_group_id = group_id.strip().lower() if isinstance(group_id, str) and group_id.strip() else None
        if normalized_group_id is None:
            raise ValueError('group_id is required when creating an experiment')
        group = self.store.get_experiment_group(normalized_group_id) if normalized_group_id is not None else None
        if normalized_group_id is not None and group is None:
            raise ValueError('the selected experiment group does not exist')
        if group is not None and group.get('archived'):
            raise ValueError('restore the experiment group before creating experiments')
        if normalized_group_id == LEGACY_EXPERIMENT_GROUP_ID:
            raise ValueError('new experiments cannot be added to the legacy experiment group')
        existing_ids = {experiment['experiment_id'] for experiment in self.store.list_experiments(archived=None)}
        existing_ids.update((path.name for path in self.exp_dir.iterdir() if path.is_dir()))
        if stage in {'interaction', 'moltnet_thread', 'moltnet_forum'}:
            prefix = datetime.now().strftime('%m%d')
        elif stage == 'tool_selection' and memory_source_mode == 'specified':
            prefix = datetime.now().strftime('%m%d') + '-specified-'
        elif stage == 'tool_selection':
            if memory_source_mode != 'run':
                raise ValueError('memory_source_mode must be run or specified')
            if not isinstance(source_experiment_id, str) or not source_experiment_id.strip():
                raise ValueError('select a first-stage experiment before generating a second-stage ID')
            source_experiment_id = source_experiment_id.strip().lower()
            source_experiment = self.store.get_experiment(source_experiment_id)
            if source_experiment is None:
                raise ValueError('the selected first-stage experiment does not exist')
            if source_experiment.get('runner_type') == 'tool_selection':
                raise ValueError('a second-stage experiment must reference a first-stage experiment')
            if source_experiment.get('archived'):
                raise ValueError('restore the first-stage experiment before creating a second-stage experiment')
            if source_experiment.get('group_id') != normalized_group_id:
                raise ValueError('the selected first-stage experiment belongs to another group')
            prefix = f'{source_experiment_id}-'
        else:
            raise ValueError(f'unsupported experiment stage: {stage}')
        if len(prefix) + 2 > 64:
            raise ValueError('the first-stage experiment ID is too long for a second-stage suffix')
        sequence_pattern = re.compile(f'^{re.escape(prefix)}([0-9]{{2}})$')
        used = [int(match.group(1)) for experiment_id in existing_ids if (match := sequence_pattern.fullmatch(experiment_id)) and int(match.group(1)) > 0]
        sequence = max(used, default=0) + 1
        if sequence > 99:
            raise ValueError(f'no experiment IDs remain for prefix {prefix}')
        return f'{prefix}{sequence:02d}'

    def _prepare_tool_selection_config(self, config: dict[str, Any], *, previous: dict[str, Any] | None=None, group_id: str | None=None, require_model_selection: bool=True) -> dict[str, Any]:
        normalized = copy.deepcopy(config)
        requested_backend = normalized.get('backend')
        selected_model = requested_backend.get('model') if isinstance(requested_backend, dict) else None
        if require_model_selection:
            if not isinstance(selected_model, str) or not selected_model.strip():
                raise ValueError('second-stage experiments require an explicit backend.model')
            selected_model = selected_model.strip()
            if selected_model not in SELECTABLE_EXPERIMENT_MODELS:
                raise ValueError('backend.model must be one of: ' + ', '.join(SELECTABLE_EXPERIMENT_MODELS))
        elif isinstance(selected_model, str):
            selected_model = selected_model.strip() or None
        validation = normalized.get('validation')
        if not isinstance(validation, dict):
            raise ValueError('second-stage validation settings must be an object')
        source_mode = validation.get('memory_source_mode', 'run')
        if not isinstance(source_mode, str) or source_mode not in {'run', 'specified'}:
            raise ValueError('validation.memory_source_mode must be run or specified')
        if source_mode == 'specified':
            source = build_specified_memory_source(validation.get('specified_memory'), validation.get('specified_agent_id', 'ROWAN'))
            if (validation.get('memory_account_pool') or {}).get('enabled'):
                raise ValueError('specified memory cannot use cloud memory account pools')
            validation.update(memory_source=source, source_run_id=None, source_experiment_id=None, source_agent_id=source['agent_id'], source_round=1, specified_agent_id=source['agent_id'])
            validation.setdefault('injection_enabled', False)
            validation.pop('memory_account_pool', None)
            normalized['memory'] = {'strategy': 'specified_memory', 'options': {}}
            backend = copy.deepcopy((previous or {}).get('backend') or self.default_config().get('backend', {}))
            if isinstance(requested_backend, dict):
                backend.update(requested_backend)
            if selected_model is not None:
                backend['model'] = selected_model
                backend.pop('model_env', None)
            normalized['backend'] = backend
            normalized.setdefault('agents', {'count': 1})
            return normalized
        source_id = validation.get('source_run_id')
        agent_id = validation.get('source_agent_id')
        if not isinstance(source_id, str) or not isinstance(agent_id, str) or (not agent_id.strip()):
            raise ValueError('select a first-stage run and exactly one agent')
        requested_experiment_id = validation.get('source_experiment_id')
        if requested_experiment_id is not None and (not isinstance(requested_experiment_id, str) or not requested_experiment_id.strip()):
            raise ValueError('select a first-stage experiment')
        previous_validation = (previous or {}).get('validation') or {}
        same_source = previous_validation.get('memory_source') and all((validation.get(key) == previous_validation.get(key) for key in ('source_run_id', 'source_agent_id', 'source_round')))
        if same_source:
            source = copy.deepcopy(previous_validation['memory_source'])
            backend = copy.deepcopy(previous['backend'])
        else:
            with self._launch_lock:
                run = self.store.get_run(source_id)
                if run is None:
                    raise ValueError('the selected first-stage run does not exist')
                if run.get('runner_type') == 'tool_selection':
                    raise ValueError('the selected source run must belong to a first-stage experiment')
                source = read_memory_source(run, agent_id=agent_id, round_index=validation.get('source_round'))
                backend = read_source_config(run)['backend']
        source_experiment_id = str(source.get('experiment_id') or '').strip().lower()
        if not source_experiment_id:
            raise ValueError('the selected source has no first-stage experiment identity')
        if isinstance(requested_experiment_id, str) and requested_experiment_id.strip().lower() != source_experiment_id:
            raise ValueError('the selected run does not belong to the selected first-stage experiment')
        source_experiment = self.store.get_experiment(source_experiment_id)
        if source_experiment is None or source_experiment.get('runner_type') == 'tool_selection':
            raise ValueError('the selected source must belong to an existing first-stage experiment')
        if group_id is not None and source_experiment.get('group_id') != group_id:
            raise ValueError('the selected first-stage source must belong to the same experiment group')
        if source_experiment.get('archived'):
            raise ValueError('restore the first-stage experiment before creating a second-stage experiment')
        validation['source_experiment_id'] = source_experiment_id
        validation['memory_source'] = source
        validation['source_round'] = source['round_index']
        requested_pool = validation.get('memory_account_pool')
        if same_source:
            previous_pool = previous_validation.get('memory_account_pool')
            if requested_pool is None:
                requested_pool = copy.deepcopy(previous_pool)
            elif isinstance(requested_pool, dict) and isinstance(previous_pool, dict) and (not requested_pool.get('memory_set_id')) and (not requested_pool.get('revision_hash')) and previous_pool.get('memory_set_id') and previous_pool.get('revision_hash'):
                requested_pool = {**requested_pool, 'memory_set_id': previous_pool['memory_set_id'], 'revision_hash': previous_pool['revision_hash']}
        pool = self.prepare_memory_account_pool(source, requested_pool)
        if pool is not None:
            validation['memory_account_pool'] = pool
        normalized['backend'] = copy.deepcopy(backend)
        if isinstance(requested_backend, dict):
            normalized['backend'].update(copy.deepcopy(requested_backend))
        if selected_model is not None:
            normalized['backend']['model'] = selected_model
            normalized['backend'].pop('model_env', None)
        normalized.setdefault('agents', {'count': 1})
        requested_memory = normalized.get('memory')
        if requested_memory is None and same_source:
            requested_memory = copy.deepcopy((previous or {}).get('memory'))
        normalized['memory'] = resolve_tool_selection_memory_settings(strategy=source['memory_strategy'], source_options=source['memory_options'], requested=requested_memory)
        return normalized

    def default_config(self) -> dict[str, Any]:
        if self.default_config_path.is_file():
            value = _read_yaml(self.default_config_path)
            settings = self.platform_settings()
            value.setdefault('backend', {}).update(settings['backend'])
            environment = value.setdefault('environment', {})
            environment['hide_speaker'] = settings['reading']['hide_speaker']
            environment['speaker_as_json'] = not environment['hide_speaker'] and settings['reading']['speaker_as_json']
            value.setdefault('agents', {})['require_identity'] = settings['reading']['require_identity']
            strategy = value.get('memory', {}).get('strategy')
            if strategy in settings['memory_profiles']:
                value['memory'] = {'strategy': strategy, **copy.deepcopy(settings['memory_profiles'][strategy])}
            value.setdefault('output', {}).update({key: settings['output'][key] for key in ('save_thread_after_each_round', 'save_memory_after_each_round')})
            value['run'] = dict(value.get('run') or {})
            value['run']['id'] = None
            return value
        raise FileNotFoundError(f'No experiment template is available: {self.default_config_path}')

    def platform_settings(self) -> dict[str, Any]:
        return read_platform_settings(self.platform_settings_path)

    def _apply_default_model_connection(self, config: dict[str, Any], *, previous: dict[str, Any] | None) -> None:
        """Freeze a model-specific default into a new stage-two configuration."""
        if previous is not None:
            return
        model = config.get('backend', {}).get('model')
        settings = self.platform_settings()
        if model not in settings['validation']['model_connections']:
            return
        validation = config['validation']
        general = settings['validation']['backend_connection']
        if validation.get('backend_connection', general) == general:
            validation['backend_connection'] = validation_connection_for_model(settings, model)

    def save_platform_settings(self, value: Any) -> dict[str, Any]:
        with self._launch_lock:
            return write_platform_settings(self.platform_settings_path, value)

    def _create_quick_experiment(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a regular frozen experiment from the minimal quick form."""
        allowed = {'stage', 'group_id', 'name', 'posts_revision_id', 'post_ids', 'reader_agent_id', 'memory_strategy', 'memory_model', 'query_revision_id', 'mcp_revision_id', 'target_mapping_id', 'source_experiment_id', 'source_run_id', 'source_agent_id', 'model', 'mcp_parameter_additions'}
        if set(payload) - allowed:
            raise ValueError('unsupported quick configuration fields')
        stage = payload.get('stage')
        group_id = payload.get('group_id')
        if stage not in {'interaction', 'tool_selection'}:
            raise ValueError('stage must be interaction or tool_selection')
        if stage != 'tool_selection' and 'mcp_parameter_additions' in payload:
            raise ValueError('MCP parameter additions require the tool-selection stage')
        if not isinstance(group_id, str) or not group_id.strip():
            raise ValueError('choose an experiment group')
        group_id = group_id.strip().lower()
        group = self.get_experiment_group(group_id)
        if group.get('archived') or group.get('legacy') or (not group.get('registered_resources_ready')):
            raise ValueError('choose an active group using registered resources')
        settings = self.platform_settings()
        config = self.default_config()
        experiment_id = self.suggest_experiment_id(stage=stage, group_id=group_id, source_experiment_id=payload.get('source_experiment_id') if stage == 'tool_selection' else None)
        if stage == 'interaction':
            revision_id = payload.get('posts_revision_id')
            revision = self._resource_revision(revision_id, expected_kind='posts', allow_archived=False)
            dataset = revision['content']['dataset']
            available_posts = {post['id']: post for post in dataset['posts']}
            post_ids = payload.get('post_ids')
            if not isinstance(post_ids, list) or not post_ids or any((not isinstance(item, str) for item in post_ids)):
                raise ValueError('select one or more registered posts')
            if len(post_ids) != len(set(post_ids)) or any((item not in available_posts for item in post_ids)):
                raise ValueError('selected posts must be distinct IDs in the registered revision')
            environment_type = revision.get('environment_type')
            if environment_type not in {'moltnet_forum', 'moltnet_thread'}:
                raise ValueError('quick reading requires a forum or thread posts resource')
            if environment_type == 'moltnet_thread' and len(post_ids) != 1:
                raise ValueError('a thread resource must select exactly one post')
            reader_id = payload.get('reader_agent_id')
            if not isinstance(reader_id, str) or reader_id not in {agent['id'] for agent in dataset['agents']}:
                raise ValueError('choose one agent from the registered posts resource')
            strategy = payload.get('memory_strategy')
            profile = {'model': SELECTABLE_EXPERIMENT_MODELS[0], 'options': {}} if strategy == 'agent_memory_preview' else settings['memory_profiles'].get(strategy) if isinstance(strategy, str) else None
            if profile is None:
                raise ValueError('choose a supported memory strategy')
            memory = {'strategy': strategy, **copy.deepcopy(profile)}
            if 'model' in profile:
                model = payload.get('memory_model')
                if strategy in FIXED_LOCAL_MEMORY_STRATEGIES:
                    if model not in (None, FIXED_LOCAL_MEMORY_MODEL):
                        raise ValueError(f'first-stage local memory model is fixed to {FIXED_LOCAL_MEMORY_MODEL}')
                    model = FIXED_LOCAL_MEMORY_MODEL
                if model is not None:
                    if not isinstance(model, str) or model not in SELECTABLE_EXPERIMENT_MODELS:
                        raise ValueError('choose a supported memory management model')
                    memory['model'] = model
            elif payload.get('memory_model') is not None:
                raise ValueError('this memory strategy does not use a management model')
            titles = [str(available_posts[item].get('title') or item) for item in post_ids]
            strategy_name = {'langmem_official': 'LangMem', 'a_mem_official': 'A-Mem', 'mem0_platform_v3': 'Mem0', 'hindsight_cloud': 'Hindsight', 'memos_cloud': 'MemOS', 'agent_memory_preview': 'Active memory fact extraction (Hermes preview)'}[strategy]
            suggested_name = '、'.join(titles) + ' + ' + strategy_name
            if 'model' in memory:
                suggested_name += ' + ' + memory['model']
            config['environment'] = {'type': environment_type, 'thread' if environment_type == 'moltnet_thread' else 'forum': {'selection': 'file'} if environment_type == 'moltnet_thread' else {'selection': 'file', 'feed_limit': 50}, 'file': {'resource_revision_id': revision_id, 'selected_post_ids': post_ids}, 'hide_speaker': settings['reading']['hide_speaker'], 'speaker_as_json': not settings['reading']['hide_speaker'] and settings['reading']['speaker_as_json']}
            config.pop('data', None)
            config['agents'] = {'count': 1, 'require_identity': settings['reading']['require_identity'], 'history_limit': 100}
            config['memory'] = memory
            config['simulation'] = {'mode': 'read_only', 'read_only_agent_id': reader_id, 'rounds': 1}
        else:
            source_experiment_id = payload.get('source_experiment_id')
            if not isinstance(source_experiment_id, str) or not source_experiment_id.strip():
                raise ValueError('choose a first-stage experiment')
            source_experiment = self.get_experiment(source_experiment_id.strip().lower())
            if source_experiment.get('group_id') != group_id or source_experiment.get('runner_type') == 'tool_selection':
                raise ValueError('choose a first-stage experiment in the same group')
            if source_experiment.get('archived'):
                raise ValueError('restore the first-stage experiment before using it')
            query_revision_id = payload.get('query_revision_id')
            query_revision = self._resource_revision(query_revision_id, expected_kind='queries', allow_archived=False)
            mcp_revision = self._resource_revision(payload.get('mcp_revision_id') or group.get('mcp_revision_id'), expected_kind='mcp', allow_archived=False)
            mapping_id = payload.get('target_mapping_id')
            if not isinstance(mapping_id, str) or self.store.get_target_mapping(mapping_id) is None:
                raise ValueError('choose a registered target mapping')
            source_run_id = payload.get('source_run_id')
            if source_run_id is not None and (not isinstance(source_run_id, str)):
                raise ValueError('source_run_id must be a string')
            sources = []
            offset = 0
            while True:
                page = self.memory_sources(experiment_id=source_experiment_id, group_id=group_id, limit=100, offset=offset)
                sources.extend((source for source in page['sources'] if source.get('usable')))
                if not page['has_more']:
                    break
                offset += 100
                if offset >= 1000:
                    raise ValueError('too many source runs; select from the full experiment editor')
            if source_run_id:
                sources = [source for source in sources if source['run_id'] == source_run_id]
            if len(sources) != 1:
                raise ValueError('choose one completed first-stage run with usable memory')
            source = sources[0]
            model = payload.get('model')
            if (model is None or model == '') and source['memory_strategy'] not in {*CLOUD_STRATEGIES, 'mem0_platform_v3'}:
                model = source.get('memory_model') or source.get('backend_model')
            if not isinstance(model, str) or model not in SELECTABLE_EXPERIMENT_MODELS:
                raise ValueError('choose a supported second-stage model')
            agents = source['agents']
            source_agent_id = payload.get('source_agent_id')
            if source_agent_id:
                agents = [agent for agent in agents if agent['agent_id'] == source_agent_id]
            if len(agents) != 1:
                raise ValueError('choose one source agent for the memory snapshot')
            strategy = source['memory_strategy']
            suggested_name = source_experiment['name'] + ' + ' + str(query_revision.get('resource_name') or query_revision['filename']) + ' + ' + model
            config = {'version': 1, 'run': {'name': suggested_name, 'id': None}, 'environment': {'type': 'tool_selection'}, 'validation': {'source_experiment_id': source_experiment_id, 'source_run_id': source['run_id'], 'source_agent_id': agents[0]['agent_id'], 'source_round': source['source_round'], 'queries': {'resource_revision_id': query_revision_id}, 'mcp': {'resource_revision_id': mcp_revision['revision_id']}, 'target_mode': 'mapping', 'target_mapping_id': mapping_id, 'backend_connection': copy.deepcopy(settings['validation']['backend_connection']), 'api_key_position': settings['validation']['api_key_position']}, 'backend': {'model': model}, 'memory': {'strategy': strategy, 'options': copy.deepcopy(settings['validation']['retrieval_profiles'][strategy])}}
            if 'mcp_parameter_additions' in payload:
                config['validation']['mcp_parameter_additions'] = copy.deepcopy(payload['mcp_parameter_additions'])
        name = payload.get('name')
        if name is None or name == '':
            name = suggested_name
        if not isinstance(name, str) or not name.strip() or len(name) > 500:
            raise ValueError('experiment name must contain 1 to 500 characters')
        config['run'] = {'name': name.strip(), 'id': None}
        config['output'] = {'root_dir': output_root(settings, experiment_id)}
        if stage == 'interaction':
            config['output'].update({key: settings['output'][key] for key in ('save_thread_after_each_round', 'save_memory_after_each_round')})
        self.assert_registered_resource_config(config, group_id=group_id)
        return self.save_experiment(experiment_id=experiment_id, group_id=group_id, name=name, description='', config=config, create_only=True)

    def create_quick_experiments(self, group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one configuration per selected model, then optionally start them."""
        if not isinstance(payload, dict):
            raise ValueError('quick configuration must be an object')
        if not isinstance(group_id, str) or not group_id.strip():
            raise ValueError('choose an experiment group')
        group_id = group_id.strip().lower()
        if payload.get('group_id') not in (None, group_id):
            raise ValueError('quick configuration belongs to another experiment group')
        launch = payload.get('launch', False)
        if not isinstance(launch, bool):
            raise ValueError('launch must be true or false')
        if set(payload) - {'stage', 'group_id', 'name', 'posts_revision_id', 'post_ids', 'reader_agent_id', 'memory_strategy', 'memory_models', 'query_revision_id', 'mcp_revision_id', 'target_mapping_id', 'source_experiment_id', 'source_run_id', 'source_agent_id', 'models', 'model', 'launch', 'mcp_parameter_additions'}:
            raise ValueError('unsupported quick configuration fields')
        stage = payload.get('stage')
        base = {key: value for key, value in payload.items() if key not in {'memory_models', 'models', 'launch'}}
        base['group_id'] = group_id
        if stage == 'interaction':
            strategy = payload.get('memory_strategy')
            profile = {'model': SELECTABLE_EXPERIMENT_MODELS[0], 'options': {}} if strategy == 'agent_memory_preview' else self.platform_settings()['memory_profiles'].get(strategy) if isinstance(strategy, str) else None
            if profile is None:
                raise ValueError('choose a supported memory strategy')
            selected = payload.get('memory_models')
            if strategy in FIXED_LOCAL_MEMORY_STRATEGIES:
                if selected is not None and selected != [FIXED_LOCAL_MEMORY_MODEL]:
                    raise ValueError(f'first-stage local memory model is fixed to {FIXED_LOCAL_MEMORY_MODEL}; select only this model')
                variants = [('memory_model', FIXED_LOCAL_MEMORY_MODEL)]
            elif 'model' in profile:
                if not isinstance(selected, list) or not 1 <= len(selected) <= 50:
                    raise ValueError('select 1 to 50 memory management models')
                if len(set(map(str, selected))) != len(selected) or any((not isinstance(model, str) or model not in SELECTABLE_EXPERIMENT_MODELS for model in selected)):
                    raise ValueError('memory management models must be distinct supported models')
                variants = [('memory_model', model) for model in selected]
            else:
                if selected is not None:
                    raise ValueError('this memory strategy does not use a management model')
                variants = [(None, None)]
        elif stage == 'tool_selection':
            source_id = payload.get('source_experiment_id')
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValueError('choose a first-stage experiment')
            source_experiment = self.get_experiment(source_id.strip().lower())
            if source_experiment.get('group_id') != group_id or source_experiment.get('runner_type') == 'tool_selection':
                raise ValueError('choose a first-stage experiment in the same group')
            strategy = (source_experiment.get('config') or {}).get('memory', {}).get('strategy')
            selected = payload.get('models')
            hosted = strategy in {*CLOUD_STRATEGIES, 'mem0_platform_v3'}
            if hosted:
                if not isinstance(selected, list) or not 1 <= len(selected) <= 50:
                    raise ValueError('select 1 to 50 second-stage models')
                if len(set(map(str, selected))) != len(selected) or any((not isinstance(model, str) or model not in SELECTABLE_EXPERIMENT_MODELS for model in selected)):
                    raise ValueError('second-stage models must be distinct supported models')
                variants = [('model', model) for model in selected]
            else:
                if selected is not None:
                    raise ValueError('multiple decision models require hosted memory')
                model = payload.get('model')
                if model not in (None, '') and model not in SELECTABLE_EXPERIMENT_MODELS:
                    raise ValueError('choose a supported second-stage model')
                variants = [('model', model)]
        else:
            raise ValueError('stage must be interaction or tool_selection')
        custom_name = payload.get('name') or ''
        if not isinstance(custom_name, str):
            raise ValueError('experiment name must be a string')
        created = []
        failures = []
        for field, model in variants:
            variant = dict(base)
            if field:
                variant[field] = model
            if len(variants) > 1 and custom_name.strip():
                variant['name'] = f'{custom_name.strip()} + {model}'
            try:
                experiment = self._create_quick_experiment(variant)
                created.append({'experiment_id': experiment['experiment_id'], 'name': experiment['name'], 'model': model})
            except Exception as exc:
                failures.append({'model': model, 'error': str(exc)})
                if not created:
                    break
        started = []
        if launch and created:
            runs = self.launch_group_runs(group_id, [item['experiment_id'] for item in created])
            started = runs['started']
            failures.extend(({'experiment_id': item['experiment_id'], 'error': item['error']} for item in runs['failures']))
        return {'group_id': group_id, 'created': created, 'started': started, 'failures': failures}

    def launch_group_runs(self, group_id: str, experiment_ids: Any) -> dict[str, Any]:
        if not isinstance(experiment_ids, list) or not 1 <= len(experiment_ids) <= 50:
            raise ValueError('select 1 to 50 experiments')
        if any((not isinstance(value, str) or not value.strip() for value in experiment_ids)):
            raise ValueError('experiment IDs must be non-empty strings')
        ids = list(dict.fromkeys(experiment_ids))
        with self._launch_lock:
            group = self.get_experiment_group(group_id)
            if group.get('archived') or group.get('legacy'):
                raise PermissionError('this experiment group cannot launch runs')
            for experiment_id in ids:
                experiment = self.get_experiment(experiment_id)
                if experiment.get('group_id') != group_id or experiment.get('archived'):
                    raise ValueError(f'experiment is not active in this group: {experiment_id}')
            started = []
            failures = []
            for experiment_id in ids:
                try:
                    run = self.launch_run(experiment_id)
                    started.append({'experiment_id': experiment_id, 'run_id': run['run_id']})
                except Exception as exc:
                    failures.append({'experiment_id': experiment_id, 'error': str(exc)})
            return {'group_id': group_id, 'started': started, 'failures': failures}

    def launch_run(self, experiment_id: str) -> dict[str, Any]:
        with self._launch_lock:
            experiment = self.get_experiment(experiment_id)
            if experiment.get('group_archived'):
                raise PermissionError('archived experiment groups cannot launch runs')
            if experiment.get('archived'):
                raise PermissionError('archived experiments cannot launch runs')
            raw = copy.deepcopy(experiment['config'])
            run_id = _new_run_id(experiment_id)
            resource_bindings: dict[str, dict[str, Any]] = {}
            if experiment.get('resource_policy') == 'registry':
                raw, resource_bindings = self._materialize_registered_resources(raw, allow_archived=True)
                if experiment.get('runner_type') == 'tool_selection':
                    validation = raw.get('validation') or {}
                    source_run_id = validation.get('source_run_id')
                    if isinstance(source_run_id, str):
                        source_refs = self.store.get_run_resource_refs(source_run_id)
                        if source_refs.get('posts') is not None:
                            resource_bindings['posts'] = source_refs['posts']
            run = raw.setdefault('run', {})
            run['id'] = run_id
            worker_config = self.jobs_dir / f'{run_id}.yaml'
            _write_yaml(worker_config, raw)
            try:
                loaded = load_experiment_config(worker_config, project_root=self.project_root)
            except Exception:
                worker_config.unlink(missing_ok=True)
                raise
            output_dir = loaded.output.root_dir / run_id
            log_path = self.logs_dir / f'{run_id}.log'
            try:
                self.store.create_run(run_id=run_id, experiment_id=experiment_id, worker_config_path=worker_config, output_dir=output_dir, log_path=log_path, resource_refs=resource_bindings)
            except Exception:
                worker_config.unlink(missing_ok=True)
                raise
            try:
                session_name = self.launcher.launch(run_id=run_id, platform_db=self.store.db_path, worker_config=worker_config, log_path=log_path)
                self.store.update_run(run_id, screen_session=session_name)
            except Exception as exc:
                self.store.update_run(run_id, status='failed', completed_at=_now(), error=str(exc))
                raise
            return self.get_run(run_id)

    def interrupt_run(self, run_id: str) -> dict[str, Any]:
        """Stop exactly one active worker and preserve its partial artifacts."""
        with self._launch_lock:
            run = self.get_run(run_id)
            if run['status'] not in ACTIVE_RUN_STATUSES:
                raise ValueError('only an active run can be interrupted')
            session_name = run.get('screen_session')
            if not session_name or not run.get('screen_active'):
                raise ValueError('run worker is not active')
            if not self.launcher.stop(str(session_name)):
                raise RuntimeError('run worker did not stop within 5 seconds')
            completed_at = _now()
            output_dir = Path(run['output_dir'])
            manifest_path = output_dir / 'manifest.json'
            manifest = _read_json_if_exists(manifest_path)
            if manifest is not None:
                manifest.update(status='interrupted', completed_at=completed_at, interrupted_at=completed_at, interrupted_by='user', error=None)
                _write_json(manifest_path, manifest)
                self.store.save_metrics(run_id, _manifest_metrics(manifest), source='user_interrupt')
            _append_json_line(output_dir / 'events.jsonl', {'at': completed_at, 'event': 'run_interrupted', 'reason': 'user_requested'})
            self.store.update_run(run_id, status='interrupted', completed_at=completed_at, error=None)
        return self.get_run(run_id)

    def resume_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        manifest = _read_json_if_exists(Path(run['output_dir']) / 'manifest.json')
        if run.get('runner_type') == 'tool_selection':
            return self.resume_memory_account_run(run_id)
        return self.resume_cloud_run(run_id)

    def recover_interrupted_memory_quota_run(self, run_id: str, *, expected_manifest_sha256: str) -> dict[str, Any]:
        """Audit a stopped, retrieval-only billing-quota failure for same-run recovery."""
        from .memory_quota_maintenance import recover_interrupted_memory_quota_run
        return recover_interrupted_memory_quota_run(self, run_id, expected_manifest_sha256=expected_manifest_sha256)

    def resume_memory_account_run(self, run_id: str) -> dict[str, Any]:
        """Continue a paused account-pool run without repeating completed queries."""
        with self._launch_lock:
            run = self.store.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            manifest_path = Path(run['output_dir']) / 'manifest.json'
            manifest = _read_json_if_exists(manifest_path) or {}
            worker_path = Path(run['worker_config_path'])
            config = _read_yaml(worker_path)
            resolved = _read_json_if_exists(Path(run['output_dir']) / 'config.resolved.json') or {}
            pool = (resolved.get('validation') or {}).get('memory_account_pool') or manifest.get('memory_account_pool') or {}
            experiment = self.store.get_experiment(run['experiment_id'])
            group = self.store.get_experiment_group(experiment['group_id']) if experiment else None
            if not experiment or experiment.get('archived') or (not group) or group.get('archived'):
                raise PermissionError('restore the experiment and its group before resuming')
            if run.get('runner_type') != 'tool_selection' or run.get('status') != 'paused' or manifest.get('pause_reason') != 'memory_accounts_unavailable' or (not pool.get('enabled')) or (run.get('screen_session') and self.launcher.is_active(run['screen_session'])):
                raise ValueError('only an idle second-stage run paused for memory accounts can be resumed')
            source = config['validation']['memory_source']
            if not pool.get('memory_set_id') or not pool.get('revision_hash'):
                raise ValueError('memory account resume requires the original frozen memory version')
            prepared_pool = self.prepare_memory_account_pool(source, pool)
            config['validation']['memory_account_pool'] = prepared_pool
            _write_yaml(worker_path, config)
            manifest['memory_account_resume_requested_at'] = _now()
            _write_json(manifest_path, manifest)
            self.store.update_run(run_id, status='queued', completed_at=None, error=None)
            try:
                session = self.launcher.launch(run_id=run_id, platform_db=self.store.db_path, worker_config=Path(run['worker_config_path']), log_path=Path(run['log_path']))
                self.store.update_run(run_id, screen_session=session)
            except Exception:
                manifest.pop('memory_account_resume_requested_at', None)
                _write_json(manifest_path, manifest)
                self.store.update_run(run_id, status='paused')
                raise
        return self.get_run(run_id)

    def _read_only_resume_config(self, run: dict[str, Any], manifest: dict[str, Any]) -> ExperimentConfig:
        """Validate frozen inputs without changing run state or starting a worker."""
        experiment = self.store.get_experiment(run['experiment_id'])
        if experiment is None:
            raise KeyError(run['experiment_id'])
        if experiment.get('archived') or experiment.get('group_archived'):
            raise ValueError('restore the experiment and its group before resuming')
        config = load_experiment_config(Path(run['worker_config_path']), project_root=self.project_root)
        if config.environment.kind not in {'moltnet_thread', 'moltnet_forum'} or config.environment.selection != 'file' or config.simulation.mode != 'read_only' or (config.agents.count != 1) or (not config.simulation.read_only_agent_id):
            raise ValueError('resume requires a file-based, single-reader read_only configuration')
        if config.run.run_id != run['run_id'] or manifest.get('run_id') != run['run_id'] or manifest.get('environment_type') != config.environment.kind or (manifest.get('memory_strategy') != config.memory.strategy) or (manifest.get('simulation_mode') != 'read_only') or (manifest.get('selected_agent_ids') != [config.simulation.read_only_agent_id]) or ((config.output.root_dir / run['run_id']).resolve() != Path(run['output_dir']).resolve()):
            raise ValueError('resume configuration does not match the original run identity')
        current = _read_yaml(Path(run['worker_config_path']))
        previous = _read_yaml(Path(run['output_dir']) / 'config.yaml')
        unchanged = _resume_identity(current) == _resume_identity(previous)
        if not unchanged:
            raise ValueError('resume must keep the original experiment configuration')
        return config

    def _cloud_resume_config(self, run: dict[str, Any], manifest: dict[str, Any]) -> ExperimentConfig:
        if run['status'] not in {'paused', 'interrupted', 'failed'} or run['screen_active']:
            raise ValueError('only an idle paused cloud run can be resumed')
        config = self._read_only_resume_config(run, manifest)
        if config.memory.strategy not in CLOUD_STRATEGIES:
            raise ValueError('resume requires a hosted cloud memory strategy')
        if manifest.get('completed_rounds'):
            raise ValueError('this first stage already completed')
        return config

    def resume_cloud_run(self, run_id: str) -> dict[str, Any]:
        """Resume the same cloud scope from its durable per-event journal."""
        with self._launch_lock:
            run = self.store.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            session_name = run.get('screen_session')
            run['screen_active'] = bool(session_name and self.launcher.is_active(str(session_name)))
            manifest = _read_json_if_exists(Path(run['output_dir']) / 'manifest.json') or {}
            self._cloud_resume_config(run, manifest)
            manifest.update(status='queued', error=None, completed_at=None)
            _write_json(Path(run['output_dir']) / 'manifest.json', manifest)
            self.store.update_run(run_id, status='queued', completed_at=None, error=None)
            try:
                name = self.launcher.launch(run_id=run_id, platform_db=self.store.db_path, worker_config=Path(run['worker_config_path']), log_path=Path(run['log_path']))
                self.store.update_run(run_id, screen_session=name)
            except Exception as exc:
                manifest.update(status='paused', error=str(exc), completed_at=_now())
                _write_json(Path(run['output_dir']) / 'manifest.json', manifest)
                self.store.update_run(run_id, status='paused', error=str(exc), completed_at=_now())
                raise
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        manifest_path = Path(run['output_dir']) / 'manifest.json'
        manifest = _read_json_if_exists(manifest_path)
        session_name = run.get('screen_session')
        screen_active = bool(session_name and self.launcher.is_active(str(session_name)))
        if run.get('runner_type') == 'tool_selection':
            run, manifest = self._reconcile_tool_selection_run(run, manifest, screen_active=screen_active)
        elif manifest and manifest.get('status') in TERMINAL_RUN_STATUSES:
            if run['status'] not in TERMINAL_RUN_STATUSES:
                run = self.store.update_run(run_id, status=str(manifest['status']), completed_at=manifest.get('completed_at') or _now(), error=manifest.get('error'))
                run['metrics'] = self.store.get_metrics(run_id)
        if run['status'] in ACTIVE_RUN_STATUSES and (not screen_active) and (manifest is None) and _older_than(str(run['created_at']), seconds=10 if session_name else 60):
            run = self.store.update_run(run_id, status='failed', completed_at=_now(), error='worker exited before producing a manifest' if session_name else 'worker launch was not registered before the platform restarted')
        run['screen_active'] = screen_active
        run['can_resume_cloud'] = False
        pool = (manifest or {}).get('memory_account_pool') or {}
        run['can_resume_memory_accounts'] = bool(not screen_active and run['status'] == 'paused' and (run.get('runner_type') == 'tool_selection') and pool.get('enabled') and (not run.get('experiment_archived_at')) and (not run.get('group_archived_at')) and ((manifest or {}).get('pause_reason') == 'memory_accounts_unavailable'))
        if not screen_active and run['status'] in {'paused', 'interrupted', 'failed'} and ((manifest or {}).get('memory_strategy') in CLOUD_STRATEGIES):
            try:
                self._cloud_resume_config(run, manifest or {})
                run['can_resume_cloud'] = True
            except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
                run['cloud_resume_error'] = str(exc)
        run['screen_attach_command'] = f'screen -r {session_name}' if session_name and (not str(session_name).startswith('process-')) else None
        worker_config = Path(run['worker_config_path'])
        try:
            run['config'] = _read_yaml(worker_config)
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            run['config'] = None
        if run.get('runner_type') == 'tool_selection' and manifest is not None and ('category_statistics' not in manifest or (manifest.get('status') in {'interrupted', 'failed'} and int(manifest.get('completed_queries') or 0) < int(manifest.get('query_count') or 0))):
            query_data = _read_json_if_exists(Path(run['output_dir']) / 'input-queries.json')
            if query_data is None:
                query_data = ((run.get('config') or {}).get('validation', {}).get('queries') or {}).get('dataset')
            if isinstance(query_data, dict) and isinstance(query_data.get('queries'), list):
                selections = _read_json_if_exists(Path(run['output_dir']) / 'selections.json')
                aggregate = selections.get('items') if isinstance(selections, dict) else None
                records = {record['query_id']: record for record in aggregate or [] if isinstance(record, dict) and isinstance(record.get('query_id'), str)} if isinstance(aggregate, list) else {}
                individual_records = [record for path in sorted((Path(run['output_dir']) / 'selections').glob('query-*.json')) if (record := _read_json_if_exists(path)) is not None]
                records.update({record['query_id']: record for record in individual_records if isinstance(record.get('query_id'), str)})
                if isinstance(aggregate, list) or individual_records:
                    manifest = {**manifest, **validation_statistics(query_data['queries'], list(records.values()))}
                    metrics = dict(run.get('metrics') or {})
                    for key, value in _manifest_metrics(manifest).items():
                        if not key.startswith('validation.'):
                            continue
                        previous = metrics.get(key) or {}
                        if previous.get('value') == value and key in metrics:
                            continue
                        metrics[key] = {**({'source': 'selection_artifacts', 'recorded_at': None} if not previous else previous), 'value': value, 'derived_from': 'selection_artifacts'}
                    run['metrics'] = metrics
        run['manifest'] = manifest
        run['events'] = _tail_json_lines(Path(run['output_dir']) / 'events.jsonl', limit=30)
        run['log_tail'] = _tail_text(Path(run['log_path']), max_bytes=24000)
        run.update(self._run_summary(run, manifest=manifest))
        return run

    def list_runs(self, *, experiment_id: str | None=None, group_id: str | None=None, status: str | None=None, limit: int=50, offset: int=0, lightweight: bool=False) -> list[dict[str, Any]]:
        if group_id is not None:
            group_id = group_id.strip().lower()
        runs = self.store.list_runs(experiment_id=experiment_id, group_id=group_id, status=status, limit=limit, offset=offset, include_resource_content=not lightweight)
        summaries = []
        for run in runs:
            metrics = run.get('metrics') or {}
            if run.get('runner_type') == 'tool_selection':
                has_progress_metrics = _metric_int(metrics, 'validation.query_count') is not None and _metric_int(metrics, 'validation.completed_queries') is not None
            else:
                has_progress_metrics = _metric_int(metrics, 'run.target_rounds') is not None and _metric_int(metrics, 'run.completed_rounds') is not None
            needs_manifest = not lightweight or run.get('status') in ACTIVE_RUN_STATUSES or (not has_progress_metrics)
            manifest = _read_json_if_exists(Path(run['output_dir']) / 'manifest.json') if needs_manifest else None
            if run.get('runner_type') == 'tool_selection':
                run, manifest = self._reconcile_tool_selection_run(run, manifest)
            if status is not None and run.get('status') != status:
                continue
            summaries.append(self._run_summary(run, manifest=manifest, allow_file_reads=not lightweight))
        return summaries

    def delete_run(self, run_id: str) -> dict[str, Any]:
        result = self.delete_runs([run_id])
        response: dict[str, Any] = {'deleted': True, 'run_id': run_id}
        if result.get('cleanup_pending') is not None:
            response['cleanup_pending'] = result['cleanup_pending']
        return response

    def delete_runs(self, run_ids: list[str]) -> dict[str, Any]:
        if not isinstance(run_ids, list):
            raise ValueError('run_ids must be an array')
        normalized_ids: list[str] = []
        for run_id in run_ids:
            if not isinstance(run_id, str) or not run_id.strip() or run_id != run_id.strip():
                raise ValueError('every run id must be a non-empty string')
            if run_id not in normalized_ids:
                normalized_ids.append(run_id)
        if not normalized_ids:
            raise ValueError('select at least one run')
        if len(normalized_ids) > 200:
            raise ValueError('delete at most 200 runs at once')
        with self._launch_lock:
            runs: list[tuple[dict[str, Any], tuple[Path, ...]]] = []
            for run_id in normalized_ids:
                run = self.get_run(run_id)
                if run['status'] in ACTIVE_RUN_STATUSES or run['screen_active']:
                    raise PermissionError(f'active runs cannot be deleted: {run_id}')
                runs.append((run, self._validated_run_artifacts(run)))
            paths_to_stage = [path for _, artifacts in runs for path in artifacts]

            def commit_run_delete() -> None:
                deleted_count = self.store.delete_runs(normalized_ids)
                if deleted_count != len(normalized_ids):
                    raise RuntimeError('not every selected run was deleted')
            cleanup_pending = self._commit_with_quarantined_paths(paths_to_stage, operation='run deletion', stage_prefix='runs', commit=commit_run_delete)
        result: dict[str, Any] = {'deleted': True, 'deleted_count': len(normalized_ids), 'run_ids': normalized_ids}
        if cleanup_pending is not None:
            result['cleanup_pending'] = cleanup_pending
        return result

    def _experiment_run_records(self, experiment_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.store.list_runs(experiment_id=experiment_id, limit=200, offset=offset)
            records.extend(page)
            if len(page) < 200:
                return records
            offset += len(page)

    def _validated_experiment_definition(self, experiment: dict[str, Any]) -> tuple[Path, Path]:
        experiment_id = str(experiment['experiment_id'])
        if not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
            raise PermissionError('experiment id is not safe to delete')
        config_dir = (self.exp_dir / experiment_id).absolute()
        expected_config = config_dir / 'config.yaml'
        registered_config = Path(experiment['config_path']).absolute()
        if registered_config != expected_config:
            raise PermissionError('experiment config path does not match the platform registry')
        if config_dir.is_symlink() or config_dir.resolve().parent != self.exp_dir.resolve():
            raise PermissionError('experiment config directory is outside the project experiment tree')
        return (expected_config, config_dir)

    def dashboard(self) -> dict[str, Any]:
        recent_runs = self.list_runs(limit=10, lightweight=True)
        return {'counts': self.store.dashboard_counts(), 'recent_runs': recent_runs, 'experiments': self.store.list_experiments(include_config=False)}

    def compare_runs(self, run_ids: list[str]) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(run_ids))
        if len(unique_ids) < 2 or len(unique_ids) > 20:
            raise ValueError('compare between 2 and 20 runs')
        runs = []
        for run_id in unique_ids:
            run = self.store.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            runs.append(run)
        metric_keys = sorted({key for run in runs for key in (run.get('metrics') or {}).keys()})
        return {'runs': runs, 'metric_keys': metric_keys}

    def validation_comparison_options(self) -> dict[str, Any]:
        """List second-stage runs carrying the unified exact-leak metric."""
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.store.list_runs(runner_type='tool_selection', archived=None, limit=200, offset=offset)
            for run in page:
                record = self._validation_comparison_record(run)
                if record is not None:
                    records.append(record)
            if len(page) < 200:
                break
            offset += len(page)
        return {'metric': EXACT_LEAK_METRIC, 'historical_backfill': False, 'runs': records, 'filters': {'experiment_groups': _comparison_filter_values(records, 'group_id', 'group_name'), 'posts': _comparison_filter_values(records, 'post_filter_id', 'post_label'), 'memory_strategies': sorted({str(item['memory_strategy']) for item in records if item.get('memory_strategy')}), 'models': sorted({str(item['behavior_model']) for item in records if item.get('behavior_model')}), 'mcps': _comparison_filter_values(records, 'mcp_filter_id', 'mcp_label')}}

    def validation_comparison(self, run_ids: Any) -> dict[str, Any]:
        if not isinstance(run_ids, list):
            raise ValueError('run_ids must be an array')
        unique_ids: list[str] = []
        for value in run_ids:
            if not isinstance(value, str) or not value.strip():
                raise ValueError('every run id must be a non-empty string')
            if value not in unique_ids:
                unique_ids.append(value)
        if not 1 <= len(unique_ids) <= 200:
            raise ValueError('compare between 1 and 200 second-stage runs')
        records: list[dict[str, Any]] = []
        for run_id in unique_ids:
            run = self.store.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            record = self._validation_comparison_record(run)
            if record is None:
                raise ValueError(f'run {run_id} has no exact leakage metrics')
            records.append(record)
        return {'metric': EXACT_LEAK_METRIC, 'historical_backfill': False, 'runs': records, 'aggregate': _aggregate_validation_comparison(records)}

    def _validation_comparison_record(self, run: dict[str, Any]) -> dict[str, Any] | None:
        if run.get('runner_type') != 'tool_selection':
            return None
        if run.get('status') not in {'completed', 'completed_with_errors'}:
            return None
        metrics = run.get('metrics') or {}
        metric = _metric_value(metrics, 'validation.exact_leak_metric')
        if metric != EXACT_LEAK_METRIC:
            return None
        display = read_run_display(run)
        post_ref = (run.get('resource_refs') or {}).get('posts') or {}
        post_filter_id = post_ref.get('revision_id')
        post_label = post_ref.get('resource_name')
        if post_label and post_ref.get('revision_number'):
            post_label = f"{post_label} · r{post_ref['revision_number']}"
        if not post_filter_id:
            source_run_id = display.get('source_run_id')
            source_run = self.store.get_run(source_run_id) if isinstance(source_run_id, str) and source_run_id else None
            source_manifest = (_read_json_if_exists(Path(source_run['output_dir']) / 'manifest.json') if source_run is not None else None) or {}
            source_post_ids = [str(value) for value in source_manifest.get('source_post_ids') or [] if value is not None and str(value)]
            if not source_post_ids and source_manifest.get('source_post_id') is not None:
                source_post_ids = [str(source_manifest['source_post_id'])]
            if source_post_ids:
                identity = json.dumps(source_post_ids, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                post_filter_id = 'posts:' + hashlib.sha256(identity).hexdigest()[:16]
                preview = ', '.join(source_post_ids[:3])
                post_label = preview if len(source_post_ids) <= 3 else f'{preview} … ({len(source_post_ids)} posts)'
            else:
                source_display = read_run_display(source_run) if source_run else {}
                source_kind = source_display.get('post_source') or 'unknown'
                post_filter_id = f'source:{source_kind}'
                post_label = source_display.get('post_filename') or ('Database-selected posts' if source_kind == 'database' else 'Unidentified post source')
        counts = {'queries': _metric_number(metrics, 'validation.query_count'), 'completed': _metric_number(metrics, 'validation.completed_queries'), 'target_evaluated': _metric_number(metrics, 'validation.target_evaluated_queries'), 'target_hits': _metric_number(metrics, 'validation.target_hits'), 'exact_leak_evaluated': _metric_number(metrics, 'validation.exact_leak_evaluated_queries'), 'exact_leak_present': _metric_number(metrics, 'validation.exact_leak_present_queries'), 'exact_leaks': _metric_number(metrics, 'validation.exact_leaks'), 'exact_leak_target_selected': _metric_number(metrics, 'validation.exact_leak_target_selected_queries'), 'exact_leak_joint_leaks': _metric_number(metrics, 'validation.exact_leak_joint_leaks'), 'exact_leak_outside_target_leaks': _metric_number(metrics, 'validation.exact_leak_outside_target_leaks')}
        category_value = _metric_value(metrics, 'validation.category_statistics')
        return {'run_id': run['run_id'], 'experiment_id': run['experiment_id'], 'experiment_name': run.get('experiment_name'), 'group_id': run.get('group_id'), 'group_name': run.get('group_name'), 'status': run.get('status'), 'created_at': run.get('created_at'), 'completed_at': run.get('completed_at'), 'post_filter_id': post_filter_id, 'post_resource_id': post_ref.get('resource_id'), 'post_revision_id': post_ref.get('revision_id'), 'post_label': post_label, 'memory_strategy': display.get('memory_strategy'), 'behavior_model': display.get('behavior_model'), **_comparison_mcp_fields(run), 'counts': counts, 'rates': {'target_hit': _ratio(counts['target_hits'], counts['target_evaluated']), 'exact_leak_presence': _ratio(counts['exact_leak_present'], counts['exact_leak_evaluated']), 'exact_leak': _ratio(counts['exact_leaks'], counts['exact_leak_evaluated']), 'exact_leak_target_joint': _ratio(counts['exact_leak_joint_leaks'], counts['exact_leak_target_selected'])}, 'categories': category_value if isinstance(category_value, dict) else {}}

    def save_metrics(self, run_id: str, metrics: dict[str, Any], *, source: str='analysis') -> dict[str, Any]:
        return self.store.save_metrics(run_id, metrics, source=source)

    def run_thread(self, run_id: str, *, comments_limit: int=200) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if comments_limit < 1 or comments_limit > 1000:
            raise ValueError('comments_limit must be between 1 and 1000')
        run_dir = Path(run['output_dir'])
        snapshots = sorted((run_dir / 'snapshots').glob('thread-round-*.json'))
        source = snapshots[-1] if snapshots else run_dir / 'snapshots' / 'thread-initial.json'
        thread = _read_json_if_exists(source)
        if not thread:
            return {'available': False, 'thread': None}
        comments = list(thread.get('comments') or [])
        truncated = max(0, len(comments) - comments_limit)
        if truncated:
            thread = dict(thread)
            thread['comments'] = comments[-comments_limit:]
        return {'available': True, 'thread': thread, 'total_comments': len(comments), 'truncated_comments': truncated, 'snapshot': str(source.name)}

    def run_forum(self, run_id: str, *, post_limit: int=100, comments_limit: int=200) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if post_limit < 1 or post_limit > 500:
            raise ValueError('post_limit must be between 1 and 500')
        if comments_limit < 1 or comments_limit > 1000:
            raise ValueError('comments_limit must be between 1 and 1000')
        run_dir = Path(run['output_dir'])
        snapshots = sorted((run_dir / 'snapshots').glob('forum-round-*.json'))
        source = snapshots[-1] if snapshots else run_dir / 'snapshots' / 'forum-initial.json'
        forum = _read_json_if_exists(source)
        if not forum:
            return {'available': False, 'forum': None}
        all_posts = list(forum.get('posts') or [])
        posts = []
        for item in all_posts[:post_limit]:
            post = dict(item)
            comments = list(post.get('comments') or [])
            post['comments'] = comments[-comments_limit:]
            post['api_truncated_comments'] = max(0, len(comments) - comments_limit)
            posts.append(post)
        result = dict(forum)
        result['posts'] = posts
        return {'available': True, 'forum': result, 'returned_posts': len(posts), 'api_truncated_posts': max(0, len(all_posts) - len(posts)), 'snapshot': str(source.name)}

    def run_rounds(self, run_id: str, *, limit: int=20, offset: int=0) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if limit < 1 or limit > 100 or offset < 0:
            raise ValueError('invalid rounds pagination')
        round_files = sorted((Path(run['output_dir']) / 'rounds').glob('round-*.json'))
        selected = round_files[offset:offset + limit]
        return {'items': [_read_json_if_exists(path) for path in selected], 'total': len(round_files), 'limit': limit, 'offset': offset}

    def run_selections(self, run_id: str, *, limit: int=100, offset: int=0) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.get('runner_type') != 'tool_selection':
            raise ValueError('tool selections are only available for second-stage runs')
        if limit < 1 or limit > 100 or offset < 0:
            raise ValueError('invalid selections pagination')
        files = sorted((Path(run['output_dir']) / 'selections').glob('query-*.json'))
        items = []
        for path in files[offset:offset + limit]:
            record = _read_json_if_exists(path)
            if record is not None:
                summary = {key: value for key, value in record.items() if key not in {'request', 'raw_response', 'memory_context'}}
                items.append(summary if record.get('memory_context') is not None else _redact_api_key_values(summary))
        return {'items': items, 'total': len(files), 'limit': limit, 'offset': offset, 'has_more': offset + limit < len(files)}

    def run_selection_detail(self, run_id: str, query_index: int) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.get('runner_type') != 'tool_selection':
            raise ValueError('selection detail requires a second-stage run')
        if not 1 <= query_index <= 500:
            raise ValueError('invalid query index')
        path = Path(run['output_dir']) / 'selections' / f'query-{query_index:04d}.json'
        detail = _read_json_if_exists(path)
        if detail is None:
            raise KeyError(f'{run_id}/query-{query_index:04d}')
        return detail

    def run_memory_result(self, run_id: str, *, limit: int=100, offset: int=0, calls_limit: int=30, calls_offset: int=0, agent_id: str | None=None, round_index: int | None=None) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.get('runner_type') == 'tool_selection':
            raise ValueError('memory result requires a first-stage run')
        if not 1 <= limit <= 200 or offset < 0 or (not 1 <= calls_limit <= 100) or (calls_offset < 0):
            raise ValueError('invalid memory result pagination')
        memory_dir = Path(run['output_dir']) / 'memory'
        if (run.get('config') or {}).get('memory', {}).get('strategy') == 'agent_memory_preview' or (_read_json_if_exists(Path(run['output_dir']) / 'manifest.json') or {}).get('preview_only'):
            from .agent_memory_preview import preview_result
            return preview_result(Path(run['output_dir']), limit=limit, offset=offset)
        snapshots = sorted(memory_dir.glob('memory-round-*.json'))
        if round_index is not None:
            if type(round_index) is not int or round_index < 1:
                raise ValueError('round_index must be a positive integer')
            candidate = memory_dir / f'memory-round-{round_index:03d}.json'
            snapshots = [candidate] if candidate.is_file() else []
        if not snapshots:
            return {'available': False, 'items': [], 'total': 0, 'model_calls': [], 'calls_total': 0}
        latest = snapshots[-1]
        payload = _read_json_if_exists(latest) or {}
        round_tag = latest.stem.removeprefix('memory-round-')
        exported = _read_json_if_exists(memory_dir / f'remote-export-round-{round_tag}.json') or {}
        records: list[dict[str, Any]] = []
        entries: list[dict[str, Any]] = []
        for selected_agent_id, wrapper in payload.items():
            if agent_id is not None and selected_agent_id != agent_id:
                continue
            if not isinstance(wrapper, dict) or not isinstance(wrapper.get('snapshot'), dict):
                continue
            snapshot = wrapper['snapshot']
            remote = exported.get(selected_agent_id) if isinstance(exported, dict) else None
            if hasattr(self, 'project_root') and hasattr(self.store, 'db_path'):
                source = self.memory_workspace.source(run_id, selected_agent_id, int(round_tag))
                records.append(self.memory_workspace.record(source))
                remote = self.memory_workspace.cached_export(source) or remote
            strategy = snapshot.get('strategy')
            if strategy == 'langmem_official':
                versions = langmem_versions(memory_dir / latest.stem, selected_agent_id)
            elif strategy == 'a_mem_official':
                versions = amem_versions(memory_dir / latest.stem, selected_agent_id)
            else:
                versions = {}
            for item in memory_inventory(snapshot, remote):
                if item['id'] in versions:
                    if strategy == 'a_mem_official':
                        item['observed_versions'] = versions[item['id']]
                    else:
                        item['history'] = versions[item['id']]
                entries.append({'agent_id': selected_agent_id, 'strategy': snapshot.get('strategy'), **item})
        trace_path = memory_dir / 'model-calls.jsonl'
        calls: list[dict[str, Any]] = []
        if trace_path.is_file():
            with trace_path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        call = json.loads(line)
                        if agent_id is None or call.get('agent_id') == agent_id:
                            calls.append(call)
                    except json.JSONDecodeError:
                        continue
        record = records[0] if len(records) == 1 else {}
        not_fetched = records and all((row['strategy'] in {'hindsight_cloud', 'mem0_platform_v3', 'memos_cloud'} and row['pull_status'] in {'not_fetched', 'failed'} for row in records))
        return {**record, 'records': records, 'available': True, 'snapshot': latest.name, 'items': entries[offset:offset + limit], 'total': None if not_fetched else len(entries), 'limit': limit, 'offset': offset, 'model_calls': calls[calls_offset:calls_offset + calls_limit], 'calls_total': len(calls), 'calls_offset': calls_offset, 'exports': {agent_id: {'completeness': value.get('completeness'), 'kind': value.get('kind'), 'error': value.get('error'), 'item_count': len(value.get('items') or [])} for agent_id, value in exported.items() if isinstance(value, dict)}}

    def _run_summary(self, run: dict[str, Any], *, manifest: dict[str, Any] | None=None, allow_file_reads: bool=True) -> dict[str, Any]:
        summary = dict(run)
        if manifest is None and allow_file_reads:
            manifest = _read_json_if_exists(Path(run['output_dir']) / 'manifest.json')
        display = read_run_display(run) if allow_file_reads else dict(run.get('display_config') or {})
        if run.get('runner_type') == 'tool_selection':
            source_id = (manifest or {}).get('source_run_id') or display.get('source_run_id')
            source_display: dict[str, Any] = {}
            if allow_file_reads:
                source_run = self.store.get_run(source_id) if isinstance(source_id, str) else None
                source_display = read_run_display(source_run) if source_run else {}
                for key in ('post_filename', 'post_source', 'hide_speaker', 'speaker_as_json'):
                    display[key] = source_display.get(key)
            display['memory_strategy'] = (manifest or {}).get('memory_strategy') or display.get('memory_strategy') or source_display.get('memory_strategy')
        summary['display_config'] = {key: display.get(key) for key in ('post_filename', 'post_source', 'memory_strategy', 'hide_speaker', 'speaker_as_json')}
        summary['memory_event_progress'] = manifest.get('memory_event_progress') if isinstance(manifest, dict) else None
        missing_config = object()
        config_value: object | dict[str, Any] | None = run.get('config', missing_config)

        def get_config() -> dict[str, Any] | None:
            nonlocal config_value
            if config_value is missing_config:
                if not allow_file_reads:
                    config_value = None
                    return None
                try:
                    config_value = _read_yaml(Path(run['worker_config_path']))
                except (OSError, TypeError, ValueError, yaml.YAMLError):
                    config_value = None
            return config_value if isinstance(config_value, dict) else None
        if run.get('runner_type') == 'tool_selection':
            metrics = run.get('metrics') or {}
            target = _nonnegative_int(manifest.get('query_count')) if isinstance(manifest, dict) else None
            if not target and allow_file_reads:
                config = get_config()
                validation = config.get('validation', {}) if config else {}
                dataset = (validation.get('queries') or {}).get('dataset')
                if dataset is not None:
                    try:
                        target = normalize_query_dataset(dataset)['query_count']
                    except (TypeError, ValueError):
                        target = None
            if target is None or target == 0:
                target = _metric_int(metrics, 'validation.query_count')
            completed = _nonnegative_int(manifest.get('completed_queries')) if isinstance(manifest, dict) else None
            if completed is None:
                completed = _metric_int(metrics, 'validation.completed_queries')
            target = max(0, target or 0)
            completed = max(0, completed or 0)
            summary['progress'] = {'unit': 'queries', 'completed_queries': completed, 'target_queries': target, 'completed_rounds': completed, 'target_rounds': target, 'percent': min(100, round(completed * 100 / target)) if target else 0}
            summary['can_delete'] = summary.get('status') not in ACTIVE_RUN_STATUSES
            summary['can_interrupt'] = summary.get('status') in ACTIVE_RUN_STATUSES and summary.get('screen_active', True)
            return summary
        completed = _manifest_completed_rounds(manifest)
        target = _positive_int(manifest.get('target_rounds')) if manifest else None
        metrics = run.get('metrics') or {}
        if completed is None:
            completed = _metric_int(metrics, 'run.completed_rounds')
        if target is None and (not allow_file_reads):
            target = _metric_int(metrics, 'run.target_rounds', positive=True)
        if target is None and allow_file_reads:
            config = get_config()
            if config:
                simulation = config.get('simulation')
                if isinstance(simulation, dict):
                    target = _positive_int(simulation.get('rounds'))
        if target is None:
            target = _metric_int(metrics, 'run.target_rounds', positive=True)
        completed = max(0, completed or 0)
        target = max(0, target or 0)
        percent = min(100, round(completed * 100 / target)) if target else 0
        summary['progress'] = {'completed_rounds': completed, 'target_rounds': target, 'percent': percent}
        summary['can_delete'] = summary.get('status') not in ACTIVE_RUN_STATUSES
        summary['can_interrupt'] = summary.get('status') in ACTIVE_RUN_STATUSES and summary.get('screen_active', True)
        return summary

    def _reconcile_tool_selection_run(self, run: dict[str, Any], manifest: dict[str, Any] | None, *, screen_active: bool | None=None) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Converge immutable validation runs after their worker disappears."""
        if manifest is None:
            return (run, manifest)
        run_id = str(run['run_id'])
        manifest_status = str(manifest.get('status') or '')
        if manifest_status == 'paused' and manifest.get('pause_reason') == 'memory_accounts_unavailable' and (run.get('status') in ACTIVE_RUN_STATUSES):
            session_name = run.get('screen_session')
            if screen_active is None:
                screen_active = bool(session_name and self.launcher.is_active(str(session_name)))
            requested_at = manifest.get('memory_account_resume_requested_at')
            if screen_active or (requested_at and (not _older_than(str(requested_at), seconds=10))):
                return (run, manifest)
        if manifest_status in TERMINAL_RUN_STATUSES:
            if run.get('status') not in TERMINAL_RUN_STATUSES:
                run = self.store.update_run(run_id, status=manifest_status, completed_at=manifest.get('completed_at') or _now(), error=manifest.get('error'))
            if not run.get('metrics'):
                run['metrics'] = self.store.save_metrics(run_id, _manifest_metrics(manifest), source='platform_recovery')
            return (run, manifest)
        session_name = run.get('screen_session')
        if run.get('status') not in ACTIVE_RUN_STATUSES or not session_name:
            return (run, manifest)
        if screen_active is None:
            screen_active = self.launcher.is_active(str(session_name))
        if screen_active:
            return (run, manifest)
        completed_at = _now()
        error = 'second-stage worker exited before completing validation'
        manifest = dict(manifest)
        manifest.update(status='interrupted', completed_at=completed_at, error=error)
        _write_json(Path(run['output_dir']) / 'manifest.json', manifest)
        run = self.store.update_run(run_id, status='interrupted', completed_at=completed_at, error=error)
        run['metrics'] = self.store.save_metrics(run_id, _manifest_metrics(manifest), source='platform_recovery')
        return (run, manifest)

    def _commit_with_quarantined_paths(self, paths: list[Path], *, operation: str, stage_prefix: str, commit: Callable[[], None]) -> str | None:
        """Stage registered files, commit deletion, and restore on failure."""
        quarantine_root = self.runtime_dir / 'trash'
        quarantine_root.mkdir(parents=True, exist_ok=True)
        if quarantine_root.is_symlink() or quarantine_root.resolve().parent != self.runtime_dir.resolve():
            raise PermissionError(f'{operation} quarantine is outside the platform runtime tree')
        stage_dir = quarantine_root / f'{stage_prefix}-{secrets.token_hex(8)}'
        stage_dir.mkdir(parents=True, exist_ok=False)
        staged: list[tuple[Path, Path]] = []
        try:
            for index, original in enumerate(paths):
                if not (original.is_symlink() or original.exists()):
                    continue
                quarantined = stage_dir / f'{index:04d}-{original.name}'
                original.replace(quarantined)
                staged.append((original, quarantined))
            commit()
        except Exception as exc:
            rollback_failures = []
            for original, quarantined in reversed(staged):
                try:
                    original.parent.mkdir(parents=True, exist_ok=True)
                    quarantined.replace(original)
                except OSError as rollback_error:
                    rollback_failures.append(f'{quarantined} -> {original}: {rollback_error}')
            try:
                stage_dir.rmdir()
            except OSError:
                pass
            if rollback_failures:
                raise RuntimeError(f'{operation} failed and quarantined files could not be fully restored: ' + '; '.join(rollback_failures)) from exc
            raise
        try:
            shutil.rmtree(stage_dir)
        except OSError:
            return str(stage_dir)
        return None

    def _validated_run_artifacts(self, run: dict[str, Any]) -> tuple[Path, ...]:
        run_id = str(run['run_id'])
        worker_config = Path(run['worker_config_path']).absolute()
        expected_worker = (self.jobs_dir / f'{run_id}.yaml').absolute()
        log_path = Path(run['log_path']).absolute()
        expected_log = (self.logs_dir / f'{run_id}.log').absolute()
        if worker_config != expected_worker or log_path != expected_log:
            raise PermissionError('run artifact paths do not match the platform registry')
        output_dir = Path(run['output_dir']).absolute()
        output_root = (self.project_root / 'output').resolve()
        output_parent = output_dir.parent.resolve()
        if output_dir.name != run_id or (output_parent != output_root and output_root not in output_parent.parents):
            raise PermissionError('run output directory is outside the project output tree')
        return (output_dir, worker_config, log_path)

    def _validate_config(self, config: dict[str, Any]) -> ExperimentConfig:
        if not isinstance(config, dict):
            raise ValueError('experiment config must be an object')
        _validate_platform_model_selection(config)
        temp_dir = self.project_root / 'tmp' / 'platform-validation'
        temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', prefix='config-', dir=temp_dir, encoding='utf-8', delete=False) as handle:
            yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False, default_flow_style=False)
            path = Path(handle.name)
        try:
            validated = load_experiment_config(path, project_root=self.project_root)
            return validated
        finally:
            path.unlink(missing_ok=True)

def _read_yaml(path: Path) -> dict[str, Any]:
    loader = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
    value = yaml.load(path.read_text(encoding='utf-8'), Loader=loader)
    if not isinstance(value, dict):
        raise ValueError(f'Experiment config root must be an object: {path}')
    return value

def _write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False, default_flow_style=False), encoding='utf-8')
    temporary.replace(path)

def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    temporary.replace(path)

def _append_json_line(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n')
        handle.flush()

def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None

def _manifest_completed_rounds(manifest: dict[str, Any] | None) -> int | None:
    if not manifest or 'completed_rounds' not in manifest:
        return None
    completed = manifest.get('completed_rounds')
    if isinstance(completed, list):
        return len(completed)
    return _nonnegative_int(completed)

def _metric_int(metrics: dict[str, Any], key: str, *, positive: bool=False) -> int | None:
    record = metrics.get(key)
    value = record.get('value') if isinstance(record, dict) else None
    return _positive_int(value) if positive else _nonnegative_int(value)

def _metric_value(metrics: dict[str, Any], key: str) -> Any:
    record = metrics.get(key)
    return record.get('value') if isinstance(record, dict) else None

def _metric_number(metrics: dict[str, Any], key: str) -> int:
    value = _metric_value(metrics, key)
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, result)

def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {'numerator': numerator, 'denominator': denominator, 'value': round(numerator / denominator, 6) if denominator else None}

def _comparison_mcp_fields(run: dict[str, Any]) -> dict[str, Any]:
    """Identify the MCP revision or actual frozen input used by this run."""
    revision = (run.get('resource_refs') or {}).get('mcp') or {}
    revision_id = revision.get('revision_id')
    if isinstance(revision_id, str) and revision_id:
        label = revision.get('resource_name') or revision.get('filename') or revision_id
        if revision.get('revision_number'):
            label = f"{label} · r{revision['revision_number']}"
        return {'mcp_filter_id': revision_id, 'mcp_resource_id': revision.get('resource_id'), 'mcp_revision_id': revision_id, 'mcp_label': label}
    output = Path(run['output_dir'])
    paths = [output / 'config.resolved.json', output / 'config.yaml']
    if run.get('worker_config_path'):
        paths.append(Path(run['worker_config_path']))
    mcp: dict[str, Any] = {}
    for path in paths:
        try:
            config = _read_json_if_exists(path) if path.suffix == '.json' else _read_yaml(path)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            continue
        validation = config.get('validation') if isinstance(config, dict) else None
        candidate = validation.get('mcp') if isinstance(validation, dict) else None
        if isinstance(candidate, dict):
            mcp = candidate
            break
    try:
        dataset = _read_json_if_exists(output / 'input-mcp.json')
    except (OSError, UnicodeError, ValueError):
        dataset = None
    if dataset is None:
        dataset = mcp.get('dataset')
    filename = mcp.get('filename')
    label = filename.replace('\\', '/').rsplit('/', 1)[-1] if isinstance(filename, str) and filename else 'Embedded MCP'
    if isinstance(dataset, dict):
        try:
            digest = _resource_content_sha256(dataset)
        except (TypeError, ValueError):
            digest = None
        if digest:
            return {'mcp_filter_id': f'mcp:{digest}', 'mcp_resource_id': None, 'mcp_revision_id': None, 'mcp_label': f'{label} · {digest[:8]}'}
    return {'mcp_filter_id': f"mcp:unknown:{run['run_id']}", 'mcp_resource_id': None, 'mcp_revision_id': None, 'mcp_label': label if filename else 'Unidentified MCP source'}

def _comparison_filter_values(records: list[dict[str, Any]], key: str, label_key: str) -> list[dict[str, str]]:
    values: dict[str, str] = {}
    for record in records:
        value = record.get(key)
        if not isinstance(value, str) or not value:
            continue
        label = record.get(label_key)
        values[value] = str(label or value)
    return [{'value': value, 'label': values[value]} for value in sorted(values, key=lambda item: (values[item].lower(), item))]

def _aggregate_validation_comparison(records: list[dict[str, Any]]) -> dict[str, Any]:
    count_keys = ('queries', 'completed', 'target_evaluated', 'target_hits', 'exact_leak_evaluated', 'exact_leak_present', 'exact_leaks', 'exact_leak_target_selected', 'exact_leak_joint_leaks', 'exact_leak_outside_target_leaks')
    counts = {key: sum((int(record.get('counts', {}).get(key) or 0) for record in records)) for key in count_keys}
    category_counts: dict[str, dict[str, Any]] = {}
    additive_category_keys = ('query_count', 'completed_queries', 'successful_actions', 'failed_actions', 'target_configured_queries', 'target_evaluated_queries', 'target_hits', 'target_misses', 'exact_leak_evaluated_queries', 'exact_leak_present_queries', 'exact_leaks', 'exact_leak_target_selected_queries', 'exact_leak_joint_leaks', 'exact_leak_outside_target_leaks')
    for record in records:
        for category, source in (record.get('categories') or {}).items():
            if not isinstance(source, dict):
                continue
            target = category_counts.setdefault(category, {'functional_group': source.get('functional_group', category)})
            for key in additive_category_keys:
                target[key] = int(target.get(key) or 0) + int(source.get(key) or 0)
    for category in category_counts.values():
        category['target_hit_rate'] = _ratio(int(category.get('target_hits') or 0), int(category.get('target_evaluated_queries') or 0))['value']
        category['exact_leak_rate'] = _ratio(int(category.get('exact_leaks') or 0), int(category.get('exact_leak_evaluated_queries') or 0))['value']
    return {'run_count': len(records), 'counts': counts, 'rates': {'target_hit': _ratio(counts['target_hits'], counts['target_evaluated']), 'exact_leak_presence': _ratio(counts['exact_leak_present'], counts['exact_leak_evaluated']), 'exact_leak': _ratio(counts['exact_leaks'], counts['exact_leak_evaluated']), 'exact_leak_target_joint': _ratio(counts['exact_leak_joint_leaks'], counts['exact_leak_target_selected'])}, 'categories': category_counts}

def _nonnegative_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None

def _positive_int(value: Any) -> int | None:
    result = _nonnegative_int(value)
    return result if result and result > 0 else None

def _experiment_matches_stage(experiment: dict[str, Any], stage: str | None) -> bool:
    if stage is None:
        return True
    runner_type = experiment.get('runner_type')
    if stage == 'tool_selection':
        return runner_type == 'tool_selection'
    return runner_type in {'moltnet_thread', 'moltnet_forum', 'interactive_thread'}

def _experiment_dependency_index(experiments: list[dict[str, Any]]) -> dict[str, list[str]]:
    experiments_by_id = {str(experiment['experiment_id']): experiment for experiment in experiments}
    index: dict[str, list[str]] = {}
    for candidate in experiments:
        if candidate.get('runner_type') != 'tool_selection':
            continue
        candidate_id = str(candidate['experiment_id'])
        for source_id in _experiment_source_ids(candidate):
            source = experiments_by_id.get(source_id)
            if source_id != candidate_id and source is not None and (source.get('runner_type') != 'tool_selection') and (source.get('group_id') == candidate.get('group_id')):
                index.setdefault(source_id, []).append(candidate_id)
    return {source_id: sorted(set(candidate_ids)) for source_id, candidate_ids in index.items()}

def _experiment_source_references(experiment: dict[str, Any]) -> list[str]:
    if experiment.get('runner_type') != 'tool_selection':
        return []
    explicit = experiment.get('source_experiment_ids')
    if isinstance(explicit, list):
        return sorted({value.strip().lower() for value in explicit if isinstance(value, str) and value.strip()})
    validation = experiment.get('config', {}).get('validation')
    if not isinstance(validation, dict):
        return []
    memory_source = validation.get('memory_source')
    return sorted({value.strip().lower() for value in (validation.get('source_experiment_id'), memory_source.get('experiment_id') if isinstance(memory_source, dict) else None) if isinstance(value, str) and value.strip()})

def _experiment_source_ids(experiment: dict[str, Any]) -> list[str]:
    references = _experiment_source_references(experiment)
    return references if len(references) == 1 else []

def _invalid_experiment_dependency_index(experiments: list[dict[str, Any]]) -> dict[str, list[str]]:
    experiments_by_id = {str(experiment['experiment_id']): experiment for experiment in experiments}
    index: dict[str, list[str]] = {}
    for candidate in experiments:
        if candidate.get('runner_type') != 'tool_selection':
            continue
        candidate_id = str(candidate['experiment_id'])
        references = _experiment_source_references(candidate)
        invalid_sources: list[str] = []
        if len(references) > 1:
            invalid_sources = references
        elif references:
            source = experiments_by_id.get(references[0])
            if source is None or (source.get('runner_type') == 'tool_selection' or source.get('group_id') != candidate.get('group_id')):
                invalid_sources = references
        for source_id in invalid_sources:
            if source_id != candidate_id:
                index.setdefault(source_id, []).append(candidate_id)
    return {source_id: sorted(set(candidate_ids)) for source_id, candidate_ids in index.items()}

def _resolve_category_target_mapping(mcp: dict[str, Any], mapping: dict[str, str]) -> dict[str, tuple[str, str]]:
    resolved: dict[str, tuple[str, str]] = {}
    for category, name in mapping.items():
        servers = [server for server in mcp.get('servers') or [] if server.get('name') == name or server.get('slug') == name]
        if len(servers) != 1 or servers[0].get('functional_group') != category:
            raise ValueError(f'category {category} must match one MCP server named {name}')
        tools = servers[0].get('tools') or []
        if len(tools) != 1:
            raise ValueError(f'target MCP {name} must have exactly one tool')
        resolved[category] = (servers[0]['slug'], tools[0]['name'])
    return resolved

def _apply_category_target_mapping(queries: dict[str, Any], mcp: dict[str, Any], mapping: dict[str, str]) -> None:
    """Resolve category/MCP display names to the one exact tool used by metrics."""
    query_rows = queries.get('queries') or []
    categories = {row.get('functional_group') for row in query_rows}
    if None in categories or '' in categories:
        raise ValueError('every mapped query needs a functional_group')
    missing = categories - set(mapping)
    if missing:
        raise ValueError('target mapping lacks query categories: ' + ', '.join(sorted(missing)))
    unknown = set(mapping) - categories
    if unknown:
        raise ValueError('target mapping contains categories absent from queries: ' + ', '.join(sorted(unknown)))
    resolved = _resolve_category_target_mapping(mcp, mapping)
    for query in query_rows:
        server, tool = resolved[query['functional_group']]
        pool = query.get('server_pool')
        if pool is not None and server not in pool:
            raise ValueError(f"query {query['query_id']} excludes mapped MCP {server}")
        evaluation = query.setdefault('evaluation', {})
        evaluation.update(target_server=server, target_tool=tool, target_tool_fqn=f'{server}.{tool}')
    validate_selection_inputs(queries, mcp)

def _normalize_group_resource(value: Any, *, name: str, normalize: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f'{name} must be an imported file object')
    unknown = sorted(set(value) - {'filename', 'dataset'})
    if unknown:
        raise ValueError(f'unknown {name} fields: ' + ', '.join(unknown))
    filename = value.get('filename')
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError(f'{name}.filename cannot be empty')
    filename = filename.strip()
    if not filename.lower().endswith('.json'):
        raise ValueError(f'{name} must be imported from a JSON file')
    return {'filename': filename, 'dataset': normalize(value.get('dataset'))}

def _registered_ref_id(value: Any, *, label: str, reject_embedded: bool=False) -> str:
    if not isinstance(value, dict):
        raise ValueError(f'{label} must select a registered resource revision')
    if reject_embedded and ('dataset' in value or 'content' in value):
        raise ValueError(f'{label} cannot embed imported data; register the file in Resources first')
    revision_id = value.get('resource_revision_id')
    if not isinstance(revision_id, str) or not revision_id.strip():
        raise ValueError(f'{label} must select a registered resource revision')
    return revision_id.strip()

def _resource_content_sha256(content: dict[str, Any]) -> str:
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()

def _redact_api_key_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: '[REDACTED]' if key.lower() == 'api_key' else _redact_api_key_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_api_key_values(item) for item in value]
    return value

def _normalize_target_mapping(filename: str, content: str) -> dict[str, str]:
    if not isinstance(filename, str) or not filename.strip().lower().endswith('.json'):
        raise ValueError('target mapping needs a JSON filename')
    if not isinstance(content, str) or len(content.encode('utf-8')) > 1048576:
        raise ValueError('target mapping content must be UTF-8 text up to 1 MiB')
    try:
        mapping = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('target mapping must be valid JSON') from exc
    if not isinstance(mapping, dict) or not mapping or len(mapping) > 200:
        raise ValueError('target mapping must be a non-empty category to MCP name object')
    if any((not isinstance(k, str) or not k.strip() or (not isinstance(v, str)) or (not v.strip()) for k, v in mapping.items())):
        raise ValueError('target mapping categories and MCP names must be non-empty strings')
    normalized = {key.strip(): value.strip() for key, value in mapping.items()}
    if len(normalized) != len(mapping):
        raise ValueError('target mapping has duplicate category names after trimming')
    return normalized

def _normalize_registered_resource(*, kind: str, filename: str, content: str, environment_type: str | None) -> dict[str, Any]:
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError('resource filename cannot be empty')
    if not isinstance(content, str):
        raise ValueError('resource content must be text')
    filename = filename.strip()
    if kind == 'posts':
        environment_type = (environment_type or 'moltnet_forum').strip()
        parsed = parse_post_selection_file(filename, content, environment_type=environment_type)
        summary = {'post_count': parsed['count'], 'comment_count': parsed['comment_count'], 'agent_count': parsed['agent_count'], 'identity_agent_count': parsed['identity_agent_count']}
    elif kind == 'queries':
        parsed = parse_query_file(filename, content)
        environment_type = None
        summary = {'query_count': int(parsed.get('query_count') or 0)}
    elif kind == 'mcp':
        parsed = parse_mcp_file(filename, content)
        environment_type = None
        summary = {'server_count': int(parsed.get('server_count') or 0), 'tool_count': int(parsed.get('tool_count') or 0)}
    else:
        raise ValueError('resource kind must be posts, queries or mcp')
    file_value = parsed['file'] if kind == 'posts' else {'filename': parsed['filename'], 'dataset': parsed['dataset']}
    return {'filename': file_value['filename'], 'environment_type': environment_type, 'file': file_value, 'content_sha256': _resource_content_sha256(file_value), 'summary': summary}

def _public_resource(resource: dict[str, Any], *, include_content: bool) -> dict[str, Any]:
    result = copy.deepcopy(resource)
    revisions = []
    for item in result.get('revisions') or []:
        revision = dict(item)
        if not include_content:
            revision.pop('content', None)
        revisions.append(revision)
    result['revisions'] = revisions
    if revisions:
        result['latest_revision'] = revisions[0]
    return result

def _tail_json_lines(path: Path, *, limit: int) -> list[dict[str, Any]]:
    text = _tail_text(path, max_bytes=96000)
    if not text:
        return []
    result: list[dict[str, Any]] = []
    for line in text.splitlines()[-limit:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result

def _tail_text(path: Path, *, max_bytes: int) -> str:
    if not path.is_file():
        return ''
    try:
        with path.open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ''
    return data.decode('utf-8', errors='replace')

def _validate_platform_model_selection(config: dict[str, Any]) -> None:
    """Enforce distinct first-stage memory and second-stage behavior models."""
    environment = config.get('environment')
    if isinstance(environment, dict) and environment.get('type') == 'tool_selection':
        backend = config.get('backend')
        selected = backend.get('model') if isinstance(backend, dict) else None
        if not isinstance(selected, str) or selected.strip() not in SELECTABLE_EXPERIMENT_MODELS:
            raise ValueError('backend.model must be one of: ' + ', '.join(SELECTABLE_EXPERIMENT_MODELS))
        return
    memory = config.get('memory')
    strategy = memory.get('strategy') if isinstance(memory, dict) else None
    selected = memory.get('model') if isinstance(memory, dict) else None
    if strategy in MEMORY_MODEL_STRATEGIES:
        if not isinstance(selected, str) or selected.strip() not in SELECTABLE_EXPERIMENT_MODELS:
            raise ValueError(f'memory.model is required for {strategy} and must be one of: ' + ', '.join(SELECTABLE_EXPERIMENT_MODELS))
        memory['model'] = selected.strip()
        return
    if selected is not None:
        raise ValueError('memory.model is only allowed for langmem_official and a_mem_official')

def _runner_type(config: dict[str, Any]) -> str:
    environment = config.get('environment') or {}
    if not isinstance(environment, dict):
        return 'unknown'
    return str(environment.get('type') or 'moltnet_thread')

def _new_run_id(experiment_id: str) -> str:
    stamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    return f'{experiment_id}-{stamp}-{secrets.token_hex(2)}'

def _screen_name(run_id: str) -> str:
    safe = re.sub('[^A-Za-z0-9_.-]+', '-', run_id).strip('-.')
    return f'ac-{safe}'[:76]

def _now() -> str:
    return datetime.now().astimezone().isoformat()

def _older_than(value: str, *, seconds: int) -> bool:
    try:
        created_at = datetime.fromisoformat(value)
    except ValueError:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.astimezone()
    return datetime.now().astimezone() - created_at > timedelta(seconds=seconds)
__all__ = ['ACTIVE_RUN_STATUSES', 'DEFAULT_EXPERIMENT_CONFIG', 'DEFAULT_PLATFORM_DB', 'ExperimentPlatform', 'JobLauncher', 'ProcessJobLauncher', 'ScreenJobLauncher']
