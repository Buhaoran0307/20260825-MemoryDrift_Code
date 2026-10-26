from __future__ import annotations
import argparse
import json
import mimetypes
import re
import shutil
import tempfile
import zipfile
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from agent_posting_core.memory import SUPPORTED_MEMORY_STRATEGIES
from agent_posting_core.post_selection_file import parse_post_selection_file
from agent_posting_core.tool_selection import API_KEY_POSITIONS, parse_mcp_file, parse_query_file
from .config import PROJECT_ROOT
from .experiment_config import SELECTABLE_EXPERIMENT_MODELS
from .platform_service import DEFAULT_EXPERIMENT_CONFIG, DEFAULT_PLATFORM_DB, ExperimentPlatform
ROOT = Path(__file__).resolve().parent
PLATFORM_STATIC_DIR = ROOT / 'platform_static'
MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MEMORY_STRATEGY_LABELS = {'langmem_official': {'label_zh': 'LangMem official SDK (LangGraph Store)', 'label_en': 'LangMem official SDK (LangGraph Store)'}, 'mem0_platform_v3': {'label_zh': 'Mem0 Platform V3 hosted API', 'label_en': 'Mem0 Platform V3 hosted API'}, 'a_mem_official': {'label_zh': 'A-Mem official repository', 'label_en': 'A-Mem official repository'}, 'hindsight_cloud': {'label_zh': 'Hindsight Cloud', 'label_en': 'Hindsight Cloud'}, 'memos_cloud': {'label_zh': 'MemOS Cloud', 'label_en': 'MemOS Cloud'}}

class PlatformRequestHandler(BaseHTTPRequestHandler):
    platform: ExperimentPlatform
    server_version = 'AgentExperimentPlatform/0.1'

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if self._memory_api(path):
                self._assert_memory_origin()
            if path == '/api/platform/memory-accounts':
                store = self.platform.memory_workspace.store
                self._json({'accounts': store.list_accounts(_first(query, 'provider')), 'next_account_names': {provider: store.next_account_name(provider) for provider in ('mem0_platform_v3', 'hindsight_cloud', 'memos_cloud')}})
                return
            account_memories = _match(path, '/api/platform/memory-accounts/([^/]+)/memories')
            if account_memories:
                self._json(self.platform.memory_workspace.account_inventory(_url_text(account_memories)))
                return
            if path == '/api/platform/memories':
                self._json(self.platform.memory_workspace.index(strategy=_first(query, 'strategy'), group_id=_first(query, 'group_id'), run_id=_first(query, 'run_id'), agent_id=_first(query, 'agent_id'), round_index=_query_int(query, 'round_index', None), limit=_query_int(query, 'limit', 50), offset=_query_int(query, 'offset', 0)))
                return
            if path == '/api/platform/memory-jobs':
                self._json({'jobs': self.platform.memory_workspace.store.list_jobs(_first(query, 'run_id'))})
                return
            if path == '/api/platform/memory-replicas':
                self._json({'replicas': self.platform.memory_workspace.store.list_replicas(_first(query, 'source_run_id'), _first(query, 'agent_id'), _first(query, 'snapshot_sha256'))})
                return
            memory_job = _match(path, '/api/platform/memory-jobs/([^/]+)')
            if memory_job:
                self._json({'job': self.platform.memory_workspace.store.get_job(_url_text(memory_job))})
                return
            memory_run = _match(path, '/api/platform/memories/([^/]+)')
            if memory_run:
                self._json(self.platform.run_memory_result(_url_text(memory_run), agent_id=_first(query, 'agent_id'), round_index=_query_int(query, 'round_index', None), limit=_query_int(query, 'limit', 100), offset=_query_int(query, 'offset', 0)))
                return
            if _is_platform_page(path):
                self._send_file(PLATFORM_STATIC_DIR / 'index.html', 'text/html; charset=utf-8')
                return
            if path.startswith('/platform-static/'):
                self._send_static(path.removeprefix('/platform-static/'))
                return
            if path == '/api/platform/health':
                self._json({'ok': True, 'service': 'experiment-platform'})
                return
            if path == '/api/platform/dashboard':
                self._json(self.platform.dashboard())
                return
            if path == '/api/platform/config/default':
                self._json({'config': self.platform.default_config()})
                return
            if path == '/api/platform/settings':
                self._json({'settings': self.platform.platform_settings()})
                return
            if path == '/api/platform/config/schema':
                self._json(_config_schema())
                return
            if path == '/api/platform/resources':
                kind = _first(query, 'kind')
                self._json({'resources': self.platform.list_resources(kind=kind, archived=_query_archive_filter(query), include_content=_query_bool(query, 'include_content', False))})
                return
            if path == '/api/platform/target-mappings':
                self._json({'mappings': self.platform.list_target_mappings()})
                return
            resource_id = _match(path, '/api/platform/resources/([^/]+)')
            if resource_id:
                self._json(self.platform.get_resource(_url_text(resource_id), include_content=_query_bool(query, 'include_content', True)))
                return
            if path == '/api/platform/experiment-groups':
                self._json({'groups': self.platform.list_experiment_groups(archived=_query_archive_filter(query))})
                return
            group_id = _match(path, '/api/platform/experiment-groups/([^/]+)')
            if group_id:
                self._json(self.platform.get_experiment_group(_url_text(group_id)))
                return
            if path == '/api/platform/experiments':
                self._json({'experiments': self.platform.list_experiments(archived=_query_archive_filter(query), group_id=_first(query, 'group_id'), stage=_first(query, 'stage'))})
                return
            if path == '/api/platform/experiment-id/suggest':
                self._json({'experiment_id': self.platform.suggest_experiment_id(stage=_first(query, 'stage') or 'interaction', memory_source_mode=_first(query, 'memory_source_mode') or 'run', source_experiment_id=_first(query, 'source_experiment_id'), group_id=_first(query, 'group_id'))})
                return
            if path == '/api/platform/memory-sources':
                self._json(self.platform.memory_sources(experiment_id=_first(query, 'experiment_id'), group_id=_first(query, 'group_id'), limit=_query_int(query, 'limit', 100), offset=_query_int(query, 'offset', 0)))
                return
            experiment_id = _match(path, '/api/platform/experiments/([^/]+)')
            if experiment_id:
                self._json(self.platform.get_experiment(_url_text(experiment_id)))
                return
            if path == '/api/platform/runs':
                self._json({'runs': self.platform.list_runs(experiment_id=_first(query, 'experiment_id'), group_id=_first(query, 'group_id'), status=_first(query, 'status'), limit=_query_int(query, 'limit', 50), offset=_query_int(query, 'offset', 0), lightweight=True)})
                return
            if path == '/api/platform/metrics/compare':
                self._json(self.platform.compare_runs(query.get('run_id', [])))
                return
            if path == '/api/platform/validation-comparison/options':
                self._json(self.platform.validation_comparison_options())
                return
            selection_detail = _match(path, '/api/platform/runs/([^/]+)/selections/([0-9]+)')
            if selection_detail:
                run_id, query_index = selection_detail
                self._json(self.platform.run_selection_detail(_url_text(run_id), int(query_index)))
                return
            run_resource = _match(path, '/api/platform/runs/([^/]+)/(thread|forum|rounds|selections|memory-result)')
            if run_resource:
                run_id, resource = run_resource
                run_id = _url_text(run_id)
                if resource == 'thread':
                    self._json(self.platform.run_thread(run_id, comments_limit=_query_int(query, 'comments_limit', 200)))
                elif resource == 'forum':
                    self._json(self.platform.run_forum(run_id, post_limit=_query_int(query, 'post_limit', 100), comments_limit=_query_int(query, 'comments_limit', 200)))
                elif resource == 'selections':
                    self._json(self.platform.run_selections(run_id, limit=_query_int(query, 'limit', 100), offset=_query_int(query, 'offset', 0)))
                elif resource == 'memory-result':
                    self._json(self.platform.run_memory_result(run_id, limit=_query_int(query, 'limit', 100), offset=_query_int(query, 'offset', 0), calls_limit=_query_int(query, 'calls_limit', 30), calls_offset=_query_int(query, 'calls_offset', 0), agent_id=_first(query, 'agent_id'), round_index=_query_int(query, 'round_index', None)))
                else:
                    self._json(self.platform.run_rounds(run_id, limit=_query_int(query, 'limit', 20), offset=_query_int(query, 'offset', 0)))
                return
            run_id = _match(path, '/api/platform/runs/([^/]+)')
            if run_id:
                self._json(self.platform.get_run(_url_text(run_id)))
                return
            self._json_error(HTTPStatus.NOT_FOUND, 'not_found', f'Unknown path: {path}')
        except Exception as exc:
            self._json_exception(exc)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        try:
            if path == '/api/platform/experiment-bundles/import':
                from .experiment_bundle import MAX_ARCHIVE_BYTES
                query = urllib.parse.parse_qs(parsed.query)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_ARCHIVE_BYTES:
                    raise ValueError('ZIP upload must be between 1 byte and 2 GiB')
                with tempfile.TemporaryDirectory(dir=self.platform.runtime_dir, prefix='bundle-upload-') as folder:
                    upload = Path(folder) / 'bundle.zip'
                    with upload.open('wb') as handle:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError('incomplete ZIP upload')
                            handle.write(chunk)
                            remaining -= len(chunk)
                    new_id = _first(query, 'new_group_id')
                    result = self.platform.import_experiments(upload, group_id=_first(query, 'group_id'), new_group={'group_id': new_id, 'name': _first(query, 'new_group_name'), 'query_revision_id': _first(query, 'query_revision_id'), 'mcp_revision_id': _first(query, 'mcp_revision_id')} if new_id else None, dry_run=_query_bool(query, 'dry_run', False))
                self._json(result)
                return
            payload = self._read_json()
            if self._memory_api(path):
                self._assert_memory_origin()
            if path == '/api/platform/memory-accounts':
                self._json({'account': self.platform.memory_workspace.store.save_account(payload)}, status=HTTPStatus.CREATED)
                return
            if path == '/api/platform/memory-accounts/display-keys':
                if payload != {}:
                    raise ValueError('display-keys requires an empty object')
                self._json(self.platform.memory_workspace.store.display_keys())
                return
            account_import = _match(path, '/api/platform/memory-accounts/([^/]+)/import')
            if account_import:
                self._json(self.platform.memory_workspace.start_account_import(_url_text(account_import), payload), status=HTTPStatus.ACCEPTED)
                return
            account_action = _match(path, '/api/platform/memory-accounts/([^/]+)/check')
            if account_action:
                self._json({'account': self.platform.memory_workspace.check_account(_url_text(account_action))})
                return
            account_id = _match(path, '/api/platform/memory-accounts/([^/]+)')
            if account_id:
                self._json({'account': self.platform.memory_workspace.store.save_account(payload, _url_text(account_id))})
                return
            if path in {'/api/platform/memories/pull', '/api/platform/memories/sync', '/api/platform/memories/connect-existing'}:
                self._json({'job': self.platform.memory_workspace.start(path.rsplit('/', 1)[1], payload)}, status=HTTPStatus.ACCEPTED)
                return
            job_action = _match(path, '/api/platform/memory-jobs/([^/]+)/(retry|cancel)')
            if job_action:
                job_id, action = job_action
                workspace = self.platform.memory_workspace
                self._json({'job': getattr(workspace, action)(_url_text(job_id))})
                return
            if path == '/api/platform/experiment-bundles/export':
                with tempfile.TemporaryDirectory(dir=self.platform.runtime_dir, prefix='bundle-download-') as folder:
                    archive = Path(folder) / 'experiments.zip'
                    self.platform.export_experiments(archive, group_id=payload.get('group_id'), experiment_ids=payload.get('experiment_ids'))
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'application/zip')
                    self.send_header('Content-Length', str(archive.stat().st_size))
                    self.send_header('Content-Disposition', 'attachment; filename="experiments.zip"')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    with archive.open('rb') as handle:
                        shutil.copyfileobj(handle, self.wfile)
                return
            if path in {'/api/platform/config/import-queries', '/api/platform/config/import-mcp'}:
                parser = parse_query_file if path.endswith('import-queries') else parse_mcp_file
                parsed_file = parser(payload.get('filename'), payload.get('content'))
                self._json({'file': {'filename': parsed_file['filename'], 'dataset': parsed_file['dataset']}, **{key: value for key, value in parsed_file.items() if key.endswith('_count')}})
                return
            if path == '/api/platform/config/import-posts':
                self._json(parse_post_selection_file(payload.get('filename'), payload.get('content'), environment_type=payload.get('environment_type', 'moltnet_forum')))
                return
            if path == '/api/platform/config/validate':
                config = payload.get('config')
                if not isinstance(config, dict):
                    raise ValueError('config must be an object')
                experiment_id = payload.get('experiment_id')
                if experiment_id is not None and (not isinstance(experiment_id, str)):
                    raise ValueError('experiment_id must be a string')
                group_id = payload.get('group_id')
                if group_id is not None and (not isinstance(group_id, str)):
                    raise ValueError('group_id must be a string')
                if experiment_id is None and (not (isinstance(group_id, str) and group_id.strip())):
                    raise ValueError('group_id is required for new experiment validation')
                self.platform.assert_registered_resource_config(config, group_id=group_id, experiment_id=experiment_id)
                self._json({'ok': True, 'resolved': self.platform.validate_config(config, experiment_id=experiment_id, group_id=group_id)})
                return
            if path == '/api/platform/memory-preview':
                self._json(self.platform.preview_memory(experiment_id=payload.get('experiment_id'), group_id=payload.get('group_id'), source_experiment_id=payload.get('source_experiment_id'), source_run_id=payload.get('source_run_id'), source_agent_id=payload.get('source_agent_id'), source_round=payload.get('source_round'), query=payload.get('query'), memory=payload.get('memory'), memory_account_pool=payload.get('memory_account_pool')))
                return
            if path == '/api/platform/experiment-groups':
                if payload.get('queries') is not None or payload.get('mcp') is not None:
                    raise ValueError('choose registered revisions instead of embedded group resources')
                group = self.platform.create_experiment_group(group_id=str(payload.get('group_id') or ''), name=str(payload.get('name') or ''), description=str(payload.get('description') or ''), query_revision_id=payload.get('query_revision_id'), mcp_revision_id=payload.get('mcp_revision_id'), target_mapping_id=payload.get('target_mapping_id'))
                self._json(group, status=HTTPStatus.CREATED)
                return
            if path == '/api/platform/experiment-groups/batch-delete':
                if set(payload) - {'group_ids'}:
                    raise ValueError('batch deletion only accepts group_ids')
                self._json(self.platform.delete_experiment_groups(payload.get('group_ids')))
                return
            if path == '/api/platform/experiment-groups/transfer':
                if set(payload) - {'source_group_id', 'target_group_id', 'experiment_ids', 'mode', 'dry_run', 'plan_token'}:
                    raise ValueError('transfer only accepts source_group_id, target_group_id, experiment_ids, mode, dry_run and plan_token')
                dry_run = payload.get('dry_run', False)
                if not isinstance(dry_run, bool):
                    raise ValueError('dry_run must be true or false')
                self._json(self.platform.transfer_experiments(source_group_id=payload.get('source_group_id'), target_group_id=payload.get('target_group_id'), experiment_ids=payload.get('experiment_ids'), mode=payload.get('mode', 'move'), dry_run=dry_run, plan_token=payload.get('plan_token')))
                return
            if path == '/api/platform/experiment-groups/merge':
                if set(payload) - {'group_ids', 'target_group_id', 'dry_run', 'plan_token'}:
                    raise ValueError('merge only accepts group_ids, target_group_id, dry_run and plan_token')
                dry_run = payload.get('dry_run', False)
                if not isinstance(dry_run, bool):
                    raise ValueError('dry_run must be true or false')
                self._json(self.platform.merge_experiment_groups(group_ids=payload.get('group_ids'), target_group_id=payload.get('target_group_id'), dry_run=dry_run, plan_token=payload.get('plan_token')))
                return
            if path == '/api/platform/settings':
                self._json({'settings': self.platform.save_platform_settings(payload.get('settings'))})
                return
            quick_group_id = _match(path, '/api/platform/experiment-groups/([^/]+)/quick-experiments')
            if quick_group_id:
                self._json(self.platform.create_quick_experiments(_url_text(quick_group_id), payload), status=HTTPStatus.CREATED)
                return
            if path == '/api/platform/resources/preview':
                self._json(self.platform.preview_resource(kind=payload.get('kind'), filename=payload.get('filename'), content=payload.get('content'), environment_type=payload.get('environment_type')))
                return
            if path == '/api/platform/target-mappings/preview':
                self._json(self.platform.preview_target_mapping(filename=payload.get('filename'), content=payload.get('content')))
                return
            resource_metadata_id = _match(path, '/api/platform/resources/([^/]+)/metadata')
            if resource_metadata_id:
                if set(payload) - {'name', 'description'}:
                    raise ValueError('only resource name and description can be edited')
                self._json(self.platform.update_resource_metadata(_url_text(resource_metadata_id), name=payload.get('name'), description=payload.get('description')))
                return
            mapping_metadata_id = _match(path, '/api/platform/target-mappings/([^/]+)/metadata')
            if mapping_metadata_id:
                if set(payload) - {'name'}:
                    raise ValueError('only target mapping name can be edited')
                self._json(self.platform.update_target_mapping_name(_url_text(mapping_metadata_id), name=payload.get('name')))
                return
            if path == '/api/platform/resources':
                resource = self.platform.create_resource(resource_id=str(payload.get('resource_id') or ''), kind=str(payload.get('kind') or ''), name=str(payload.get('name') or ''), description=str(payload.get('description') or ''), filename=payload.get('filename'), content=payload.get('content'), environment_type=payload.get('environment_type'))
                self._json(resource, status=HTTPStatus.CREATED)
                return
            if path == '/api/platform/target-mappings':
                mapping = self.platform.create_target_mapping(mapping_id=str(payload.get('mapping_id') or ''), name=str(payload.get('name') or ''), filename=payload.get('filename'), content=payload.get('content'))
                self._json(mapping, status=HTTPStatus.CREATED)
                return
            resource_revision_id = _match(path, '/api/platform/resources/([^/]+)/revisions')
            if resource_revision_id:
                revision = self.platform.add_resource_revision(_url_text(resource_revision_id), filename=payload.get('filename'), content=payload.get('content'), environment_type=payload.get('environment_type'))
                self._json(revision, status=HTTPStatus.CREATED)
                return
            resource_archive_id = _match(path, '/api/platform/resources/([^/]+)/archive')
            if resource_archive_id:
                archived = payload.get('archived')
                if not isinstance(archived, bool):
                    raise ValueError('archived must be true or false')
                self._json(self.platform.set_resource_archived(_url_text(resource_archive_id), archived=archived))
                return
            if path == '/api/platform/validation-comparison':
                self._json(self.platform.validation_comparison(payload.get('run_ids')))
                return
            if path == '/api/platform/experiments':
                config = payload.get('config')
                if not isinstance(config, dict):
                    raise ValueError('config must be an object')
                group_id = payload.get('group_id')
                if not isinstance(group_id, str) or not group_id.strip():
                    raise ValueError('group_id is required when creating an experiment')
                self.platform.assert_registered_resource_config(config, group_id=group_id)
                experiment = self.platform.save_experiment(experiment_id=str(payload.get('experiment_id') or ''), group_id=group_id, name=str(payload.get('name') or ''), description=str(payload.get('description') or ''), config=config, create_only=True)
                self._json(experiment, status=HTTPStatus.CREATED)
                return
            if path == '/api/platform/experiments/archive':
                experiment_ids = payload.get('experiment_ids')
                if not isinstance(experiment_ids, list):
                    raise ValueError('experiment_ids must be an array')
                self._json(self.platform.archive_experiments(experiment_ids))
                return
            if path == '/api/platform/runs/delete':
                run_ids = payload.get('run_ids')
                if not isinstance(run_ids, list):
                    raise ValueError('run_ids must be an array')
                self._json(self.platform.delete_runs(run_ids))
                return
            group_runs_id = _match(path, '/api/platform/experiment-groups/([^/]+)/runs')
            if group_runs_id:
                self._json(self.platform.launch_group_runs(_url_text(group_runs_id), payload.get('experiment_ids')), status=HTTPStatus.ACCEPTED)
                return
            group_mapping_id = _match(path, '/api/platform/experiment-groups/([^/]+)/target-mapping')
            if group_mapping_id:
                mapping_id = payload.get('target_mapping_id')
                if mapping_id is not None and (not isinstance(mapping_id, str)):
                    raise ValueError('target_mapping_id must be a string or null')
                self._json(self.platform.set_group_target_mapping(_url_text(group_mapping_id), mapping_id))
                return
            group_id = _match(path, '/api/platform/experiment-groups/([^/]+)/archive')
            if group_id:
                archived = payload.get('archived')
                if not isinstance(archived, bool):
                    raise ValueError('archived must be true or false')
                self._json(self.platform.set_experiment_group_archived(_url_text(group_id), archived=archived))
                return
            experiment_id = _match(path, '/api/platform/experiments/([^/]+)/archive')
            if experiment_id:
                archived = payload.get('archived')
                if not isinstance(archived, bool):
                    raise ValueError('archived must be true or false')
                self._json(self.platform.set_experiment_archived(_url_text(experiment_id), archived=archived))
                return
            experiment_id = _match(path, '/api/platform/experiments/([^/]+)/runs')
            if experiment_id:
                run = self.platform.launch_run(_url_text(experiment_id))
                self._json(run, status=HTTPStatus.ACCEPTED)
                return
            run_id = _match(path, '/api/platform/runs/([^/]+)/interrupt')
            resume_id = _match(path, '/api/platform/runs/([^/]+)/resume')
            if resume_id:
                self._json(self.platform.resume_run(_url_text(resume_id)), status=HTTPStatus.ACCEPTED)
                return
            if run_id:
                self._json(self.platform.interrupt_run(_url_text(run_id)), status=HTTPStatus.ACCEPTED)
                return
            run_id = _match(path, '/api/platform/runs/([^/]+)/metrics')
            if run_id:
                metrics = payload.get('metrics')
                if not isinstance(metrics, dict):
                    raise ValueError('metrics must be an object')
                self._json({'metrics': self.platform.save_metrics(_url_text(run_id), metrics, source=str(payload.get('source') or 'analysis'))})
                return
            self._json_error(HTTPStatus.NOT_FOUND, 'not_found', f'Unknown path: {path}')
        except Exception as exc:
            self._json_exception(exc)

    def do_PUT(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip('/') or '/'
        try:
            experiment_id = _match(path, '/api/platform/experiments/([^/]+)')
            if not experiment_id:
                self._json_error(HTTPStatus.NOT_FOUND, 'not_found', f'Unknown path: {path}')
                return
            payload = self._read_json()
            config = payload.get('config')
            if not isinstance(config, dict):
                raise ValueError('config must be an object')
            group_id = payload.get('group_id')
            if group_id is not None and (not isinstance(group_id, str)):
                raise ValueError('group_id must be a string')
            self.platform.assert_registered_resource_config(config, group_id=group_id, experiment_id=_url_text(experiment_id))
            experiment = self.platform.save_experiment(experiment_id=_url_text(experiment_id), group_id=group_id, name=str(payload.get('name') or ''), description=str(payload.get('description') or ''), config=config, create_only=False)
            self._json(experiment)
        except Exception as exc:
            self._json_exception(exc)

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        try:
            grouped_experiment = _match(path, '/api/platform/experiment-groups/([^/]+)/experiments/([^/]+)')
            if grouped_experiment:
                group_id, experiment_id = grouped_experiment
                self._json(self.platform.delete_experiment_from_group(_url_text(group_id), _url_text(experiment_id)))
                return
            group_id = _match(path, '/api/platform/experiment-groups/([^/]+)')
            if group_id:
                self._json(self.platform.delete_experiment_group(_url_text(group_id)))
                return
            experiment_id = _match(path, '/api/platform/experiments/([^/]+)')
            if experiment_id:
                decoded_id = _url_text(experiment_id)
                self._json(self.platform.delete_experiment(decoded_id))
                return
            run_id = _match(path, '/api/platform/runs/([^/]+)')
            if not run_id:
                self._json_error(HTTPStatus.NOT_FOUND, 'not_found', f'Unknown path: {path}')
                return
            self._json(self.platform.delete_run(_url_text(run_id)))
        except Exception as exc:
            self._json_exception(exc)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _read_json(self, *, max_bytes: int | None=MAX_REQUEST_BYTES) -> dict[str, Any]:
        raw_length = self.headers.get('Content-Length', '0')
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError('invalid Content-Length') from exc
        if length < 0 or (max_bytes is not None and length > max_bytes):
            raise ValueError('request body is too large')
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('request body must be valid UTF-8 JSON') from exc
        if not isinstance(payload, dict):
            raise ValueError('request body must be a JSON object')
        return payload

    def _send_static(self, rel_path: str) -> None:
        candidate = (PLATFORM_STATIC_DIR / rel_path).resolve()
        if PLATFORM_STATIC_DIR.resolve() not in candidate.parents:
            self._json_error(HTTPStatus.NOT_FOUND, 'not_found', 'Unknown static file')
            return
        content_type, _ = mimetypes.guess_type(candidate.name)
        self._send_file(candidate, content_type or 'application/octet-stream')

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._json_error(HTTPStatus.NOT_FOUND, 'not_found', f'Missing file: {path.name}')
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, *, status: HTTPStatus=HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json({'error': {'code': code, 'message': message}}, status=status)

    def _json_exception(self, exc: Exception) -> None:
        if isinstance(exc, KeyError):
            self._json_error(HTTPStatus.NOT_FOUND, 'not_found', str(exc.args[0]))
        elif isinstance(exc, FileExistsError):
            self._json_error(HTTPStatus.CONFLICT, 'already_exists', str(exc))
        elif isinstance(exc, PermissionError):
            self._json_error(HTTPStatus.FORBIDDEN, 'forbidden', str(exc))
        elif isinstance(exc, (ValueError, TypeError, zipfile.BadZipFile)):
            self._json_error(HTTPStatus.BAD_REQUEST, 'invalid_request', str(exc))
        else:
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, 'internal_error', str(exc))

    def _cors_headers(self) -> None:
        if self._memory_api(urllib.parse.urlparse(self.path).path):
            origin = self.headers.get('Origin')
            if origin and urllib.parse.urlsplit(origin).netloc == self.headers.get('Host'):
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            return
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')

    @staticmethod
    def _memory_api(path):
        return path.startswith(('/api/platform/memory-accounts', '/api/platform/memories', '/api/platform/memory-jobs', '/api/platform/memory-replicas')) or path.endswith('/memory-result') or path == '/api/platform/memory-preview'

    def _assert_memory_origin(self):
        origin = self.headers.get('Origin')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' or (origin and urllib.parse.urlsplit(origin).netloc != self.headers.get('Host')):
            raise PermissionError("memory account operations require the platform's own origin")

def create_platform_server(host: str, port: int, platform: ExperimentPlatform) -> ThreadingHTTPServer:

    class BoundHandler(PlatformRequestHandler):
        pass
    BoundHandler.platform = platform
    return ThreadingHTTPServer((host, port), BoundHandler)

def _is_platform_page(path: str) -> bool:
    if path == '/experiments/quick':
        return False
    return bool(path == '/' or path in {'/experiment-groups', '/experiment-groups/new', '/experiments', '/experiments/new', '/settings', '/memories', '/memory-accounts', '/runs', '/resources', '/comparisons'} or re.fullmatch('/experiment-groups/[^/]+', path) or re.fullmatch('/experiment-groups/[^/]+/quick', path) or re.fullmatch('/experiments/[^/]+', path) or re.fullmatch('/runs/[^/]+', path))

def _match(path: str, pattern: str) -> str | tuple[str, ...] | None:
    match = re.fullmatch(pattern, path)
    if not match:
        return None
    groups = match.groups()
    return groups[0] if len(groups) == 1 else groups

def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None

def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    value = _first(query, key)
    return default if value is None else int(value)

def _query_bool(query: dict[str, list[str]], key: str, default: bool) -> bool:
    value = _query_optional_bool(query, key)
    return default if value is None else value

def _query_optional_bool(query: dict[str, list[str]], key: str) -> bool | None:
    value = _first(query, key)
    if value is None or value == '':
        return None
    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes'}:
        return True
    if normalized in {'0', 'false', 'no'}:
        return False
    raise ValueError(f'{key} must be true or false')

def _query_archive_filter(query: dict[str, list[str]]) -> bool | None:
    value = _first(query, 'archived')
    if value is None or value == '':
        return False
    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes', 'only'}:
        return True
    if normalized in {'0', 'false', 'no', 'exclude'}:
        return False
    if normalized in {'all', 'include'}:
        return None
    raise ValueError('archived must be true, false, or all')

def _url_text(value: str) -> str:
    return urllib.parse.unquote(value)

def _config_schema() -> dict[str, Any]:
    return {'version': 1, 'runner_types': [{'value': 'moltnet_thread', 'label_zh': 'Single-post file reading', 'label_en': 'Single-post file reading'}, {'value': 'moltnet_forum', 'label_zh': 'Multi-post file reading', 'label_en': 'Multi-post file reading'}, {'value': 'tool_selection', 'label_zh': 'Stage 2: single-agent tool selection', 'label_en': 'Stage 2: single-agent tool selection'}], 'memory_strategies': [{'value': strategy, **_MEMORY_STRATEGY_LABELS[strategy]} for strategy in SUPPORTED_MEMORY_STRATEGIES], 'models': [{'value': model, 'label': model} for model in SELECTABLE_EXPERIMENT_MODELS], 'api_key_positions': [{'value': position, 'label_zh': label_zh, 'label_en': label_en} for position, label_zh, label_en in (('first', 'Beginning of memory', 'Beginning of memory'), ('middle', 'Middle of memory', 'Middle of memory'), ('last', 'End of memory', 'End of memory'), ('message', 'Separate user message', 'Separate user message'), ('query_suffix', 'After user query (same message)', 'After user query (same message)')) if position in API_KEY_POSITIONS], 'simulation_modes': [{'value': 'read_only', 'label_zh': 'Read only', 'label_en': 'Read only'}], 'post_selection_modes': [{'value': 'file', 'label_zh': 'Complete file data', 'label_en': 'Complete file data'}], 'metric_contract': {'endpoint': 'POST /api/platform/runs/{run_id}/metrics', 'shape': {'metrics': {'metric.key': 'JSON value'}, 'source': 'analysis'}}}

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run the experiment management platform.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8780)
    parser.add_argument('--platform-db', type=Path, default=DEFAULT_PLATFORM_DB)
    parser.add_argument('--default-config', type=Path, default=DEFAULT_EXPERIMENT_CONFIG, help='YAML template used for new experiments')
    return parser

def main() -> None:
    args = build_parser().parse_args()
    platform = ExperimentPlatform(project_root=PROJECT_ROOT, platform_db=args.platform_db, default_config_path=args.default_config)
    server = create_platform_server(args.host, args.port, platform)
    print(f'Experiment platform: http://{args.host}:{args.port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Stopping experiment platform...', flush=True)
    finally:
        server.server_close()
if __name__ == '__main__':
    main()
__all__ = ['PlatformRequestHandler', 'create_platform_server', 'main']
