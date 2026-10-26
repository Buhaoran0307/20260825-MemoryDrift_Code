from __future__ import annotations

import argparse
import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .experiment import ExperimentRunner
from .experiment_config import load_experiment_config
from .platform_store import PlatformStore, TERMINAL_RUN_STATUSES
from .validation_runner import ToolSelectionRunner


SUPPORTED_RUNNERS = {"moltnet_thread", "moltnet_forum", "tool_selection"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute one registered experiment run in a background worker."
    )
    parser.add_argument("--platform-db", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", type=Path, required=True)
    return parser


def run_worker(
    *,
    platform_db: Path,
    run_id: str,
    config_path: Path,
) -> dict[str, Any]:
    store = PlatformStore(platform_db)
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(f"Unknown platform run: {run_id}")
    run_dir = Path(run["output_dir"])
    existing_manifest = _read_manifest(run_dir / "manifest.json")
    resumable_accounts = bool(existing_manifest and existing_manifest.get("status") == "paused"
                              and existing_manifest.get("can_resume_memory_accounts"))
    if run.get("runner_type") == "tool_selection" and existing_manifest is not None and not resumable_accounts:
        if str(existing_manifest.get("run_id") or "") != run_id:
            raise ValueError(
                "second-stage manifest identity does not match the registered run"
            )
        existing_status = str(existing_manifest.get("status") or "")
        if existing_status not in TERMINAL_RUN_STATUSES:
            raise RuntimeError(
                "second-stage runs are immutable and an existing validation "
                "manifest cannot be resumed"
            )
        metrics = _manifest_metrics(existing_manifest)
        store.save_metrics(run_id, metrics, source="runner")
        store.update_run(
            run_id,
            status=existing_status,
            completed_at=existing_manifest.get("completed_at") or _now(),
            error=existing_manifest.get("error"),
        )
        print(
            json.dumps(
                {
                    "event": "worker_start_skipped",
                    "run_id": run_id,
                    "runner_type": "tool_selection",
                    "status": existing_status,
                    "reason": "terminal manifest already exists",
                    "at": _now(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return existing_manifest
    store.update_run(run_id, status="starting", started_at=_now(), error=None)
    try:
        config = load_experiment_config(config_path, project_root=PROJECT_ROOT)
        if (config.validation is not None and config.validation["memory_source"]["memory_strategy"]
                in {"hindsight_cloud", "mem0_platform_v3", "memos_cloud"}):
            from .cloud_config import build_memory_read_router
            pool = config.validation.setdefault("memory_account_pool", {"enabled": True, "account_ids": []})
            pool.update(enabled=True, registry_db_path=str(platform_db.resolve()))
            pool.setdefault("shared", True)
            router = build_memory_read_router(config.validation["memory_source"], pool,
                registry_db_path=platform_db.resolve())
            config.validation["memory_account_pool"] = router.freeze_pool()
        resume = (Path(run["output_dir"]) / "manifest.json").is_file()
        runner_type = config.environment.kind
        if runner_type not in SUPPORTED_RUNNERS:
            raise ValueError(f"Unsupported experiment runner: {runner_type}")
        print(
            json.dumps(
                {
                    "event": "worker_started",
                    "run_id": run_id,
                    "runner_type": runner_type,
                    "resume": resume,
                    "at": _now(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        last_status: str | None = None

        def progress(manifest: dict[str, Any]) -> None:
            nonlocal last_status
            status = str(manifest.get("status") or "running")
            if status == "running" and last_status != "running":
                store.update_run(run_id, status="running")
            last_status = status
            print(
                json.dumps(
                    {
                        "event": "run_progress",
                        "run_id": run_id,
                        "status": status,
                        "completed_rounds": len(manifest.get("completed_rounds") or []),
                        "target_rounds": manifest.get("target_rounds"),
                        "completed_queries": manifest.get("completed_queries"),
                        "query_count": manifest.get("query_count"),
                        "successful_actions": manifest.get("successful_actions", 0),
                        "failed_actions": manifest.get("failed_actions", 0),
                        "target_hits": manifest.get("target_hits"),
                        "target_misses": manifest.get("target_misses"),
                        "target_hit_rate": manifest.get("target_hit_rate"),
                        "at": _now(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        runner_class = ToolSelectionRunner if runner_type == "tool_selection" else ExperimentRunner
        runner_options = {"on_progress": progress}
        if runner_type != "tool_selection":
            runner_options["memory_account_registry_db_path"] = platform_db
        manifest = runner_class(config, **runner_options).run(resume=resume)
        metrics = _manifest_metrics(manifest)
        store.save_metrics(run_id, metrics, source="runner")
        store.update_run(
            run_id,
            status=str(manifest["status"]),
            output_dir=str(Path(manifest["run_dir"]).resolve()),
            completed_at=manifest.get("completed_at") or _now(),
            error=manifest.get("error"),
        )
        print(
            json.dumps(
                {
                    "event": "worker_completed",
                    "run_id": run_id,
                    "status": manifest["status"],
                    "metrics": metrics,
                    "at": _now(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return manifest
    except BaseException as exc:
        _mark_manifest_failed(
            Path(run["output_dir"]),
            status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            error=str(exc),
        )
        store.update_run(
            run_id,
            status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            completed_at=_now(),
            error=str(exc),
        )
        traceback.print_exc()
        raise


def _manifest_metrics(manifest: dict[str, Any]) -> dict[str, Any]:
    initial_comments = int(manifest.get("initial_comment_count") or 0)
    latest_comments = int(manifest.get("latest_comment_count") or initial_comments)
    initial_posts = int(manifest.get("initial_post_count") or 0)
    latest_posts = int(manifest.get("latest_post_count") or initial_posts)
    metrics: dict[str, Any] = {
        "run.target_rounds": int(manifest.get("target_rounds") or 0),
        "run.completed_rounds": len(manifest.get("completed_rounds") or []),
        "actions.successful": int(manifest.get("successful_actions") or 0),
        "actions.failed": int(manifest.get("failed_actions") or 0),
        "activation.mode": str(manifest.get("activation_mode") or "all"),
        "activation.population_agents": int(
            manifest.get("population_agent_count") or 0
        ),
        "activation.agents_per_round": int(
            manifest.get("active_agents_per_round") or 0
        ),
        "comments.initial": initial_comments,
        "comments.final": latest_comments,
        "comments.delta": latest_comments - initial_comments,
        "posts.initial": initial_posts,
        "posts.final": latest_posts,
        "posts.delta": latest_posts - initial_posts,
    }
    for action, count in (manifest.get("action_counts") or {}).items():
        metrics[f"actions.{action}"] = int(count)
    if manifest.get("preview_only"):
        for key in ("post_count", "completed_posts", "save_decisions", "skip_decisions", "fact_count", "actual_memory_writes"):
            metrics[f"memory_preview.{key}"] = int(manifest.get(key) or 0)
    if manifest.get("environment_type") == "tool_selection":
        metrics = {
            "validation.query_count": int(manifest.get("query_count") or 0),
            "validation.completed_queries": int(manifest.get("completed_queries") or 0),
            "validation.successful": int(manifest.get("successful_actions") or 0),
            "validation.failed": int(manifest.get("failed_actions") or 0),
            "validation.selection_counts": dict(manifest.get("selection_counts") or {}),
            "validation.target_configured_queries": int(
                manifest.get("target_configured_queries") or 0
            ),
            "validation.target_evaluated_queries": int(
                manifest.get("target_evaluated_queries") or 0
            ),
            "validation.target_hits": int(manifest.get("target_hits") or 0),
            "validation.target_misses": int(manifest.get("target_misses") or 0),
            "validation.target_hit_rate": manifest.get("target_hit_rate"),
            "validation.category_statistics": dict(manifest.get("category_statistics") or {}),
            "validation.source_run_id": manifest.get("source_run_id"),
            "validation.source_agent_id": manifest.get("source_agent_id"),
            "validation.source_round": manifest.get("source_round"),
        }
        if manifest.get("exact_leak_metric"):
            metrics.update({
                "validation.exact_leak_metric": manifest.get(
                    "exact_leak_metric"
                ),
                "validation.exact_leak_evaluated_queries": int(
                    manifest.get("exact_leak_evaluated_queries") or 0
                ),
                "validation.exact_leak_present_queries": int(
                    manifest.get("exact_leak_present_queries") or 0
                ),
                "validation.exact_leaks": int(
                    manifest.get("exact_leaks") or 0
                ),
                "validation.exact_leak_target_selected_queries": int(
                    manifest.get("exact_leak_target_selected_queries") or 0
                ),
                "validation.exact_leak_joint_leaks": int(
                    manifest.get("exact_leak_joint_leaks") or 0
                ),
                "validation.exact_leak_outside_target_leaks": int(
                    manifest.get("exact_leak_outside_target_leaks") or 0
                ),
                "validation.exact_leak_presence_rate": manifest.get(
                    "exact_leak_presence_rate"
                ),
                "validation.exact_leak_rate": manifest.get(
                    "exact_leak_rate"
                ),
                "validation.exact_leak_target_joint_rate": manifest.get(
                    "exact_leak_target_joint_rate"
                ),
            })
    created_at = _parse_time(manifest.get("created_at"))
    completed_at = _parse_time(manifest.get("completed_at"))
    if created_at is not None and completed_at is not None:
        metrics["run.duration_seconds"] = max(
            0.0, (completed_at - created_at).total_seconds()
        )
    return metrics


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _mark_manifest_failed(run_dir: Path, *, status: str, error: str) -> None:
    path = run_dir / "manifest.json"
    if not path.is_file():
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(value, dict):
        return
    value.update({"status": status, "completed_at": _now(), "error": error})
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def main() -> None:
    args = build_parser().parse_args()
    run_worker(
        platform_db=args.platform_db,
        run_id=args.run_id,
        config_path=args.config,
    )


if __name__ == "__main__":
    main()


__all__ = ["build_parser", "run_worker"]
