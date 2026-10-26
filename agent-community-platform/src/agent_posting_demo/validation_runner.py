"""Persisted, single-agent tool selection using frozen first-stage memory."""

from __future__ import annotations

from agent_posting_core.cloud_memory import CLOUD_STRATEGIES
from .cloud_config import build_memory_read_router, cloud_runtime, memory_scope_run_id

import asyncio
import copy
import json
import secrets
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_posting_core.llm import (
    LLMCallManager, LLMClient, ManagedLLMClient, OpenAICompatibleTransport,
)
from agent_posting_core.simulation import AgentProfile
from agent_posting_core.tool_selection import API_KEY_MEMORY_NOTE, evaluate_query

from .experiment_config import ExperimentConfig
from .validation_metrics import (
    EXACT_LEAK_METRIC,
    attach_exact_leak_evidence,
    category_statistics,
    validation_statistics,
)


class ToolSelectionRunner:
    """Run each query independently; never write to the source run or its memory."""

    def __init__(
        self, config: ExperimentConfig, *, llm: LLMClient | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if config.environment.kind != "tool_selection" or config.validation is None:
            raise ValueError("ToolSelectionRunner requires a second-stage configuration")
        self.config = config
        self.llm = llm
        self.manager: LLMCallManager | None = None
        self.on_progress = on_progress
        self.manifest: dict[str, Any] = {}
        self.run_dir: Path | None = None
        self._summaries: list[dict[str, Any]] = []
        self._prior_llm_calls: dict[str, Any] = {}

    def run(self, *, resume: bool = False) -> dict[str, Any]:
        try:
            if resume:
                self._resume()
            else:
                self._prepare()
            self._initialize_llm()
            asyncio.run(self._evaluate())
            return dict(self.manifest)
        except BaseException as exc:
            if self.manifest:
                self.manifest.update(
                    status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                    completed_at=_now(), error=str(exc),
                )
                self._save_manifest()
                self._event("validation_failed", {"error": str(exc)})
            raise

    def _prepare(self) -> None:
        validation = self.config.validation
        assert validation is not None
        source = validation["memory_source"]
        run_id = self.config.run.run_id or (
            f"tool-selection-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        )
        self.run_dir = self.config.output.root_dir / run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        (self.run_dir / "selections").mkdir()
        shutil.copy2(self.config.config_path, self.run_dir / "config.yaml")
        _write_json(self.run_dir / "config.resolved.json", self.config.public_dict())
        _write_json(self.run_dir / "source-memory.json", source)
        _write_json(self.run_dir / "input-queries.json", validation["queries"]["dataset"])
        _write_json(self.run_dir / "input-mcp.json", validation["mcp"]["dataset"])
        query_rows = validation["queries"]["dataset"]["queries"]
        target_count = sum(
            1 for query in query_rows
            if isinstance(query.get("evaluation"), dict)
            and query["evaluation"].get("target_server")
            and query["evaluation"].get("target_tool")
        )
        self.manifest = {
            "schema_version": 1, "run_id": run_id, "run_name": self.config.run.name,
            "run_dir": str(self.run_dir), "environment_type": "tool_selection",
            "stage": 2, "status": "preparing", "created_at": _now(),
            "source_run_id": source["run_id"], "source_round": source["round_index"],
            "source_agent_id": source["agent_id"], "snapshot_sha256": source["snapshot_sha256"],
            "selected_agent_ids": [source["agent_id"]], "population_agent_count": 1,
            "memory_strategy": source["memory_strategy"],
            "query_count": len(query_rows),
            "completed_queries": 0, "successful_actions": 0, "failed_actions": 0,
            "selection_counts": {}, "current_query_id": None,
            "target_configured_queries": target_count, "target_evaluated_queries": 0,
            "target_hits": 0, "target_misses": 0, "target_hit_rate": None,
            "exact_leak_metric": EXACT_LEAK_METRIC,
            "exact_leak_evaluated_queries": 0, "exact_leak_present_queries": 0,
            "exact_leaks": 0, "exact_leak_target_selected_queries": 0,
            "exact_leak_joint_leaks": 0, "exact_leak_outside_target_leaks": 0,
            "exact_leak_presence_rate": None, "exact_leak_rate": None,
            "exact_leak_target_joint_rate": None,
            "category_statistics": category_statistics(query_rows, []),
            "files": {
                "config": "config.yaml", "resolved_config": "config.resolved.json",
                "source_memory": "source-memory.json", "queries": "input-queries.json",
                "mcp": "input-mcp.json", "selections": "selections.json",
            },
        }
        if (validation.get("memory_account_pool") or {}).get("enabled"):
            self.manifest["memory_account_pool"] = {
                key: value for key, value in validation["memory_account_pool"].items() if key != "registry_db_path"
            }
        _write_json(self.run_dir / "selections.json", {"items": [], "total": 0})
        self._save_manifest()
        self._event("validation_prepared", {
            "source_run_id": source["run_id"], "agent_id": source["agent_id"],
            "query_count": self.manifest["query_count"],
        })

    def _resume(self) -> None:
        validation = self.config.validation
        assert validation is not None
        pool = validation.get("memory_account_pool") or {}
        if not pool.get("enabled") or not self.config.run.run_id:
            raise ValueError("second-stage runs are immutable; only paused memory account runs can resume")
        self.run_dir = self.config.output.root_dir / self.config.run.run_id
        saved = json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))
        if saved.get("run_id") != self.config.run.run_id or saved.get("status") != "paused":
            raise ValueError("only the original paused memory account run can resume")
        previous = json.loads((self.run_dir / "config.resolved.json").read_text(encoding="utf-8"))
        if _resume_identity(previous) != _resume_identity(self.config.public_dict()):
            raise ValueError("memory account resume must keep the original frozen configuration")
        for filename, value in (("source-memory.json", validation["memory_source"]),
                                ("input-queries.json", validation["queries"]["dataset"]),
                                ("input-mcp.json", validation["mcp"]["dataset"])):
            if json.loads((self.run_dir / filename).read_text(encoding="utf-8")) != value:
                raise ValueError("memory account resume frozen input has changed")
        summaries = []
        for index, query in enumerate(validation["queries"]["dataset"]["queries"], 1):
            path = self.run_dir / "selections" / f"query-{index:04d}.json"
            if not path.is_file():
                continue
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("query_id") != query["query_id"] or item.get("snapshot_sha256") != validation["memory_source"]["snapshot_sha256"]:
                raise ValueError("saved query does not match the resumed frozen input")
            if item.get("status") in {"completed", "failed"}:
                summaries.append(_summary(item))
        self.manifest = saved
        self._prior_llm_calls = copy.deepcopy(saved.get("llm_calls") or {})
        self._summaries = summaries
        self.manifest.update(validation_statistics(validation["queries"]["dataset"]["queries"], self._summaries))
        _write_json(self.run_dir / "selections.json", {"items": self._summaries, "total": len(self._summaries)})
        self.manifest.setdefault("resume_attempts", []).append({"at": _now(), "completed_queries": len(self._summaries)})
        self._event("validation_resumed", {"completed_queries": len(self._summaries)})

    def _initialize_llm(self) -> None:
        if self.llm is not None:
            return
        backend = self.config.backend
        if not backend.api_key:
            raise ValueError(f"LLM API key is empty; set {backend.api_key_env}")
        self.manager = LLMCallManager(1)
        self.llm = ManagedLLMClient(
            OpenAICompatibleTransport(
                api_base=backend.api_base, api_key=backend.api_key, model=backend.model,
                timeout=backend.timeout_seconds, temperature=backend.temperature,
                max_tokens=backend.max_tokens, max_transient_retries=backend.max_transient_retries,
                retry_backoff_seconds=backend.retry_backoff_seconds,
                max_retry_wait_seconds=backend.max_retry_wait_seconds,
                min_request_interval_seconds=backend.min_request_interval_seconds,
            ), self.manager,
        )

    async def _evaluate(self) -> None:
        assert self.config.validation is not None and self.llm is not None
        assert self.run_dir is not None
        validation = self.config.validation
        source = validation["memory_source"]
        profile = AgentProfile(**source["profile"])
        memory_runtime = None
        memory_reader = build_memory_read_router(
            source, validation.get("memory_account_pool"), on_attempt=self._memory_attempt,
        )
        if memory_reader is not None:
            pass
        elif source["memory_strategy"] in {"hindsight_cloud", "mem0_platform_v3", "memos_cloud"}:
            raise ValueError("Stage-two cloud memory requires experiment accounts. Import the selected memory from the default account first.")
        elif source["memory_strategy"] in CLOUD_STRATEGIES:
            memory_runtime = cloud_runtime(
                source["memory_strategy"], memory_scope_run_id(source),
                api_keys=self.config.backend.cloud_api_keys, read_only=True,
            )
        elif source["memory_strategy"] == "langmem_official":
            from agent_posting_core.langmem_official import uses_local_embeddings

            backend = self.config.backend
            local_embeddings = uses_local_embeddings(
                source["snapshot"]["options"]["embedding_model"]
            )
            if not local_embeddings and not backend.api_key:
                raise ValueError(
                    f"LangMem embedding API key is empty; set {backend.api_key_env}"
                )
            memory_runtime = {
                "api_base": backend.api_base,
                "api_key": backend.api_key,
                "model": backend.model,
                "run_id": memory_scope_run_id(source),
                "request_timeout_seconds": backend.timeout_seconds,
                "max_retries": backend.max_transient_retries,
                "memory_max_tokens": backend.max_tokens,
            }
            if local_embeddings:
                for key in ("api_base", "api_key", "model"):
                    memory_runtime.pop(key, None)
        summaries = self._summaries
        completed_ids = {item["query_id"] for item in summaries}
        self.manifest.update(status="running", started_at=self.manifest.get("started_at") or _now(), error=None,
                             completed_at=None)
        self._save_manifest()
        for index, query in enumerate(validation["queries"]["dataset"]["queries"], start=1):
            if query["query_id"] in completed_ids:
                continue
            self.manifest["current_query_id"] = query["query_id"]
            self.manifest["current_query_index"] = index
            self._save_manifest()
            self._event("query_started", {"query_id": query["query_id"], "query_index": index})
            result = await evaluate_query(
                profile=profile, memory_snapshot=source["snapshot"],
                memory_options=self.config.memory.options, query=query,
                catalog=validation["mcp"]["dataset"], llm=self.llm, seed=self.config.run.seed,
                memory_runtime=memory_runtime,
                memory_reader=memory_reader,
                memory_suffix=validation.get("memory_suffix", ""),
                api_key_position=validation.get("api_key_position", "middle"),
                insertion_payload=(
                    validation["custom_payload"]
                    if validation.get("custom_payload_enabled")
                    else API_KEY_MEMORY_NOTE
                ),
                tool_choice="auto",
                injection_enabled=validation.get("injection_enabled", True),
                mcp_parameter_additions=validation.get("mcp_parameter_additions"),
            )
            result.update(
                query_index=index, source_run_id=source["run_id"],
                source_round=source["round_index"], snapshot_sha256=source["snapshot_sha256"],
                completed_at=_now(),
            )
            if result["status"] == "pending_memory":
                result["paused_at"] = result.pop("completed_at")
                _write_json(self.run_dir / "selections" / f"query-{index:04d}.json", result)
                self.manifest.update(status="paused", pause_reason="memory_accounts_unavailable",
                                     paused_at=_now(), error=result.get("error"),
                                     memory_read_audit=result.get("memory_read_audit"), can_resume_memory_accounts=True)
                self._save_manifest()
                self._event("validation_paused", {"query_id": query["query_id"],
                            "reason": "memory_accounts_unavailable", "memory_read_audit": result.get("memory_read_audit")})
                return
            attach_exact_leak_evidence(
                result,
                expected_value=validation["expected_leak_value"],
                argument_path=validation["leak_argument_path"],
                insertion_payload=((validation["custom_payload"]
                                   if validation["custom_payload_enabled"] else API_KEY_MEMORY_NOTE)
                                   if validation.get("injection_enabled", True) else ""),
            )
            _write_json(self.run_dir / "selections" / f"query-{index:04d}.json", result)
            summaries.append(_summary(result))
            _write_json(self.run_dir / "selections.json", {"items": summaries, "total": len(summaries)})
            self.manifest.update(validation_statistics(
                validation["queries"]["dataset"]["queries"], summaries,
            ))
            self._save_manifest()
            self._event("query_completed", {
                "query_id": query["query_id"], "status": result["status"],
                "functional_group": result.get("functional_group"),
                "selected_server": result.get("selected_server"),
                "selected_tool": result.get("selected_tool"),
                "target_server": result.get("target_server"),
                "target_tool": result.get("target_tool"),
                "target_hit": result.get("target_hit"),
                "exact_leak_present": bool(
                    (result.get("exact_leak_evidence") or {}).get(
                        "argument_present"
                    )
                ),
                "exact_leak": bool(
                    (result.get("exact_leak_evidence") or {}).get(
                        "exact_match"
                    )
                ),
                "evaluation_status": result.get("evaluation_status"),
                "error": result.get("error"),
                "behavior_llm_calls": result.get("behavior_llm_calls"),
                "response_retries": result.get("response_retries"),
                "memory_account_id": result.get("memory_account_id"),
            })
        succeeded = self.manifest["successful_actions"]
        failed = self.manifest["failed_actions"]
        status = "completed_with_errors" if succeeded and failed else "completed" if succeeded else "failed"
        self.manifest.update(
            status=status, completed_at=_now(), current_query_id=None, can_resume_memory_accounts=False,
            pause_reason=None,
            error="all query selections failed; inspect the selection results" if not succeeded else None,
        )
        self._save_manifest()
        self._event("validation_completed", {
            "status": status, "completed_queries": len(summaries),
            "target_hits": self.manifest["target_hits"],
            "target_misses": self.manifest["target_misses"],
            "target_hit_rate": self.manifest["target_hit_rate"],
            "exact_leaks": self.manifest["exact_leaks"],
            "exact_leak_evaluated_queries": self.manifest["exact_leak_evaluated_queries"],
            "exact_leak_rate": self.manifest["exact_leak_rate"],
        })

    def _memory_attempt(self, audit):
        self.manifest["memory_read_audit"] = audit
        self._save_manifest()
        self._event("memory_account_attempt", {"query_id": self.manifest.get("current_query_id"),
                                               "audit": audit})

    def _save_manifest(self) -> None:
        assert self.run_dir is not None
        stats = self.manager.stats() if self.manager else {}
        if self._prior_llm_calls:
            prior = self._prior_llm_calls
            stats["total_calls"] = stats.get("total_calls", 0) + prior.get("total_calls", 0)
            stats["peak_in_flight"] = max(stats.get("peak_in_flight", 0), prior.get("peak_in_flight", 0))
            current = stats.get("by_purpose", {})
            stats["by_purpose"] = {key: current.get(key, 0) + prior.get("by_purpose", {}).get(key, 0)
                                   for key in set(current) | set(prior.get("by_purpose", {}))}
        self.manifest["llm_calls"] = stats
        _write_json(self.run_dir / "manifest.json", self.manifest)
        if self.on_progress:
            self.on_progress(dict(self.manifest))

    def _event(self, event: str, details: dict[str, Any]) -> None:
        if self.run_dir is not None:
            with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": event, "at": _now(), **details}, ensure_ascii=False) + "\n")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _summary(result):
    return {key: value for key, value in result.items() if key not in {
        "request", "raw_response", "memory_context", "response_attempts",
    }}


def _resume_identity(value):
    result = copy.deepcopy(value)
    result.pop("config_path", None)
    for name in ("api_key_configured", "mem0_api_key_configured"):
        result.get("backend", {}).pop(name, None)
    pool = result.get("validation", {}).get("memory_account_pool", {})
    pool.pop("registry_db_path", None)
    if pool.get("shared") or result.get("validation", {}).get("memory_source", {}).get("memory_strategy") in {
            "hindsight_cloud", "mem0_platform_v3", "memos_cloud"}:
        pool.pop("account_ids", None)
        pool.pop("shared", None)
    return result
