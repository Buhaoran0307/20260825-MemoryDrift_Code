"""Hosted memory adapters with run/agent isolation and durable write receipts.

Only event.content is submitted as content. Provider-generated memories remain
remote; snapshots are audit/checkpoint state, never an offline memory substitute.
"""
from __future__ import annotations
import asyncio
import hashlib
import inspect
import json
import math
import os
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from .memory import MemoryContext, MemoryEvent, MemoryQuery, MemorySnapshot, _EventMemory
from .cloud_transport import CloudHTTPTransport, response_detail
from .llm_diagnostics import redact, safe_endpoint
from .memory_read_router import classify_cloud_failure
CLOUD_STRATEGIES = frozenset({'hindsight_cloud', 'memos_cloud'})
API_BASES = {'hindsight_cloud': 'https://api.hindsight.vectorize.io', 'memos_cloud': 'https://memos.memtensor.cn/api/openmem/v1'}
COMMON_DEFAULTS = {'request_timeout_seconds': 30.0, 'processing_timeout_seconds': 600.0, 'poll_interval_seconds': 2.0, 'max_read_retries': 3}
RETRIEVAL_DEFAULTS = {'hindsight_cloud': {'recall_budget': 'mid', 'recall_max_tokens': 4096}, 'memos_cloud': {'retrieval_top_k': 9, 'search_threshold': 0.45}}
for _defaults in RETRIEVAL_DEFAULTS.values():
    _defaults['context_max_chars'] = 12000

def scope_id(run_id: str, agent_id: str) -> str:
    if not run_id or not agent_id:
        raise ValueError('cloud memory requires run_id and agent_id')
    return 'ac_' + digest([run_id, agent_id])

def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

class CloudMemoryError(RuntimeError):
    """Stop the run; never silently substitute an empty retrieval."""

    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = deepcopy(diagnostics or {})

class CloudHTTPError(CloudMemoryError):

    def __init__(self, service, status):
        self.status_code = status
        super().__init__(f'{service} HTTP {status}; run paused, inspect cloud checkpoint')

class CloudUnknownWrite(CloudMemoryError):
    """A write may have committed. It must be reconciled before any resend."""

class CloudMemory(_EventMemory):
    preserve_progress_on_failure = True
    _authorization_scheme = 'Bearer'

    def __init__(self, *, strategy, agent_id, llm=None, options=None, runtime=None):
        super().__init__(agent_id=agent_id, llm=llm)
        self.strategy = strategy
        options = dict(options or {})
        defaults = {**COMMON_DEFAULTS, **RETRIEVAL_DEFAULTS[strategy]}
        unknown = options.keys() - defaults.keys()
        if unknown:
            raise ValueError(f'unsupported {strategy} options: {sorted(unknown)}')
        for name, default in defaults.items():
            value = options.get(name, default)
            if isinstance(default, bool):
                if type(value) is not bool:
                    raise ValueError(f'{name} must be boolean')
            elif isinstance(default, str):
                if value not in {'low', 'mid', 'high'}:
                    raise ValueError('recall_budget must be low, mid or high')
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or (not math.isfinite(value)):
                    raise ValueError(f'{name} must be a finite number')
                if isinstance(default, int) and type(value) is not int:
                    raise ValueError(f'{name} must be an integer')
                low, high = (0, 1) if name in {'search_threshold', 'search_distance'} else (1, 86400)
                if name == 'max_read_retries':
                    low, high = (0, 10)
                elif name == 'retrieval_top_k':
                    low, high = (1, 25 if strategy == 'memos_cloud' else 100)
                elif name == 'recall_max_tokens':
                    low, high = (1, 100000)
                elif name == 'context_max_chars':
                    low, high = (1, 1000000)
                elif name == 'merged_max_items':
                    low, high = (1, 10000)
                elif name.endswith('seconds'):
                    low = 0.001
                if not low <= value <= high:
                    raise ValueError(f'{name} must be between {low} and {high}')
            setattr(self, name, value)
        self._setting_names = tuple(defaults)
        runtime = dict(runtime or {})
        allowed = {'api_key', 'run_id', 'transport', 'checkpoint_path', 'read_only', 'request_timeout_seconds', 'physical_scope', 'account_id', 'api_base', 'org_id', 'project_id'}
        if runtime.keys() - allowed:
            raise ValueError('unsupported cloud memory runtime fields')
        self._api_key = runtime.get('api_key', '')
        self._physical_scope = runtime.get('physical_scope')
        self._account_id = runtime.get('account_id')
        self._api_base = runtime.get('api_base') or API_BASES[strategy]
        if not isinstance(self._api_base, str) or not self._api_base.startswith(('https://', 'http://')):
            raise ValueError('cloud API base must be an HTTP URL')
        self._api_base = self._api_base.rstrip('/')
        if self._physical_scope is not None and (strategy not in {'hindsight_cloud', 'memos_cloud'} or not runtime.get('read_only') or (not isinstance(self._physical_scope, str)) or (not self._physical_scope) or (self._physical_scope in {'.', '..'}) or any((c in '/\\' or ord(c) < 32 or ord(c) == 127 for c in self._physical_scope))):
            raise ValueError('physical cloud scope requires a valid read-only provider scope')
        self._runtime_timeout = runtime.get('request_timeout_seconds')
        if self._runtime_timeout is not None and (isinstance(self._runtime_timeout, bool) or not isinstance(self._runtime_timeout, (int, float)) or (not math.isfinite(self._runtime_timeout)) or (not 0 < self._runtime_timeout <= 86400)):
            raise ValueError('runtime request_timeout_seconds must be finite and positive')
        self._run_id = runtime.get('run_id')
        self._scope_id = scope_id(self._run_id, agent_id) if self._run_id else None
        self._transport = runtime.get('transport') or CloudHTTPTransport()
        self._current_event_id = None
        self._last_request_diagnostics = {}
        self._read_only = runtime.get('read_only', False)
        self._checkpoint_path = Path(runtime['checkpoint_path']) if runtime.get('checkpoint_path') else None
        self._journal_loaded = False
        self._records = {}
        self._scope_created = False
        self._remote_scope_id = None
        self._ingestion_status = 'empty'
        self._ready_at = None
        self._peers = set()
        self._sessions = set()
        self.last_retrieval = None
        self._query_details = None

    def _scope(self):
        if not self._scope_id:
            raise CloudMemoryError('cloud memory has no experiment/agent scope')
        return self._scope_id

    @property
    def pending_update_event_ids(self):
        return frozenset((k for k, r in self._records.items() if r['status'] != 'ready'))

    def _snapshot_options(self):
        return {**{k: getattr(self, k) for k in self._setting_names}, 'schema_version': 1, 'remote_live': True, 'scope': {'scheme': 'sha256_run_agent_v1', 'run_id': self._run_id, 'agent_id': self.agent_id, 'id': self._scope_id}, 'scope_created': self._scope_created, 'remote_scope_id': self._remote_scope_id, 'records': deepcopy(self._records), 'ingestion_status': self._ingestion_status, 'ready_at': self._ready_at, 'peers': sorted(self._peers), 'sessions': sorted(self._sessions)}

    def restore(self, snapshot):
        if snapshot.strategy != self.strategy:
            raise ValueError('cloud snapshot strategy mismatch')
        o = {k: v for k, v in RETRIEVAL_DEFAULTS[self.strategy].items() if k in {'context_max_chars', 'merged_max_items'}}
        o.update(snapshot.options)
        if set(o) != set(self._snapshot_options()) or o.get('schema_version') != 1 or o.get('remote_live') is not True:
            raise ValueError('invalid cloud snapshot schema')
        for name in self._setting_names:
            if o[name] != getattr(self, name):
                raise ValueError(f'cloud snapshot setting mismatch: {name}')
        scope = o['scope']
        if not isinstance(scope, dict) or set(scope) != {'scheme', 'run_id', 'agent_id', 'id'}:
            raise ValueError('invalid cloud scope')
        if scope['scheme'] != 'sha256_run_agent_v1' or scope['agent_id'] != self.agent_id:
            raise ValueError('cloud snapshot agent/scheme mismatch')
        expected = scope_id(scope['run_id'], self.agent_id) if scope['run_id'] else None
        if scope['id'] != expected or (self._run_id and scope['run_id'] != self._run_id):
            raise ValueError('cloud snapshot experiment scope mismatch')
        events = [MemoryEvent.from_dict(v) for v in snapshot.events]
        ids = [e.event_id for e in events]
        if len(ids) != len(set(ids)) or snapshot.revision != len(ids):
            raise ValueError('invalid cloud event revision')
        records = o['records']
        if not isinstance(records, dict) or set(records) != set(ids):
            raise ValueError('cloud receipts must cover every source event')
        for e in events:
            r = records[e.event_id]
            if r.get('event_digest') != digest(e.as_dict()) or r.get('status') not in {'uncertain', 'accepted', 'ready', 'rejected', 'failed'}:
                raise ValueError('invalid cloud write receipt')
        if o['ingestion_status'] not in {'empty', 'uploading', 'processing', 'paused', 'ready'}:
            raise ValueError('invalid cloud ingestion status')
        if o['ingestion_status'] == 'ready' and (not o['ready_at'] or any((r['status'] != 'ready' for r in records.values()))):
            raise ValueError('cloud ready state has pending writes')
        if records and (not expected):
            raise ValueError('populated cloud memory must have scope')
        self._run_id, self._scope_id = (scope['run_id'], expected)
        self._events, self._event_ids, self.revision = (events, set(ids), len(ids))
        self._records = deepcopy(records)
        self._scope_created = o['scope_created']
        self._remote_scope_id = o['remote_scope_id']
        self._ingestion_status, self._ready_at = (o['ingestion_status'], o['ready_at'])
        self._peers, self._sessions = (set(o['peers']), set(o['sessions']))

    def _persist(self):
        if not self._checkpoint_path or self._read_only:
            return
        p = self._checkpoint_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.pending')
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(self.snapshot().as_dict(), f, ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    def _load_journal(self):
        if self._journal_loaded or self._read_only:
            return
        if self._checkpoint_path and self._checkpoint_path.is_file():
            saved = MemorySnapshot.from_dict(json.loads(self._checkpoint_path.read_text(encoding='utf-8')))
            if saved.revision < self.revision:
                raise CloudMemoryError('checkpoint is older than restored memory; refusing to replay')
            self.restore(saved)
        self._journal_loaded = True

    async def _request(self, method, path, body=None, *, read=False, allow_404=False):
        if not self._api_key:
            raise CloudMemoryError(f'{self.strategy} API key is not configured')
        if self._read_only and (not read):
            raise CloudMemoryError('second-stage cloud memory is read-only')
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8') if body is not None else None
        request_id = uuid.uuid4().hex
        timeout = self._runtime_timeout or self.request_timeout_seconds
        secrets = (self._api_key,) + tuple((e.content for e in self._events))
        for attempt in range(self.max_read_retries + 1 if read else 1):
            started = time.monotonic()
            detail = {'request_id': request_id, 'provider': self.strategy, 'run_id': self._run_id, 'agent_id': self.agent_id, 'source_event_id': self._current_event_id, 'method': method, 'endpoint': safe_endpoint(self._api_base + path, secrets), 'attempt': attempt + 1, 'read_only': read, 'timeout_seconds': timeout, 'configured_timeout_seconds': self.request_timeout_seconds, 'at': now()}
            response = None
            try:
                response = self._transport.request(method, self._api_base + path, headers={'Authorization': self._authorization_scheme + ' ' + self._api_key, 'Content-Type': 'application/json', 'Accept': 'application/json'}, body=encoded, timeout=timeout)
                if inspect.isawaitable(response):
                    response = await response
            except Exception as exc:
                response = None
                detail.update(getattr(exc, 'diagnostics', {}) or {'status_code': None, 'error_type': type(exc).__name__, 'error_message': redact(str(exc), secrets)[:1024]})
            if response is not None:
                detail['status_code'] = response.status_code
                if getattr(response, 'connection_attempts', None):
                    detail['connection_attempts'] = deepcopy(response.connection_attempts)
                if not 200 <= response.status_code < 300:
                    detail.update(response_detail(response.status_code, response.body, response.headers, secrets))
            status = detail.get('status_code')
            failure = classify_cloud_failure(status, detail.get('response_body'), detail.get('response_headers'), provider=self.strategy)
            detail['cloud_failure'] = failure
            retry = read and attempt < self.max_read_retries and (status is None or status in {408, 429, 500, 502, 503, 504}) and (failure['category'] != 'quota_exhausted')
            detail.update(elapsed_seconds=round(time.monotonic() - started, 3), will_retry=retry, final_attempt=not retry)
            self._record_request(detail)
            if retry:
                await asyncio.sleep(min(2 ** attempt, 10))
                continue
            if status is None:
                reason = detail.get('error_type', 'transport failure')
                raise CloudMemoryError(f'{self.strategy} transport failed ({reason}); request_id={request_id}', detail) from None
            if status == 404 and allow_404:
                return None
            if not 200 <= status < 300:
                error = CloudHTTPError(self.strategy, status)
                error.diagnostics = deepcopy(detail)
                error.cloud_failure = failure
                raise error
            try:
                raw = response.body
                data = deepcopy(raw) if isinstance(raw, (dict, list)) else json.loads(raw)
                if not isinstance(data, (dict, list)):
                    raise ValueError()
                return data
            except (ValueError, TypeError, UnicodeError):
                detail.update(response_detail(status, response.body, response.headers, secrets))
                detail['error_type'] = 'InvalidJSONResponse'
                self._record_request(detail)
                raise CloudMemoryError(f'{self.strategy} invalid JSON response', detail) from None

    def _record_request(self, detail):
        self._last_request_diagnostics = deepcopy(detail)
        if self._checkpoint_path and (not self._read_only):
            path = self._checkpoint_path.with_suffix('.requests.jsonl')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(detail, ensure_ascii=False, allow_nan=False) + '\n')
                f.flush()
                os.fsync(f.fileno())

    async def initialize(self, events):
        await self.observe(events)

    async def observe(self, events):
        if self._read_only:
            raise CloudMemoryError('second-stage memory cannot ingest events')
        self._load_journal()
        self._scope()
        try:
            if events and (not self._scope_created):
                await self._ensure_scope()
                self._scope_created = True
                self._persist()
            for event in events:
                self._current_event_id = event.event_id
                previous = self._records.get(event.event_id)
                if previous:
                    if previous['event_digest'] != digest(event.as_dict()):
                        raise CloudMemoryError('source event changed under an existing event ID')
                    if previous['status'] == 'uncertain':
                        await self._reconcile(event, previous)
                    if previous['status'] in {'accepted', 'ready'}:
                        continue
                    if previous['status'] != 'rejected':
                        raise CloudMemoryError('cloud processing failed; inspect checkpoint before resuming')
                await self._prepare_event(event)
                record = {'event_digest': digest(event.as_dict()), 'status': 'uncertain', 'document_id': 'ev_' + digest([self._scope(), event.event_id]), 'operation_id': str(uuid.uuid5(uuid.NAMESPACE_URL, self._scope() + event.event_id)), 'remote_id': None, 'submitted_at': now()}
                self._remember_events([event])
                self._records[event.event_id] = record
                self._ingestion_status, self._ready_at = ('uploading', None)
                self._persist()
                try:
                    await self._submit(event, record)
                except CloudHTTPError as exc:
                    record['write_failure'] = deepcopy(exc.diagnostics)
                    if exc.status_code in {400, 401, 402, 403, 413, 422, 429}:
                        record['status'] = 'rejected'
                    raise
                except Exception as exc:
                    record['write_failure'] = deepcopy(getattr(exc, 'diagnostics', {}))
                    self._persist()
                    await self._reconcile(event, record)
                self._persist()
            if self.pending_update_event_ids:
                self._ingestion_status = 'processing'
            self._persist()
        except BaseException:
            self._ingestion_status = 'paused'
            self._persist()
            raise

    def _metadata(self, event, record):
        return {'source': 'agent_community', 'source_event_ids': [event.event_id], 'batch_digest': record['event_digest'], 'run_agent_scope': self._scope()}

    async def _prepare_event(self, event):
        """Create idempotent identity resources before a content write receipt."""

    async def _reconcile(self, event, record):
        raise CloudUnknownWrite(f'{self.strategy}: write outcome unknown for {event.event_id}; no automatic resend', {'write_failure': record.get('write_failure', {}), 'reconciliation': deepcopy(self._last_request_diagnostics)})

    async def finalize_ingestion(self):
        if self._read_only:
            raise CloudMemoryError('second-stage cannot finalize ingestion')
        self._load_journal()
        deadline = time.monotonic() + self.processing_timeout_seconds
        self._ingestion_status = 'processing'
        self._persist()
        try:
            while True:
                for event in self._events:
                    r = self._records[event.event_id]
                    if r['status'] == 'uncertain':
                        await self._reconcile(event, r)
                    if r['status'] in {'failed', 'rejected'}:
                        raise CloudMemoryError(f'{self.strategy}: event {event.event_id} not processed')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CloudMemoryError(f'{self.strategy}: processing deadline exceeded; run paused')
                try:
                    ready = not self._records or await asyncio.wait_for(self._poll_ready(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise CloudMemoryError(f'{self.strategy}: processing deadline exceeded; run paused') from None
                if ready:
                    for r in self._records.values():
                        r['status'] = 'ready'
                    self._ingestion_status, self._ready_at = ('ready', now())
                    self._persist()
                    return
                self._persist()
                if time.monotonic() >= deadline:
                    raise CloudMemoryError(f'{self.strategy}: processing deadline exceeded; run paused')
                await asyncio.sleep(min(self.poll_interval_seconds, max(0, deadline - time.monotonic())))
        except BaseException:
            self._ingestion_status = 'paused'
            self._persist()
            raise

    async def retrieve(self, query: MemoryQuery):
        if not isinstance(query.text, str) or not query.text.strip():
            raise ValueError('cloud retrieval query cannot be empty')
        if self._ingestion_status != 'ready':
            if self._read_only:
                raise CloudMemoryError('source ingestion is incomplete; second stage is unavailable')
            await self.finalize_ingestion()
        self.last_retrieval = None
        self._query_details = None
        rows, payload = await self._search(query.text) if self._records else ([], {'query': query.text, 'empty_scope': True})
        if not isinstance(rows, list) or not all((isinstance(r, dict) for r in rows)):
            raise CloudMemoryError('invalid cloud memory search results')
        selected = self._select_results(rows, query)
        items = []
        for r in selected:
            content = r.get(self._text_field)
            if not isinstance(content, str) or not content.strip():
                raise CloudMemoryError('search result has no memory text')
            items.append(MemoryEvent(event_id=f"{self.strategy}:{r.get('id') or digest(r)}", thread_id=query.thread_id, kind='cloud_memory', actor_id='', content=content, created_at=str(r.get('created_at') or r.get('updatedAt') or ''), provenance=self.strategy, visibility='retrieved', metadata={'remote_result': deepcopy(r), 'scope_id': self._scope()}))
        self.last_retrieval = {'provider': self.strategy, 'scope_id': self._scope(), 'retrieved_at': now(), 'remote_scope_id': self._remote_scope_id, 'request': deepcopy(payload), 'results': deepcopy(rows), 'remote_live': True, 'source_ready_at': self._ready_at, 'details': deepcopy(self._query_details)}
        if self._physical_scope:
            self.last_retrieval.update(physical_scope=self._physical_scope, account_id=self._account_id)
        return MemoryContext(self.strategy, tuple(items), self.revision)

    def _select_results(self, rows, query):
        return rows if query.max_items is None else rows[:max(0, query.max_items)]

class HindsightCloudMemory(CloudMemory):
    _text_field = 'text'

    def __init__(self, *, options=None, **kwargs):
        self._bank_initialization = None
        options = dict(options or {})
        self.retrieval_top_k = options.pop('retrieval_top_k', 10)
        if isinstance(self.retrieval_top_k, bool) or not isinstance(self.retrieval_top_k, int) or (not 1 <= self.retrieval_top_k <= 1000):
            raise ValueError('retrieval_top_k must be an integer between 1 and 1000')
        self.min_final_score = options.pop('min_final_score', None)
        if self.min_final_score is not None and (isinstance(self.min_final_score, bool) or not isinstance(self.min_final_score, (int, float)) or (not math.isfinite(self.min_final_score)) or (self.min_final_score < 0)):
            raise ValueError('min_final_score must be a finite non-negative number or null')
        target_bank = options.pop('target_bank', '')
        if not isinstance(target_bank, str):
            raise ValueError('target_bank must be a bank ID string')
        self.target_bank = target_bank.strip()
        if self.target_bank in {'.', '..'} or any((c in '/\\' or ord(c) < 32 or ord(c) == 127 for c in self.target_bank)):
            raise ValueError('target_bank must be a single bank ID without path separators or control characters')
        super().__init__(strategy='hindsight_cloud', options=options, **kwargs)
        if self.min_final_score is not None:
            self._setting_names += ('min_final_score',)
        if self.retrieval_top_k != 10:
            self._setting_names += ('retrieval_top_k',)

    def _snapshot_options(self):
        options = super()._snapshot_options()
        if self.target_bank:
            options['target_bank'] = self.target_bank
        if self._bank_initialization is not None:
            options['bank_initialization'] = deepcopy(self._bank_initialization)
        return options

    def restore(self, snapshot):
        if snapshot.options.get('retrieval_top_k', 10) != self.retrieval_top_k:
            raise ValueError('cloud snapshot setting mismatch: retrieval_top_k')
        if snapshot.options.get('target_bank', '') != self.target_bank:
            raise ValueError('cloud snapshot setting mismatch: target_bank')
        if snapshot.options.get('min_final_score') != self.min_final_score:
            raise ValueError('cloud snapshot setting mismatch: min_final_score')
        scope = snapshot.options.get('scope', {})
        expected = self.target_bank or (scope.get('id') if isinstance(scope, dict) else None)
        remote = snapshot.options.get('remote_scope_id')
        if remote is not None and remote != expected or (snapshot.options.get('scope_created') and remote != expected):
            raise ValueError('Hindsight snapshot bank identity mismatch')
        initialization = snapshot.options.get('bank_initialization')
        if initialization is not None:
            if self.target_bank or not isinstance(initialization, dict) or initialization.get('status') not in {'prepared', 'importing', 'verified'} or (not isinstance(initialization.get('template'), dict)) or (initialization.get('sha256') != digest(initialization['template'])) or (initialization.get('bank_id') != expected) or (snapshot.options.get('scope_created') and initialization['status'] != 'verified'):
                raise ValueError('invalid Hindsight bank initialization checkpoint')
        previous = self._bank_initialization
        self._bank_initialization = deepcopy(initialization)
        try:
            super().restore(snapshot)
        except Exception:
            self._bank_initialization = previous
            raise

    def _bank_id(self):
        return self._physical_scope or self.target_bank or self._scope()

    def _bank(self):
        return '/v1/default/banks/' + quote(self._bank_id(), safe='')

    async def _bank_exists(self):
        offset, seen = (0, set())
        while True:
            data = await self._request('GET', f'/v1/default/banks?limit=100&offset={offset}', read=True)
            rows, total = (data.get('banks'), data.get('total'))
            if not isinstance(rows, list) or type(total) is not int or total < 0 or (type(data.get('offset')) is not int) or (data['offset'] != offset) or (not all((isinstance(row, dict) and isinstance(row.get('bank_id'), str) and row['bank_id'] for row in rows))):
                raise CloudMemoryError('Hindsight bank inventory is invalid; cannot confirm bank existence')
            ids = [row['bank_id'] for row in rows]
            if self._bank_id() in ids:
                return True
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise CloudMemoryError('Hindsight bank inventory pagination made no progress')
            seen.update(ids)
            offset += len(rows)
            if offset >= total:
                return False
            if not rows:
                raise CloudMemoryError('Hindsight bank inventory is incomplete; cannot confirm bank existence')

    async def _ensure_scope(self):
        if self.target_bank:
            if not await self._bank_exists():
                raise CloudMemoryError('Hindsight target_bank does not exist; use an existing bank ID or leave it blank')
            data = await self._request('GET', self._bank() + '/config', read=True)
        else:
            await self._initialize_default_bank()
            return
        actual = data.get('bank_id') or data.get('id')
        if actual != self._bank_id():
            raise CloudMemoryError('Hindsight bank identity mismatch')
        self._remote_scope_id = actual

    async def _initialize_default_bank(self):
        if self._bank_initialization is None:
            template_path = Path(__file__).with_name('templates') / 'hindsight-community-bot.json'
            template = json.loads(template_path.read_text(encoding='utf-8'))
            self._bank_initialization = {'bank_id': self._bank_id(), 'status': 'prepared', 'source': 'user-provided:2026-09-13:community-bot-retain-recall', 'template': template, 'sha256': digest(template)}
            self._persist()
        initialization = self._bank_initialization
        if initialization['status'] == 'prepared':
            if await self._bank_exists():
                raise CloudMemoryError('Hindsight automatic bank already exists; refusing to overwrite it')
            initialization['status'] = 'importing'
            initialization['submitted_at'] = now()
            self._persist()
            receipt = await self._request('POST', self._bank() + '/import', initialization['template'])
            initialization['receipt'] = receipt
            self._persist()
        if not await self._bank_exists():
            raise CloudMemoryError('Hindsight template import unconfirmed; no events uploaded; inspect bank initialization')
        config = await self._request('GET', self._bank() + '/config', read=True, allow_404=True)
        if config is None or config.get('bank_id') != self._bank_id():
            raise CloudMemoryError('Hindsight template import unconfirmed; no events uploaded; inspect bank initialization')
        exported = await self._request('GET', self._bank() + '/export', read=True)
        template = initialization['template']
        if not self._template_matches(template, exported, config.get('config')):
            raise CloudMemoryError('Hindsight imported template verification failed; no events uploaded; inspect bank initialization')
        initialization.update(status='verified', verified_at=now(), verified_export=exported, resolved_config=config['config'])
        self._remote_scope_id = self._bank_id()
        self._persist()

    @staticmethod
    def _template_matches(template, exported, config):

        def contains(expected, actual):
            if isinstance(expected, dict):
                return isinstance(actual, dict) and all((key in actual and contains(value, actual[key]) for key, value in expected.items()))
            return type(expected) is type(actual) and expected == actual
        if not isinstance(exported, dict) or exported.get('version') != template['version']:
            return False
        if not contains(template['bank'], config) or not contains(template['bank'], exported.get('bank')):
            return False
        for section, key in (('mental_models', 'id'), ('directives', 'name')):
            if section not in template:
                continue
            rows = exported.get(section)
            if not isinstance(rows, list) or not all((isinstance(row, dict) for row in rows)):
                return False
            for expected in template[section]:
                matches = [row for row in rows if row.get(key) == expected[key]]
                if len(matches) != 1 or not contains(expected, matches[0]):
                    return False
        return True

    async def _submit(self, event, r):
        data = await self._request('POST', self._bank() + '/memories', {'items': [{'content': event.content, 'document_id': r['document_id'], 'metadata': {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in self._metadata(event, r).items()}}], 'async': True, 'operation_id': r['operation_id']})
        if data.get('success') is False or not data.get('operation_id'):
            raise CloudUnknownWrite('Hindsight retain has no usable operation receipt')
        r.update(remote_id=data['operation_id'], operation_id=data['operation_id'], status='accepted')

    async def _reconcile(self, event, r):
        data = await self._request('GET', self._bank() + '/operations/' + quote(r['operation_id'], safe=''), read=True, allow_404=True)
        if data is None or data.get('status') == 'not_found':
            await super()._reconcile(event, r)
        r.update(remote_id=r['operation_id'], status='accepted')
        self._persist()

    async def _poll_ready(self):
        for r in self._records.values():
            data = await self._request('GET', self._bank() + '/operations/' + quote(r['remote_id'], safe=''), read=True)
            status = data.get('status')
            if status in {'failed', 'cancelled'}:
                r['status'] = 'failed'
                raise CloudMemoryError('Hindsight retain operation failed')
            if status != 'completed':
                return False
        for status in ('failed', 'pending', 'processing'):
            data = await self._request('GET', self._bank() + f'/operations?status={status}&limit=100', read=True)
            ops = data.get('operations')
            if not isinstance(ops, list):
                raise CloudMemoryError('Hindsight operation list missing')
            if ops and status == 'failed':
                raise CloudMemoryError('Hindsight background operation failed')
            if ops:
                return False
        return True

    async def _search(self, query):
        payload = {'query': query, 'budget': self.recall_budget, 'max_tokens': self.recall_max_tokens}
        data = await self._request('POST', self._bank() + '/memories/recall', payload, read=True)
        return (data.get('results'), payload)

    def _select_results(self, rows, query):
        scores = [(row.get('scores') or {}).get('final') for row in rows]
        scored = all((isinstance(score, (int, float)) and (not isinstance(score, bool)) and math.isfinite(score) for score in scores))
        ranked = sorted(rows, key=lambda row: row['scores']['final'], reverse=True) if scored else rows
        limit = self.retrieval_top_k
        if query.max_items is not None:
            limit = min(limit, max(0, query.max_items))
        selected = ranked[:limit]
        self._query_details = {'ordering': 'scores.final descending' if scored else 'provider order (scores unavailable)', 'retrieval_top_k': self.retrieval_top_k, 'candidate_count': len(rows), 'selected_count': len(selected), 'selected_ids': [row.get('id') for row in selected]}
        return selected

def build_cloud_memory(strategy, **kwargs):
    if strategy == 'memos_cloud':
        from .memos_memory import MemosCloudMemory
        return MemosCloudMemory(**kwargs)
    cls = {'hindsight_cloud': HindsightCloudMemory}[strategy]
    return cls(**kwargs)
