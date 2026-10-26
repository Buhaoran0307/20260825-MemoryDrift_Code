"""Read-only retrieval policies that may vary over a frozen memory snapshot."""
from __future__ import annotations
import copy
from typing import Any
from .memory import Memory, MemorySnapshot, create_memory
_RETRIEVAL_OPTIONS = {'a_mem_official': ('retrieval_top_k',), 'langmem_official': ('retrieval_top_k',), 'mem0_platform_v3': ('retrieval_top_k', 'search_threshold', 'rerank'), 'hindsight_cloud': ('recall_budget', 'recall_max_tokens', 'retrieval_top_k', 'context_max_chars'), 'memos_cloud': ('retrieval_top_k', 'search_threshold', 'context_max_chars')}

def retrieval_option_names(strategy: str) -> tuple[str, ...]:
    """Return only options used at read time, never write-time search settings."""
    normalized = strategy.strip().lower().replace('-', '_')
    return _RETRIEVAL_OPTIONS.get(normalized, ())

def _canonical_options(strategy: str, options: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(options)
    if strategy.strip().lower().replace('-', '_') == 'hindsight_cloud':
        result.pop('min_final_score', None)
    if 'retrieval_top_k' in retrieval_option_names(strategy) and 'top_k' in result:
        if 'retrieval_top_k' in result:
            raise ValueError('memory options cannot contain both retrieval_top_k and top_k')
        result['retrieval_top_k'] = result.pop('top_k')
    return result

def merge_retrieval_options(strategy: str, source_options: dict[str, Any], requested_options: dict[str, Any]) -> dict[str, Any]:
    """Merge read overrides while preserving every frozen write-time option.

    Partial requests are supported. Full saved memory settings are accepted as
    long as their non-retrieval values still match the source. The legacy top_k
    alias is normalized before merging so it cannot conflict with a new value.
    """
    source = _canonical_options(strategy, source_options)
    requested = _canonical_options(strategy, requested_options)
    allowed = retrieval_option_names(strategy)
    effective = copy.deepcopy(source)
    for name, value in requested.items():
        if name in allowed:
            effective[name] = value
        elif name not in source or source[name] != value:
            raise ValueError(f'second-stage memory settings may change only retrieval options; {strategy} {name} must match the frozen source')
    create_memory(strategy, agent_id='retrieval-settings-validation', options=effective)
    return effective

def get_retrieval_settings(strategy: str, options: dict[str, Any]) -> dict[str, Any]:
    """Describe effective read settings, including constructor defaults."""
    memory = create_memory(strategy, agent_id='retrieval-settings-description', options=_canonical_options(strategy, options))
    return {name: copy.deepcopy(getattr(memory, name)) for name in retrieval_option_names(memory.strategy)}

def apply_retrieval_settings_to_snapshot(snapshot: MemorySnapshot, *, memory: Memory) -> MemorySnapshot:
    """Adapt a private snapshot copy to an already validated read policy.

    Adapter restore checks remain strict. Only runtime read options are changed;
    recorded events, records, vectors, provider scope and write settings retain
    the frozen source values. Use the constructed instance to resolve aliases
    and option types exactly as the adapter does.
    """
    if snapshot.strategy != memory.strategy:
        raise ValueError('retrieval memory strategy must match the frozen source')
    options = copy.deepcopy(snapshot.options)
    if memory.strategy == 'hindsight_cloud' and memory.min_final_score is None:
        options.pop('min_final_score', None)
    for name in retrieval_option_names(memory.strategy):
        if memory.strategy == 'hindsight_cloud' and name == 'retrieval_top_k' and (memory.retrieval_top_k == 10):
            options.pop(name, None)
            continue
        options[name] = copy.deepcopy(getattr(memory, name))
    return MemorySnapshot(strategy=snapshot.strategy, revision=snapshot.revision, events=copy.deepcopy(snapshot.events), options=options)
__all__ = ['apply_retrieval_settings_to_snapshot', 'get_retrieval_settings', 'merge_retrieval_options', 'retrieval_option_names']
