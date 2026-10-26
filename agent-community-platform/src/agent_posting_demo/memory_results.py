"""Read-only presentation of frozen stage-one memory artifacts."""
from __future__ import annotations
from typing import Any
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import json
import threading
from urllib.parse import quote

class TracingLLMClient:
    """Persist the actual visible output of every local stage-one model call."""

    def __init__(self, client: Any, path: Path) -> None:
        self.client = client
        self.path = path
        self.manager = getattr(client, 'manager', None)
        self._lock = threading.Lock()

    async def complete(self, request: Any) -> Any:
        record: dict[str, Any] = {'at': datetime.now(timezone.utc).isoformat(), 'agent_id': request.agent_id, 'purpose': request.purpose, 'request': asdict(request)}
        try:
            response = await self.client.complete(request)
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            self._append(record)
            raise
        record.update({'response_content': response.content, 'reasoning_content': response.reasoning_content, 'raw_response': response.raw, 'tool_calls': [asdict(call) for call in response.tool_calls]})
        self._append(record)
        return response

    def _append(self, record: dict[str, Any]) -> None:
        with self._lock:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')

def inventory(snapshot: dict[str, Any], exported: dict[str, Any] | None=None) -> list[dict[str, Any]]:
    strategy = snapshot.get('strategy')
    options = snapshot.get('options') or {}
    events = {event.get('event_id'): event for event in snapshot.get('events') or []}
    rows: list[dict[str, Any]] = []

    def add(identifier: Any, content: Any, source_ids: Any=None, **details: Any) -> None:
        ids = source_ids if isinstance(source_ids, list) else [source_ids] if source_ids else []
        rows.append({'id': identifier, 'content': content, 'sources': [events[item] for item in ids if item in events], 'unresolved_source_ids': [item for item in ids if item not in events], 'history': details.pop('history', []), **details})
    if strategy == 'langmem_official':
        for item in options.get('memories') or []:
            value = item.get('value') or {}
            content = value.get('content') or {}
            add(item.get('key'), content.get('important_fact') or content.get('content') or content, kind=value.get('kind'), details=content, created_at=item.get('created_at'), updated_at=item.get('updated_at'))
    elif strategy == 'a_mem_official':
        for item in options.get('records') or []:
            note = item.get('note') or {}
            atomic_note = {**note, 'embedding': item.get('embedding')}
            add(note.get('id'), note.get('content'), item.get('source_event_id'), kind='atomic_note', details={key: value for key, value in note.items() if key not in {'id', 'content', 'evolution_history'}}, history=note.get('evolution_history') or [], atomic_note=atomic_note)
    elif exported:
        for item in exported.get('items') or []:
            if isinstance(item, dict):
                source_ids = item.get('source_event_ids') or item.get('source_memory_ids') or []
                add(item.get('id') or item.get('memory_id'), item.get('memory') or item.get('content') or item.get('text') or item.get('summary'), source_ids, kind=exported.get('kind', 'remote_memory'), details=item, history=item.get('history') if isinstance(item.get('history'), list) else [item['history']] if item.get('history') else [])
    return rows

def langmem_versions(process_dir: Path, agent_id: str) -> dict[str, list[dict[str, Any]]]:
    """Record observed versions; a checkpoint event is temporal context, not proven provenance."""
    versions: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(process_dir.glob('memory-event-*.json')):
        try:
            checkpoint = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if checkpoint.get('agent_id') != agent_id:
            continue
        snapshot = checkpoint.get('snapshot') or {}
        if snapshot.get('strategy') != 'langmem_official':
            continue
        event_id = (checkpoint.get('source_event') or {}).get('event_id')
        for row in (snapshot.get('options') or {}).get('memories') or []:
            key = row.get('key')
            if not isinstance(key, str):
                continue
            trail = versions.setdefault(key, [])
            if trail and trail[-1]['value'] == row.get('value'):
                continue
            trail.append({'value': row.get('value'), 'saved_at': checkpoint.get('saved_at'), 'change_observed_after_event': event_id})
    return {key: trail[:-1] for key, trail in versions.items() if len(trail) > 1}

def amem_versions(process_dir: Path, agent_id: str) -> dict[str, list[dict[str, Any]]]:
    """Find older observed note states; the upstream evolution_history is not populated."""
    versions: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(process_dir.glob('memory-event-*.json')):
        try:
            checkpoint = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if checkpoint.get('agent_id') != agent_id:
            continue
        snapshot = checkpoint.get('snapshot') or {}
        if snapshot.get('strategy') != 'a_mem_official':
            continue
        event_id = (checkpoint.get('source_event') or {}).get('event_id')
        for record in (snapshot.get('options') or {}).get('records') or []:
            note = record.get('note') or {}
            key = note.get('id')
            if not isinstance(key, str):
                continue
            trail = versions.setdefault(key, [])
            if trail and trail[-1]['note'] == note:
                continue
            trail.append({'note': note, 'saved_at': checkpoint.get('saved_at'), 'change_observed_after_event': event_id})
    return {key: trail[:-1] for key, trail in versions.items() if len(trail) > 1}

async def export_remote_memories(memory: Any) -> dict[str, Any]:
    """Freeze provider list responses after ingestion; never perform a search as export."""
    strategy = memory.strategy
    if strategy == 'memos_cloud':
        from .memory_transfer import memos_snapshot_package
        provider_snapshot = (memory.snapshot().options or {}).get('provider_snapshot')
        if not isinstance(provider_snapshot, dict) or provider_snapshot.get('complete') is not True:
            raise ValueError('MemOS export requires completed full native memory readback')
        return {**memos_snapshot_package(provider_snapshot, memory._user_id()), 'strategy': strategy, 'completeness': 'all_six_native_memory_categories'}
    items: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    for page in range(1, 1001):
        if strategy == 'mem0_platform_v3':
            response = await memory._request_read('POST', f'/v3/memories/?page={page}&page_size=100', {'filters': {'user_id': memory._require_scope()}})
            batch = response.get('results')
            has_more = bool(response.get('next'))
            kind, completeness = ('remote_memory', 'provider_list')
        elif strategy == 'hindsight_cloud':
            response = await memory._request('GET', memory._bank() + f'/memories/list?limit=100&offset={(page - 1) * 100}', read=True)
            batch = response.get('items')
            total = response.get('total')
            has_more = page * 100 < total if isinstance(total, int) else len(batch or []) == 100
            kind, completeness = ('remote_memory', 'provider_list')
        else:
            raise ValueError(f'unsupported remote memory export: {strategy}')
        if not isinstance(batch, list) or any((not isinstance(item, dict) for item in batch)):
            raise ValueError(f'{strategy} export did not return a list of memory objects')
        if strategy == 'mem0_platform_v3' and any((item.get('user_id') != memory._require_scope() for item in batch)):
            raise ValueError('Mem0 export contains memory outside this run scope')
        pages.append({'page': page, 'response': response})
        items.extend(batch)
        if not has_more:
            break
    else:
        raise ValueError(f'{strategy} export exceeded 1000 pages')
    if strategy == 'hindsight_cloud':
        for row in items:
            memory_type = str(row.get('type') or row.get('memory_type') or '').lower()
            if memory_type != 'observation' or not isinstance(row.get('id'), str):
                continue
            try:
                history = await memory._request('GET', memory._bank() + '/memories/' + quote(row['id'], safe='') + '/history', read=True)
                row['history'] = history
            except Exception as exc:
                row['history_error'] = f'{type(exc).__name__}: {exc}'
    return {'strategy': strategy, 'kind': kind, 'completeness': completeness, 'exported_at': datetime.now(timezone.utc).isoformat(), 'items': items, 'pages': pages}
