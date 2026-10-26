from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from agent_posting_core.llm import DEFAULT_LLM_TIMEOUT_SECONDS, DEFAULT_MAX_RETRY_WAIT_SECONDS, DEFAULT_MAX_TRANSIENT_RETRIES, DEFAULT_RETRY_BACKOFF_SECONDS
from agent_posting_core.cloud_memory import CLOUD_STRATEGIES
from agent_posting_core.memory import DEFAULT_MEMORY_STRATEGY, create_memory, registered_memory_strategies
from agent_posting_core.post_selection_file import normalize_post_dataset, select_file_reader
from .config import AppConfig, PROJECT_ROOT, read_env_file
SELECTABLE_EXPERIMENT_MODELS = ('gpt-5.6-luna', 'gpt-5.6-terra', 'deepseek-v4-flash', 'deepseek-v4-pro', 'qwen3.8-max', 'qwen3.8-flash')
MEMORY_PREVIEW_STRATEGY = 'agent_memory_preview'
MEMORY_MODEL_STRATEGIES = frozenset({'langmem_official', 'a_mem_official', MEMORY_PREVIEW_STRATEGY})

@dataclass(frozen=True)
class RunSettings:
    name: str
    run_id: str | None
    seed: int

@dataclass(frozen=True)
class BackendSettings:
    api_base: str
    api_key_env: str
    api_key: str = field(repr=False)
    mem0_api_key_env: str
    mem0_api_key: str = field(repr=False)
    model: str
    concurrency: int
    timeout_seconds: float
    temperature: float
    max_tokens: int
    max_transient_retries: int
    retry_backoff_seconds: float
    max_retry_wait_seconds: float
    min_request_interval_seconds: float
    cloud_api_keys: dict[str, str] = field(default_factory=dict, repr=False)

@dataclass(frozen=True)
class DataSettings:
    dataset_dir: Path
    source_db: Path
    version: str
    build_if_missing: bool

@dataclass(frozen=True)
class EnvironmentSettings:
    kind: str
    selection: str
    source_post_id: str | None
    submolt: str | None
    source_post_ids: tuple[str, ...]
    post_count: int | None
    submolts: tuple[str, ...]
    min_comments: int
    max_comments: int | None
    feed_limit: int
    imported_file: dict[str, Any] | None = None
    hide_speaker: bool = True
    speaker_as_json: bool = False

@dataclass(frozen=True)
class AgentSettings:
    count: int
    require_identity: bool
    prefer_thread_participants: bool
    seed: int
    history_limit: int

@dataclass(frozen=True)
class MemorySettings:
    strategy: str
    options: dict[str, Any]
    model: str | None = None

@dataclass(frozen=True)
class AgentActivationSettings:
    mode: str
    agents_per_round: int | None

@dataclass(frozen=True)
class SimulationSettings:
    rounds: int
    activation: AgentActivationSettings
    round_interval_seconds: float
    continue_on_agent_error: bool
    max_consecutive_all_failed_rounds: int | None
    tools: tuple[str, ...]
    mode: str = 'read_only'
    read_only_agent_id: str | None = None

@dataclass(frozen=True)
class OutputSettings:
    root_dir: Path
    save_thread_after_each_round: bool
    save_memory_after_each_round: bool

@dataclass(frozen=True)
class WebSettings:
    enabled: bool
    host: str
    port: int
    keep_alive_after_run: bool
    poll_interval_ms: int

@dataclass(frozen=True)
class ExperimentConfig:
    config_path: Path
    run: RunSettings
    backend: BackendSettings
    data: DataSettings
    environment: EnvironmentSettings
    agents: AgentSettings
    memory: MemorySettings
    simulation: SimulationSettings
    output: OutputSettings
    web: WebSettings
    validation: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        value = {'config_path': str(self.config_path), 'run': {'name': self.run.name, 'run_id': self.run.run_id, 'seed': self.run.seed}, 'backend': {'api_base': self.backend.api_base, 'api_key_env': self.backend.api_key_env, 'api_key_configured': bool(self.backend.api_key), 'mem0_api_key_env': self.backend.mem0_api_key_env, 'mem0_api_key_configured': bool(self.backend.mem0_api_key), 'model': self.backend.model, 'concurrency': self.backend.concurrency, 'timeout_seconds': self.backend.timeout_seconds, 'temperature': self.backend.temperature, 'max_tokens': self.backend.max_tokens, 'max_transient_retries': self.backend.max_transient_retries, 'retry_backoff_seconds': self.backend.retry_backoff_seconds, 'max_retry_wait_seconds': self.backend.max_retry_wait_seconds, 'min_request_interval_seconds': self.backend.min_request_interval_seconds}, 'environment': self._public_environment(), 'agents': {'count': self.agents.count, 'require_identity': self.agents.require_identity, 'prefer_thread_participants': self.agents.prefer_thread_participants, 'seed': self.agents.seed, 'history_limit': self.agents.history_limit}, 'memory': {'strategy': self.memory.strategy, 'options': self.memory.options, **({'model': self.memory.model} if self.memory.model is not None else {})}, 'simulation': {'rounds': self.simulation.rounds}, 'output': {'root_dir': str(self.output.root_dir), 'save_thread_after_each_round': self.output.save_thread_after_each_round, 'save_memory_after_each_round': self.output.save_memory_after_each_round}, 'web': {'enabled': self.web.enabled, 'host': self.web.host, 'port': self.web.port, 'keep_alive_after_run': self.web.keep_alive_after_run, 'poll_interval_ms': self.web.poll_interval_ms}}
        if self.validation is not None:
            value['validation'] = self.validation
        else:
            value['environment']['hide_speaker'] = self.environment.hide_speaker
            value['environment']['speaker_as_json'] = self.environment.speaker_as_json
            value['simulation']['mode'] = self.simulation.mode
            if self.simulation.read_only_agent_id is not None:
                value['simulation']['read_only_agent_id'] = self.simulation.read_only_agent_id
        return value

    def _public_environment(self) -> dict[str, Any]:
        if self.environment.kind == 'tool_selection':
            return {'type': 'tool_selection'}
        active = 'thread' if self.environment.kind == 'moltnet_thread' else 'forum'
        return {'type': self.environment.kind, active: {'selection': 'file'}, 'file': self.environment.imported_file}

def load_experiment_config(path: Path, *, project_root: Path=PROJECT_ROOT) -> ExperimentConfig:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError('Experiment configuration requires PyYAML; install it with `python -m pip install PyYAML`.') from exc
    config_path = path.resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f'Experiment config does not exist: {config_path}')
    raw = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError('experiment config root must be an object')
    _reject_unknown(raw, {'version', 'run', 'backend', 'data', 'environment', 'agents', 'memory', 'simulation', 'output', 'web', 'validation'}, 'root')
    version = _integer(raw.get('version', 1), 'version', minimum=1)
    if version != 1:
        raise ValueError(f'unsupported experiment config version: {version}')
    run_data = _section(raw, 'run', {'name', 'id', 'seed'})
    run_seed = _integer(run_data.get('seed', 0), 'run.seed')
    run = RunSettings(name=_nonempty(run_data.get('name', 'moltnet-demo'), 'run.name'), run_id=_optional_text(run_data.get('id')), seed=run_seed)
    backend_data = _section(raw, 'backend', {'api_base', 'api_base_env', 'api_key_env', 'mem0_api_key_env', 'model', 'model_env', 'concurrency', 'timeout_seconds', 'temperature', 'max_tokens', 'max_transient_retries', 'retry_backoff_seconds', 'max_retry_wait_seconds', 'min_request_interval_seconds'})
    if isinstance(raw.get('environment'), dict) and raw['environment'].get('type') == 'tool_selection':
        from .validation_connection import apply_backend_connection
        validation_data = _mapping(raw.get('validation', {}), 'validation')
        backend_data = apply_backend_connection(backend_data, validation_data.get('backend_connection'))
    api_key_env = _nonempty(backend_data.get('api_key_env', 'AGENT_COMMUNITY_API_KEY'), 'backend.api_key_env')
    env_file = read_env_file(project_root / '.env')
    api_key = os.environ.get(api_key_env) or env_file.get(api_key_env) or ''
    mem0_api_key_env = _nonempty(backend_data.get('mem0_api_key_env', 'MEM0_API_KEY'), 'backend.mem0_api_key_env')
    mem0_api_key = os.environ.get(mem0_api_key_env) or env_file.get(mem0_api_key_env) or ''
    api_base = _backend_value(backend_data, value_name='api_base', env_name='api_base_env', default_env='AGENT_COMMUNITY_API_BASE', env_file=env_file)
    model = _backend_model(backend_data, env_file=env_file, required=_configuration_requires_backend_model(raw))
    backend = BackendSettings(api_base=api_base.rstrip('/'), api_key_env=api_key_env, api_key=api_key, mem0_api_key_env=mem0_api_key_env, mem0_api_key=mem0_api_key, cloud_api_keys={service + '_cloud': os.environ.get(service.upper() + '_API_KEY') or env_file.get(service.upper() + '_API_KEY') or '' for service in ('hindsight', 'memos')}, model=model, concurrency=_integer(backend_data.get('concurrency', 1), 'backend.concurrency', minimum=1), timeout_seconds=_number(backend_data.get('timeout_seconds', DEFAULT_LLM_TIMEOUT_SECONDS), 'backend.timeout_seconds', minimum=0.001), temperature=_number(backend_data.get('temperature', 0.2), 'backend.temperature'), max_tokens=_integer(backend_data.get('max_tokens', 800), 'backend.max_tokens', minimum=1), max_transient_retries=_integer(backend_data.get('max_transient_retries', DEFAULT_MAX_TRANSIENT_RETRIES), 'backend.max_transient_retries', minimum=0), retry_backoff_seconds=_number(backend_data.get('retry_backoff_seconds', DEFAULT_RETRY_BACKOFF_SECONDS), 'backend.retry_backoff_seconds', minimum=0.001), max_retry_wait_seconds=_number(backend_data.get('max_retry_wait_seconds', DEFAULT_MAX_RETRY_WAIT_SECONDS), 'backend.max_retry_wait_seconds', minimum=0.001), min_request_interval_seconds=_number(backend_data.get('min_request_interval_seconds', 0), 'backend.min_request_interval_seconds', minimum=0))
    environment_value = raw.get('environment') or {}
    if isinstance(environment_value, dict) and environment_value.get('type') == 'tool_selection':
        from .validation_config import load_tool_selection_config
        return load_tool_selection_config(raw, config_path=config_path, project_root=project_root, run=run, backend=backend)
    if 'validation' in raw:
        raise ValueError('validation is only allowed for environment.type: tool_selection')
    data = DataSettings(project_root / 'data', project_root / 'data/unused.sqlite', 'unused', False)
    environment_data = _section(raw, 'environment', {'type', 'thread', 'forum', 'file', 'hide_speaker', 'speaker_as_json'})
    environment_type = _choice(environment_data.get('type', 'moltnet_thread'), 'environment.type', {'moltnet_thread', 'moltnet_forum'})
    active_key = 'forum' if environment_type == 'moltnet_forum' else 'thread'
    inactive_key = 'thread' if active_key == 'forum' else 'forum'
    if inactive_key in environment_data:
        raise ValueError(f'environment.{inactive_key} must be omitted for {environment_type}')
    active_data = _mapping(environment_data.get(active_key, {}), f'environment.{active_key}')
    if active_data.get('selection', 'file') != 'file':
        raise ValueError('first-stage experiments require file selection; database selection was removed')
    _reject_unknown(active_data, {'selection', 'feed_limit'}, f'environment.{active_key}')
    imported_file = _mapping(environment_data.get('file'), 'environment.file')
    _reject_unknown(imported_file, {'filename', 'dataset'}, 'environment.file')
    filename = _nonempty(imported_file.get('filename'), 'environment.file.filename')
    dataset = normalize_post_dataset(imported_file.get('dataset'), environment_type=environment_type)
    environment = EnvironmentSettings(kind=environment_type, selection='file', source_post_id=None, submolt=None, source_post_ids=(), post_count=None, submolts=(), min_comments=0, max_comments=None, feed_limit=len(dataset['posts']), imported_file={'filename': filename, 'dataset': dataset}, hide_speaker=_boolean(environment_data.get('hide_speaker', True), 'environment.hide_speaker'), speaker_as_json=_boolean(environment_data.get('speaker_as_json', False), 'environment.speaker_as_json'))
    simulation_data = _mapping(raw.get('simulation', {}), 'simulation')
    if simulation_data.get('mode', 'read_only') != 'read_only':
        raise ValueError('first-stage experiments support only read_only; automatic interaction was removed')
    reader_id = _optional_text(simulation_data.get('read_only_agent_id'))
    if not reader_id:
        raise ValueError('simulation.read_only_agent_id is required in read_only mode')
    agent_data = _section(raw, 'agents', {'count', 'require_identity', 'prefer_thread_participants', 'seed', 'history_limit'})
    if _integer(agent_data.get('count', 1), 'agents.count', minimum=1) != 1:
        raise ValueError('read_only mode requires agents.count: 1')
    agents = AgentSettings(count=1, require_identity=_boolean(agent_data.get('require_identity', True), 'agents.require_identity'), prefer_thread_participants=False, seed=_integer(agent_data.get('seed', run_seed), 'agents.seed'), history_limit=_integer(agent_data.get('history_limit', 100), 'agents.history_limit', minimum=1))
    select_file_reader(dataset, agent_id=reader_id, require_identity=agents.require_identity)
    memory_data = _section(raw, 'memory', {'strategy', 'model', 'options'})
    memory_strategy = _choice(memory_data.get('strategy', DEFAULT_MEMORY_STRATEGY), 'memory.strategy', {*registered_memory_strategies(), MEMORY_PREVIEW_STRATEGY})
    configured_memory_model = memory_data.get('model')
    if configured_memory_model is not None:
        memory_model = _choice(configured_memory_model, 'memory.model', set(SELECTABLE_EXPERIMENT_MODELS))
        if memory_strategy not in MEMORY_MODEL_STRATEGIES:
            raise ValueError('memory.model is only allowed for langmem_official and a_mem_official or agent_memory_preview')
    elif memory_strategy in MEMORY_MODEL_STRATEGIES:
        memory_model = backend.model or None
        if memory_model is None:
            raise ValueError(f'memory.model is required for {memory_strategy}; select one of: ' + ', '.join(SELECTABLE_EXPERIMENT_MODELS))
    else:
        memory_model = None
    memory = MemorySettings(strategy=memory_strategy, options=dict(_mapping(memory_data.get('options', {}), 'memory.options')), model=memory_model)
    try:
        if memory.strategy == MEMORY_PREVIEW_STRATEGY:
            if memory.options:
                raise ValueError('agent_memory_preview does not accept memory backend options')
        else:
            create_memory(memory.strategy, agent_id='config-validation', options=memory.options)
    except ValueError as exc:
        raise ValueError(f'invalid {memory.strategy} memory options: {exc}') from exc
    simulation = SimulationSettings(mode='read_only', read_only_agent_id=reader_id, rounds=1, activation=AgentActivationSettings(mode='all', agents_per_round=None), round_interval_seconds=0, continue_on_agent_error=False, max_consecutive_all_failed_rounds=None, tools=())
    output_data = _section(raw, 'output', {'root_dir', 'save_thread_after_each_round', 'save_memory_after_each_round'})
    output = OutputSettings(root_dir=_project_path(output_data.get('root_dir', 'output/demo'), project_root, 'output.root_dir'), save_thread_after_each_round=_boolean(output_data.get('save_thread_after_each_round', True), 'output.save_thread_after_each_round'), save_memory_after_each_round=_boolean(output_data.get('save_memory_after_each_round', True), 'output.save_memory_after_each_round'))
    web_data = _section(raw, 'web', {'enabled', 'host', 'port', 'keep_alive_after_run', 'poll_interval_ms'})
    web = WebSettings(enabled=_boolean(web_data.get('enabled', True), 'web.enabled'), host=_nonempty(web_data.get('host', '127.0.0.1'), 'web.host'), port=_integer(web_data.get('port', 8765), 'web.port', minimum=1), keep_alive_after_run=_boolean(web_data.get('keep_alive_after_run', True), 'web.keep_alive_after_run'), poll_interval_ms=_integer(web_data.get('poll_interval_ms', 1000), 'web.poll_interval_ms', minimum=100))
    return ExperimentConfig(config_path=config_path, run=run, backend=backend, data=data, environment=environment, agents=agents, memory=memory, simulation=simulation, output=output, web=web)

def experiment_app_config(config: ExperimentConfig, *, run_dir: Path, db_path: Path) -> AppConfig:
    return AppConfig(api_base=config.backend.api_base, model=config.backend.model, api_key=config.backend.api_key, output_dir=run_dir, tmp_dir=run_dir / 'tmp', db_path=db_path, llm_concurrency=config.backend.concurrency, llm_timeout_seconds=config.backend.timeout_seconds, llm_temperature=config.backend.temperature, llm_max_tokens=config.backend.max_tokens)

def _section(root: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    value = _mapping(root.get(name, {}), name)
    _reject_unknown(value, allowed, name)
    return value

def _backend_value(backend_data: dict[str, Any], *, value_name: str, env_name: str, default_env: str, env_file: dict[str, str]) -> str:
    direct_value = backend_data.get(value_name)
    configured_env = backend_data.get(env_name)
    if direct_value is not None and configured_env is not None:
        raise ValueError(f'backend.{value_name} and backend.{env_name} cannot both be set')
    if direct_value is not None:
        return _nonempty(direct_value, f'backend.{value_name}')
    variable = _nonempty(configured_env or default_env, f'backend.{env_name}')
    value = os.environ.get(variable) or env_file.get(variable)
    if not value:
        raise ValueError(f'backend.{value_name} is not configured; set environment variable {variable}')
    return _nonempty(value, f'environment variable {variable}')

def _backend_model(backend_data: dict[str, Any], *, env_file: dict[str, str], required: bool) -> str:
    """Resolve the behavior model, which remains separate from memory.model."""
    direct_value = backend_data.get('model')
    configured_env = backend_data.get('model_env')
    if direct_value is not None and configured_env is not None:
        raise ValueError('backend.model and backend.model_env cannot both be set')
    if direct_value is not None:
        return _nonempty(direct_value, 'backend.model')
    variable = _nonempty(configured_env or 'AGENT_COMMUNITY_MODEL', 'backend.model_env')
    value = os.environ.get(variable) or env_file.get(variable)
    if value:
        return _nonempty(value, f'environment variable {variable}')
    if required:
        raise ValueError(f'backend.model is not configured; set environment variable {variable}')
    return ''

def _configuration_requires_backend_model(raw: dict[str, Any]) -> bool:
    environment = raw.get('environment')
    if isinstance(environment, dict) and environment.get('type') == 'tool_selection':
        return True
    memory = raw.get('memory')
    strategy = memory.get('strategy', DEFAULT_MEMORY_STRATEGY) if isinstance(memory, dict) else DEFAULT_MEMORY_STRATEGY
    if strategy in MEMORY_MODEL_STRATEGIES:
        selected = memory.get('model') if isinstance(memory, dict) else None
        return not isinstance(selected, str) or not selected.strip()
    return strategy not in {'mem0_platform_v3', *CLOUD_STRATEGIES}

def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f'{name} must be an object')
    return value

def _reject_unknown(value: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown {name} config fields: {', '.join(unknown)}")

def _nonempty(value: Any, name: str) -> str:
    text = '' if value is None else str(value).strip()
    if not text:
        raise ValueError(f'{name} cannot be empty')
    return text

def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

def _integer(value: Any, name: str, minimum: int | None=None) -> int:
    if isinstance(value, bool):
        raise ValueError(f'{name} must be an integer')
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be an integer') from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f'{name} must be at least {minimum}')
    return parsed

def _optional_integer(value: Any, name: str, minimum: int | None=None) -> int | None:
    if value is None:
        return None
    return _integer(value, name, minimum)

def _number(value: Any, name: str, minimum: float | None=None) -> float:
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a number')
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be a number') from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f'{name} must be at least {minimum}')
    return parsed

def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f'{name} must be true or false')
    return value

def _choice(value: Any, name: str, choices: set[str]) -> str:
    parsed = _nonempty(value, name)
    if parsed not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return parsed

def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f'{name} must be a non-empty array')
    result = tuple((_nonempty(item, name) for item in value))
    if len(result) != len(set(result)):
        raise ValueError(f'{name} cannot contain duplicates')
    return result

def _optional_string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f'{name} must be an array')
    result = tuple(dict.fromkeys((_nonempty(item, name) for item in value)))
    return result

def _project_path(value: Any, root: Path, name: str) -> Path:
    path = Path(_nonempty(value, name)).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()
__all__ = ['AgentActivationSettings', 'AgentSettings', 'BackendSettings', 'DataSettings', 'EnvironmentSettings', 'ExperimentConfig', 'MemorySettings', 'OutputSettings', 'RunSettings', 'SELECTABLE_EXPERIMENT_MODELS', 'MEMORY_MODEL_STRATEGIES', 'SimulationSettings', 'WebSettings', 'experiment_app_config', 'load_experiment_config']
