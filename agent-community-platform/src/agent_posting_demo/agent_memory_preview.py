"""Persist an output-only, per-thread first-stage memory decision experiment."""
from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_posting_core.agent_memory_preview import (
    HERMES_REVISION, HERMES_PROMPT_SOURCE, HERMES_SCHEMA_SOURCE,
    build_preview_request, parse_preview_response,
)
from agent_posting_core.forum_simulation import forum_snapshot
from agent_posting_core.llm import LLMCallManager, ManagedLLMClient, OpenAICompatibleTransport
from agent_posting_core.post_selection_file import import_post_dataset, select_file_reader
from agent_posting_core.simulation import ForumStore
from agent_posting_core.thread_simulation import thread_memory_events

from .experiment_config import ExperimentConfig
from .memory_results import TracingLLMClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


class AgentMemoryPreviewRunner:
    def __init__(self, config: ExperimentConfig, *, llm: Any = None, on_progress: Any = None):
        self.config = config
        self.llm = llm
        self.on_progress = on_progress
        self.manifest: dict[str, Any] = {}
        self.run_dir: Path | None = None

    def _progress(self, **changes: Any) -> None:
        self.manifest.update(changes)
        assert self.run_dir is not None
        _write(self.run_dir / "manifest.json", self.manifest)
        with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": "memory_preview_progress", "at": _now(),
                                     "completed_posts": self.manifest["completed_posts"],
                                     "status": self.manifest["status"]}, ensure_ascii=False) + "\n")
        if self.on_progress:
            self.on_progress(dict(self.manifest))

    def run(self, *, resume: bool = False) -> dict[str, Any]:
        if resume:
            raise ValueError("memory preview runs cannot be resumed; start a new test")
        config = self.config
        run_id = config.run.run_id or f"memory-preview-{uuid.uuid4().hex[:12]}"
        self.run_dir = config.output.root_dir / run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        for name in ("memory", "facts", "rounds", "snapshots"):
            (self.run_dir / name).mkdir()
        shutil.copy2(config.config_path, self.run_dir / "config.yaml")
        _write(self.run_dir / "config.resolved.json", config.public_dict())
        imported = config.environment.imported_file
        assert imported is not None
        dataset = imported["dataset"]
        _write(self.run_dir / "input-posts.json", dataset)
        self.manifest = {
            "schema_version": 1, "run_id": run_id, "run_name": config.run.name,
            "run_dir": str(self.run_dir.resolve()), "status": "preparing", "created_at": _now(),
            "environment_type": config.environment.kind, "simulation_mode": "read_only",
            "memory_strategy": "agent_memory_preview", "memory_model": config.memory.model,
            "preview_only": True, "actual_memory_writes": 0, "memory_source_usable": False,
            "data_source": "file", "input_file": imported["filename"],
            "hermes_revision": HERMES_REVISION,
            "prompt_sources": [HERMES_PROMPT_SOURCE, HERMES_SCHEMA_SOURCE],
            "target_rounds": 1, "completed_rounds": [], "error_rounds": [],
            "activation_mode": "all", "population_agent_count": 1, "active_agents_per_round": 1,
            "post_count": len(dataset["posts"]), "completed_posts": 0,
            "save_decisions": 0, "skip_decisions": 0, "fact_count": 0,
            "successful_actions": 0, "failed_actions": 0, "action_counts": {},
            "files": {"config": "config.yaml", "resolved_config": "config.resolved.json",
                      "database": "simulation.sqlite", "events": "events.jsonl",
                      "input_posts": "input-posts.json", "fact_outputs": "facts"},
        }
        self._progress()
        store = None
        try:
            store = ForumStore(self.run_dir / "simulation.sqlite")
            selected = select_file_reader(dataset, agent_id=config.simulation.read_only_agent_id,
                                          require_identity=config.agents.require_identity)
            restored = import_post_dataset(store, dataset, selected_ids=selected,
                                           history_limit=config.agents.history_limit)
            post_ids = [item["post_id"] for item in restored["imports"]]
            reader_id = restored["agents"]["agents"][0]["agent_id"]
            threads = [store.get_thread(post_id) for post_id in post_ids]
            communities = list(dict.fromkeys(post["community"] for post in dataset["posts"]))
            kind = "thread" if config.environment.kind == "moltnet_thread" else "forum"
            environment = (threads[0] if kind == "thread" else
                           forum_snapshot(store, communities, post_limit=len(post_ids)))
            _write(self.run_dir / "snapshots" / f"{kind}-initial.json", environment)
            _write(self.run_dir / "selection.json", {"source": "file", "filename": imported["filename"], **restored})
            self.manifest.update({
                "post_ids": post_ids, "source_post_ids": [post["id"] for post in dataset["posts"]],
                "selected_agent_ids": [reader_id], "communities": communities,
                "initial_post_count": len(post_ids), "latest_post_count": len(post_ids),
                "initial_comment_count": sum(len(thread["comments"]) for thread in threads),
                "latest_comment_count": sum(len(thread["comments"]) for thread in threads),
            })
            if self.llm is None:
                if not config.backend.api_key:
                    raise ValueError(f"LLM API key is empty; set {config.backend.api_key_env}")
                backend = config.backend
                self.llm = ManagedLLMClient(OpenAICompatibleTransport(
                    api_base=backend.api_base, api_key=backend.api_key,
                    model=config.memory.model or backend.model, timeout=backend.timeout_seconds,
                    temperature=backend.temperature, max_tokens=backend.max_tokens,
                    max_transient_retries=backend.max_transient_retries,
                    retry_backoff_seconds=backend.retry_backoff_seconds,
                    max_retry_wait_seconds=backend.max_retry_wait_seconds,
                    min_request_interval_seconds=backend.min_request_interval_seconds,
                ), LLMCallManager(backend.concurrency))
            self.llm = TracingLLMClient(self.llm, self.run_dir / "memory" / "model-calls.jsonl")
            self._progress(status="running")
            records = asyncio.run(self._read(threads, reader_id, store.get_agent(reader_id)))
            _write(self.run_dir / "snapshots" / f"{kind}-round-001.json", environment)
            _write(self.run_dir / "rounds" / "round-001.json", {
                "round_index": 1, "simulation_mode": "read_only", "preview_only": True,
                "environment_type": config.environment.kind, "memory_strategy": "agent_memory_preview",
                "post_ids": post_ids, "active_agent_ids": [reader_id],
                "executions": [{"agent_id": reader_id, "action": "memory_fact_preview",
                                "ok": row["ok"], "source_post_id": row["source_post_id"],
                                "preview_only": True, "actual_memory_writes": 0,
                                **({"error": row["error"]} if not row["ok"] else {})}
                               for row in records],
            })
            errors = self.manifest["failed_actions"]
            status = ("completed" if not errors else "completed_with_errors"
                      if self.manifest["successful_actions"] else "failed")
            self._progress(status=status, completed_rounds=[1],
                           error_rounds=[1] if errors else [], latest_round=1, completed_at=_now())
            return dict(self.manifest)
        except BaseException as exc:
            self._progress(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                           error=str(exc), completed_at=_now())
            raise
        finally:
            if store is not None:
                store.close()

    async def _read(self, threads: list[dict[str, Any]], reader_id: str,
                    profile: dict[str, Any]) -> list[dict[str, Any]]:
        records = []
        proposals: list[dict[str, Any]] = []
        assert self.run_dir is not None
        for index, thread in enumerate(threads, 1):
            events = thread_memory_events(thread, hide_speaker=self.config.environment.hide_speaker,
                                          speaker_as_json=self.config.environment.speaker_as_json)
            source_id = self.manifest["source_post_ids"][index - 1]
            request = build_preview_request(
                agent_id=reader_id,
                profile={"id": reader_id, "name": profile["display_name"], "persona": profile["persona"]},
                thread={"post_id": source_id, "title": thread["post"]["title"],
                        "messages": [{"kind": event.kind, "content": event.content} for event in events]},
                previous_proposals=proposals[-self.config.agents.history_limit:],
            )
            record = {"index": index, "source_post_id": source_id, "post_id": thread["post"]["post_id"],
                      "agent_id": reader_id, "at": _now(), "preview_only": True,
                      "actual_memory_writes": 0, "request": asdict(request)}
            try:
                response = await self.llm.complete(request)
                record.update(response_content=response.content, reasoning_content=response.reasoning_content,
                              raw_response=response.raw,
                              tool_calls=[asdict(call) for call in response.tool_calls])
                result = parse_preview_response(response)
                record.update(ok=True, **result)
                proposals.append({"post_id": source_id, **result})
                self.manifest["successful_actions"] += 1
                self.manifest[result["decision"] + "_decisions"] += 1
                self.manifest["fact_count"] += len(result["facts"])
            except Exception as exc:
                record.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                self.manifest["failed_actions"] += 1
            records.append(record)
            _write(self.run_dir / "facts" / f"post-{index:06d}.json", record)
            self._progress(completed_posts=index, action_counts={"memory_fact_preview": len(records)})
        return records


def preview_result(run_dir: Path, *, limit: int, offset: int) -> dict[str, Any]:
    paths = sorted((run_dir / "facts").glob("post-*.json"))
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    return {
        "available": True, "preview_only": True, "actual_memory_writes": 0,
        "items": [json.loads(path.read_text(encoding="utf-8")) for path in paths[offset:offset + limit]],
        "total": len(paths), "limit": limit, "offset": offset,
        "has_more": offset + limit < len(paths), "model_calls": [], "calls_total": 0,
        "fact_count": manifest.get("fact_count", 0), "save_decisions": manifest.get("save_decisions", 0),
        "skip_decisions": manifest.get("skip_decisions", 0), "failed_decisions": manifest.get("failed_actions", 0),
        "post_count": manifest.get("post_count", 0),
        "prompt_sources": manifest.get("prompt_sources", [HERMES_PROMPT_SOURCE, HERMES_SCHEMA_SOURCE]),
        "hermes_revision": manifest.get("hermes_revision", HERMES_REVISION),
    }
