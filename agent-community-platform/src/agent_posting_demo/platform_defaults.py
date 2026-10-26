"""Editable defaults for newly created platform experiments."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
from typing import Any
import json
import re
from agent_posting_core.memory import create_memory
from agent_posting_core.memory_retrieval import merge_retrieval_options
from .validation_connection import normalize_backend_connection
from .experiment_config import SELECTABLE_EXPERIMENT_MODELS
FIXED_LOCAL_MEMORY_MODEL = 'deepseek-v4-flash'
FIXED_LOCAL_MEMORY_STRATEGIES = frozenset({'langmem_official', 'a_mem_official'})
DEFAULT_PLATFORM_SETTINGS: dict[str, Any] = {'version': 1, 'backend': {'concurrency': 10, 'timeout_seconds': 90, 'min_request_interval_seconds': 0.5, 'max_transient_retries': 8, 'retry_backoff_seconds': 2, 'max_retry_wait_seconds': 60, 'temperature': 0.2, 'max_tokens': 8000}, 'reading': {'hide_speaker': True, 'speaker_as_json': False, 'require_identity': True}, 'memory_profiles': {'langmem_official': {'model': FIXED_LOCAL_MEMORY_MODEL, 'options': {'query_limit': 5, 'max_steps': 1, 'embedding_model': 'all-MiniLM-L6-v2', 'embedding_dimensions': 384, 'enable_deletes': False, 'custom_instructions': ''}}, 'a_mem_official': {'model': FIXED_LOCAL_MEMORY_MODEL, 'options': {'evo_threshold': 100, 'response_tolerance_enabled': True, 'response_validation_max_attempts': 3}}, 'mem0_platform_v3': {'options': {'request_timeout_seconds': 30, 'event_timeout_seconds': 300, 'event_poll_interval_seconds': 1, 'max_transient_retries': 3, 'retry_backoff_seconds': 1, 'max_retry_wait_seconds': 30, 'custom_instructions': ''}}, 'hindsight_cloud': {'options': {'target_bank': '', 'request_timeout_seconds': 30, 'processing_timeout_seconds': 600, 'poll_interval_seconds': 2, 'max_read_retries': 3}}, 'memos_cloud': {'options': {'request_timeout_seconds': 30, 'processing_timeout_seconds': 600, 'poll_interval_seconds': 2, 'max_read_retries': 3}}}, 'validation': {'backend_connection': {'mode': 'inherit'}, 'model_connections': {'deepseek-v4-flash': {'mode': 'environment', 'api_base_env': 'AGENT_COMMUNITY_STAGE2_DEEPSEEK_API_BASE', 'api_key_env': 'AGENT_COMMUNITY_STAGE2_DEEPSEEK_API_KEY'}, 'deepseek-v4-pro': {'mode': 'environment', 'api_base_env': 'AGENT_COMMUNITY_STAGE2_DEEPSEEK_API_BASE', 'api_key_env': 'AGENT_COMMUNITY_STAGE2_DEEPSEEK_API_KEY'}, 'qwen3.8-max': {'mode': 'inherit'}, 'qwen3.8-flash': {'mode': 'inherit'}}, 'api_key_position': 'middle', 'retrieval_profiles': {'langmem_official': {'retrieval_top_k': 10}, 'a_mem_official': {'retrieval_top_k': 5}, 'mem0_platform_v3': {'retrieval_top_k': 10, 'search_threshold': 0.1, 'rerank': False}, 'hindsight_cloud': {'retrieval_top_k': 10, 'recall_budget': 'mid', 'recall_max_tokens': 4096, 'context_max_chars': 12000}, 'memos_cloud': {'retrieval_top_k': 10, 'context_max_chars': 12000}}}, 'output': {'root_dir_template': 'output/experiments/{experiment_id}', 'save_thread_after_each_round': True, 'save_memory_after_each_round': True}}
_BACKEND_RANGES = {'concurrency': (1, 100, int), 'timeout_seconds': (0.001, 3600, float), 'min_request_interval_seconds': (0, 3600, float), 'max_transient_retries': (0, 100, int), 'retry_backoff_seconds': (0.001, 3600, float), 'max_retry_wait_seconds': (0.001, 3600, float), 'temperature': (0, 2, float), 'max_tokens': (1, 1000000, int)}
_MEMORY_NUMERIC = {'query_limit': (1, 1000, int), 'max_steps': (1, 10, int), 'embedding_dimensions': (1, 65536, int), 'evo_threshold': (1, 10000, int), 'response_validation_max_attempts': (1, 100, int), 'request_timeout_seconds': (0.001, 3600, float), 'event_timeout_seconds': (0.001, 3600, float), 'event_poll_interval_seconds': (0.001, 3600, float), 'processing_timeout_seconds': (0.001, 86400, float), 'poll_interval_seconds': (0.001, 3600, float), 'max_transient_retries': (0, 100, int), 'max_read_retries': (0, 100, int), 'retry_backoff_seconds': (0.001, 3600, float), 'max_retry_wait_seconds': (0.001, 3600, float)}
_RETRIEVAL_NUMERIC = {'retrieval_top_k': (1, 1000, int), 'search_threshold': (0, 1, float), 'search_distance': (0, 1, float), 'recall_max_tokens': (1, 100000, int), 'context_max_chars': (1, 1000000, int), 'merged_max_items': (1, 10000, int)}
_API_KEY_POSITIONS = {'first', 'middle', 'last', 'message', 'query_suffix'}

def _keys(value: Any, expected: dict[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f"{label} must contain exactly: {', '.join(expected)}")
    return value

def _number(value: Any, limits: tuple[float, float, type], label: str) -> None:
    minimum, maximum, kind = limits
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label} must be a number')
    if kind is int and (not isinstance(value, int)):
        raise ValueError(f'{label} must be an integer')
    if not minimum <= value <= maximum:
        raise ValueError(f'{label} must be between {minimum} and {maximum}')

def validate_platform_settings(value: Any) -> dict[str, Any]:
    """Validate a complete settings document; no secrets or arbitrary keys."""
    template = DEFAULT_PLATFORM_SETTINGS
    if isinstance(value, dict):
        value = deepcopy(value)
        profiles = value.get('memory_profiles')
        if isinstance(profiles, dict) and 'memos_cloud' not in profiles:
            profiles['memos_cloud'] = deepcopy(template['memory_profiles']['memos_cloud'])
        amem = profiles.get('a_mem_official') if isinstance(profiles, dict) else None
        amem_options = amem.get('options') if isinstance(amem, dict) else None
        if isinstance(amem_options, dict):
            for key in ('response_tolerance_enabled', 'response_validation_max_attempts'):
                amem_options.setdefault(key, template['memory_profiles']['a_mem_official']['options'][key])
        validation = value.get('validation')
        retrieval = validation.get('retrieval_profiles') if isinstance(validation, dict) else None
        if isinstance(retrieval, dict) and 'memos_cloud' not in retrieval:
            retrieval['memos_cloud'] = deepcopy(template['validation']['retrieval_profiles']['memos_cloud'])
    if isinstance(value, dict) and isinstance(value.get('reading'), dict) and ('speaker_as_json' not in value['reading']):
        value = deepcopy(value)
        value['reading']['speaker_as_json'] = False
    if isinstance(value, dict) and isinstance(value.get('validation'), dict) and ('model_connections' not in value['validation'] or isinstance(value['validation']['model_connections'], dict)):
        value = deepcopy(value)
        connections = deepcopy(template['validation']['model_connections'])
        connections.update(value['validation'].get('model_connections', {}))
        value['validation']['model_connections'] = connections
    value = _keys(value, template, 'settings')
    if value['version'] != 1:
        raise ValueError('settings.version must be 1')
    backend = _keys(value['backend'], template['backend'], 'settings.backend')
    for key, limits in _BACKEND_RANGES.items():
        _number(backend[key], limits, f'settings.backend.{key}')
    reading = _keys(value['reading'], template['reading'], 'settings.reading')
    for key, setting in reading.items():
        if not isinstance(setting, bool):
            raise ValueError(f'settings.reading.{key} must be true or false')
    profiles = _keys(value['memory_profiles'], template['memory_profiles'], 'settings.memory_profiles')
    for strategy, profile in profiles.items():
        expected = template['memory_profiles'][strategy]
        _keys(profile, expected, f'settings.memory_profiles.{strategy}')
        if 'model' in expected and profile['model'] not in SELECTABLE_EXPERIMENT_MODELS:
            raise ValueError(f'settings.memory_profiles.{strategy}.model must be a supported model')
        options = _keys(profile['options'], expected['options'], f'settings.memory_profiles.{strategy}.options')
        for key, setting in options.items():
            label = f'settings.memory_profiles.{strategy}.options.{key}'
            if key in _MEMORY_NUMERIC:
                _number(setting, _MEMORY_NUMERIC[key], label)
            elif isinstance(expected['options'][key], bool):
                if not isinstance(setting, bool):
                    raise ValueError(f'{label} must be true or false')
            elif not isinstance(setting, str) or len(setting) > 10000:
                raise ValueError(f'{label} must be a string of at most 10000 characters')
        create_memory(strategy, agent_id='settings-validation', options=options)
    validation = _keys(value['validation'], template['validation'], 'settings.validation')
    connection = normalize_backend_connection(validation['backend_connection'])
    model_connections = _keys(validation['model_connections'], template['validation']['model_connections'], 'settings.validation.model_connections')
    normalized_model_connections = {model: normalize_backend_connection(model_connections[model]) for model in model_connections}
    if validation['api_key_position'] not in _API_KEY_POSITIONS:
        raise ValueError('settings.validation.api_key_position is invalid')
    retrieval = _keys(validation['retrieval_profiles'], template['validation']['retrieval_profiles'], 'settings.validation.retrieval_profiles')
    for strategy, options in retrieval.items():
        expected = template['validation']['retrieval_profiles'][strategy]
        _keys(options, expected, f'settings.validation.retrieval_profiles.{strategy}')
        for key, setting in options.items():
            label = f'settings.validation.retrieval_profiles.{strategy}.{key}'
            if key in _RETRIEVAL_NUMERIC:
                _number(setting, _RETRIEVAL_NUMERIC[key], label)
            elif key in {'rerank', 'rewrite_query', 'aggregate'}:
                if not isinstance(setting, bool):
                    raise ValueError(f'{label} must be true or false')
            elif key == 'recall_budget' and setting not in {'low', 'mid', 'high'}:
                raise ValueError(f'{label} must be low, mid, or high')
        merge_retrieval_options(strategy, {}, options)
    output = _keys(value['output'], template['output'], 'settings.output')
    path_template = output['root_dir_template']
    if not isinstance(path_template, str) or not re.fullmatch('output/[A-Za-z0-9_./{}-]+', path_template) or path_template.count('{experiment_id}') != 1 or ('{' in path_template.replace('{experiment_id}', '')) or ('}' in path_template.replace('{experiment_id}', '')) or ('..' in path_template):
        raise ValueError('settings.output.root_dir_template must be inside output/ and contain {experiment_id} once')
    for key in ('save_thread_after_each_round', 'save_memory_after_each_round'):
        if not isinstance(output[key], bool):
            raise ValueError(f'settings.output.{key} must be true or false')
    result = deepcopy(value)
    for strategy in FIXED_LOCAL_MEMORY_STRATEGIES:
        result['memory_profiles'][strategy]['model'] = FIXED_LOCAL_MEMORY_MODEL
    result['validation']['backend_connection'] = connection
    result['validation']['model_connections'] = normalized_model_connections
    return result

def validation_connection_for_model(settings: dict[str, Any], model: str) -> dict[str, str]:
    """Resolve the saved default for one new second-stage model configuration."""
    validation = settings['validation']
    selected = validation['model_connections'].get(model)
    if selected and selected['mode'] != 'inherit':
        return deepcopy(selected)
    return deepcopy(validation['backend_connection'])

def read_platform_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return deepcopy(DEFAULT_PLATFORM_SETTINGS)
    value = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(value, dict):
        profiles = value.get('memory_profiles')
        if isinstance(profiles, dict):
            profiles.pop('graphmem', None)
        validation = value.get('validation')
        retrieval = validation.get('retrieval_profiles') if isinstance(validation, dict) else None
        if isinstance(retrieval, dict):
            retrieval.pop('graphmem', None)
    return validate_platform_settings(value)

def write_platform_settings(path: Path, value: Any) -> dict[str, Any]:
    validated = validate_platform_settings(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(validated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    return validated

def output_root(settings: dict[str, Any], experiment_id: str) -> str:
    return settings['output']['root_dir_template'].replace('{experiment_id}', experiment_id)
