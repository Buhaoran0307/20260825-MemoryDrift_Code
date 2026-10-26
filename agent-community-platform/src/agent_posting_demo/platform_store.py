from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


TERMINAL_RUN_STATUSES = {
    "paused",
    "completed",
    "completed_with_errors",
    "failed",
    "interrupted",
}

LEGACY_EXPERIMENT_GROUP_ID = "legacy"
LEGACY_EXPERIMENT_GROUP_NAME = "Historical experiments"


class PlatformStore:
    """SQLite registry for reusable experiments, runs, and analysis metrics."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            # WAL is persistent database state. Reapplying it on every short-lived
            # connection is especially expensive on Windows drives mounted in WSL.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiment_groups (
                  group_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  queries_json TEXT,
                  mcp_json TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS experiments (
                  experiment_id TEXT PRIMARY KEY,
                  group_id TEXT NOT NULL DEFAULT 'legacy',
                  name TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  runner_type TEXT NOT NULL DEFAULT 'moltnet_thread',
                  resource_policy TEXT NOT NULL DEFAULT 'legacy',
                  config_path TEXT NOT NULL UNIQUE,
                  config_json TEXT NOT NULL,
                  source_experiment_id TEXT,
                  memory_source_experiment_id TEXT,
                  display_post_filename TEXT,
                  display_environment_type TEXT,
                  display_memory_strategy TEXT,
                  display_hide_speaker INTEGER,
                  display_speaker_as_json INTEGER,
                  display_source_run_id TEXT,
                  summary_version INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  archived_at TEXT,
                  FOREIGN KEY(group_id) REFERENCES experiment_groups(group_id)
                );

                CREATE TABLE IF NOT EXISTS experiment_summaries (
                  experiment_id TEXT PRIMARY KEY,
                  group_id TEXT NOT NULL,
                  name TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  runner_type TEXT NOT NULL,
                  resource_policy TEXT NOT NULL DEFAULT 'legacy',
                  config_path TEXT NOT NULL,
                  source_experiment_id TEXT,
                  memory_source_experiment_id TEXT,
                  display_post_filename TEXT,
                  display_environment_type TEXT,
                  display_memory_strategy TEXT,
                  display_hide_speaker INTEGER,
                  display_speaker_as_json INTEGER,
                  display_source_run_id TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  archived_at TEXT,
                  FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
                    ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS experiment_runs (
                  run_id TEXT PRIMARY KEY,
                  experiment_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  screen_session TEXT,
                  worker_config_path TEXT NOT NULL,
                  output_dir TEXT NOT NULL,
                  log_path TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  started_at TEXT,
                  completed_at TEXT,
                  error TEXT,
                  FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
                    ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_experiment_runs_experiment_created
                ON experiment_runs(experiment_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_experiment_runs_status_created
                ON experiment_runs(status, created_at DESC);

                CREATE TABLE IF NOT EXISTS run_metrics (
                  run_id TEXT NOT NULL,
                  metric_key TEXT NOT NULL,
                  metric_value_json TEXT NOT NULL,
                  source TEXT NOT NULL,
                  recorded_at TEXT NOT NULL,
                  PRIMARY KEY(run_id, metric_key),
                  FOREIGN KEY(run_id) REFERENCES experiment_runs(run_id)
                    ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS platform_resources (
                  resource_id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL CHECK(kind IN ('posts', 'queries', 'mcp')),
                  name TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS platform_target_mappings (
                  mapping_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  filename TEXT NOT NULL,
                  content_json TEXT NOT NULL,
                  content_sha256 TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS platform_resource_revisions (
                  revision_id TEXT PRIMARY KEY,
                  resource_id TEXT NOT NULL,
                  revision_number INTEGER NOT NULL CHECK(revision_number > 0),
                  filename TEXT NOT NULL,
                  environment_type TEXT,
                  content_json TEXT NOT NULL,
                  content_sha256 TEXT NOT NULL,
                  summary_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(resource_id, revision_number),
                  FOREIGN KEY(resource_id) REFERENCES platform_resources(resource_id)
                    ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_resource_revisions_resource_number
                ON platform_resource_revisions(resource_id, revision_number DESC);

                CREATE TABLE IF NOT EXISTS experiment_group_resource_refs (
                  group_id TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('queries', 'mcp')),
                  revision_id TEXT NOT NULL,
                  PRIMARY KEY(group_id, role),
                  FOREIGN KEY(group_id) REFERENCES experiment_groups(group_id)
                    ON DELETE CASCADE,
                  FOREIGN KEY(revision_id) REFERENCES platform_resource_revisions(revision_id)
                    ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS experiment_resource_refs (
                  experiment_id TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('posts', 'queries', 'mcp')),
                  revision_id TEXT NOT NULL,
                  PRIMARY KEY(experiment_id, role),
                  FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
                    ON DELETE CASCADE,
                  FOREIGN KEY(revision_id) REFERENCES platform_resource_revisions(revision_id)
                    ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS run_resource_refs (
                  run_id TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('posts', 'queries', 'mcp')),
                  revision_id TEXT NOT NULL,
                  content_sha256 TEXT NOT NULL,
                  PRIMARY KEY(run_id, role),
                  FOREIGN KEY(run_id) REFERENCES experiment_runs(run_id)
                    ON DELETE CASCADE,
                  FOREIGN KEY(revision_id) REFERENCES platform_resource_revisions(revision_id)
                    ON DELETE RESTRICT
                );
                """
            )
            now = _now()
            connection.execute(
                """
                INSERT OR IGNORE INTO experiment_groups(
                  group_id, name, description, queries_json, mcp_json,
                  created_at, updated_at, archived_at
                ) VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL)
                """,
                (
                    LEGACY_EXPERIMENT_GROUP_ID,
                    LEGACY_EXPERIMENT_GROUP_NAME,
                    "Automatically contains experiments created before groups were introduced.",
                    now,
                    now,
                ),
            )
            experiment_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(experiments)")
            }
            if "runner_type" not in experiment_columns:
                connection.execute(
                    "ALTER TABLE experiments ADD COLUMN runner_type TEXT NOT NULL "
                    "DEFAULT 'moltnet_thread'"
                )
            if "archived_at" not in experiment_columns:
                connection.execute(
                    "ALTER TABLE experiments ADD COLUMN archived_at TEXT"
                )
            if "group_id" not in experiment_columns:
                # SQLite cannot add a non-null REFERENCES column with a non-null
                # default to a populated table. Application writes enforce the
                # relationship for upgraded databases; fresh databases also get
                # the schema-level foreign key declared above.
                connection.execute(
                    "ALTER TABLE experiments ADD COLUMN group_id TEXT"
                )
            if "resource_policy" not in experiment_columns:
                connection.execute(
                    "ALTER TABLE experiments ADD COLUMN resource_policy TEXT NOT NULL "
                    "DEFAULT 'legacy'"
                )
            summary_column_definitions = {
                "source_experiment_id": "TEXT",
                "memory_source_experiment_id": "TEXT",
                "display_post_filename": "TEXT",
                "display_environment_type": "TEXT",
                "display_memory_strategy": "TEXT",
                "display_hide_speaker": "INTEGER",
                "display_speaker_as_json": "INTEGER",
                "display_source_run_id": "TEXT",
                "summary_version": "INTEGER NOT NULL DEFAULT 0",
            }
            group_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(experiment_groups)")
            }
            if "target_mapping_id" not in group_columns:
                connection.execute("ALTER TABLE experiment_groups ADD COLUMN target_mapping_id TEXT")
            for column, definition in summary_column_definitions.items():
                if column not in experiment_columns:
                    connection.execute(
                        f"ALTER TABLE experiments ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                "UPDATE experiments SET group_id = ? WHERE group_id IS NULL OR group_id = ''",
                (LEGACY_EXPERIMENT_GROUP_ID,),
            )
            self._repair_legacy_runner_types_and_summaries(connection)
            self._backfill_experiment_summary_table(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiments_archived_updated "
                "ON experiments(archived_at, updated_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiments_group_updated "
                "ON experiments(group_id, updated_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiment_summaries_archived_updated "
                "ON experiment_summaries(archived_at, updated_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiment_summaries_group_updated "
                "ON experiment_summaries(group_id, updated_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiment_groups_archived_updated "
                "ON experiment_groups(archived_at, updated_at DESC)"
            )
            connection.commit()

    @staticmethod
    def _repair_legacy_runner_types_and_summaries(
        connection: sqlite3.Connection,
    ) -> None:
        """Backfill stage identity and the small fields used by list queries."""
        rows = connection.execute(
            "SELECT experiment_id, runner_type, config_json FROM experiments "
            "WHERE summary_version < 1"
        ).fetchall()
        for row in rows:
            try:
                config = json.loads(str(row["config_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                config = {}
            environment = config.get("environment") if isinstance(config, dict) else None
            inferred = (
                environment.get("type")
                if isinstance(environment, dict)
                else None
            )
            if not isinstance(inferred, str) or not inferred.strip():
                inferred = (
                    "tool_selection"
                    if isinstance(config, dict)
                    and isinstance(config.get("validation"), dict)
                    else None
                )
            runner_type = inferred or str(row["runner_type"])
            summary = _experiment_config_summary(config, runner_type)
            connection.execute(
                """
                UPDATE experiments SET
                  runner_type = ?, source_experiment_id = ?,
                  memory_source_experiment_id = ?, display_post_filename = ?,
                  display_environment_type = ?, display_memory_strategy = ?,
                  display_hide_speaker = ?, display_speaker_as_json = ?,
                  display_source_run_id = ?, summary_version = 1
                WHERE experiment_id = ?
                """,
                (
                    runner_type,
                    summary["source_experiment_id"],
                    summary["memory_source_experiment_id"],
                    summary["display_post_filename"],
                    summary["display_environment_type"],
                    summary["display_memory_strategy"],
                    summary["display_hide_speaker"],
                    summary["display_speaker_as_json"],
                    summary["display_source_run_id"],
                    row["experiment_id"],
                ),
            )

    @staticmethod
    def _backfill_experiment_summary_table(
        connection: sqlite3.Connection,
    ) -> None:
        experiment_count = int(
            connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
        )
        summary_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM experiment_summaries"
            ).fetchone()[0]
        )
        if experiment_count == summary_count:
            return
        connection.execute(
            """
            INSERT OR IGNORE INTO experiment_summaries(
              experiment_id, group_id, name, description, runner_type,
              resource_policy, config_path, source_experiment_id,
              memory_source_experiment_id, display_post_filename,
              display_environment_type, display_memory_strategy,
              display_hide_speaker, display_speaker_as_json,
              display_source_run_id, created_at, updated_at, archived_at
            )
            SELECT
              experiment_id, group_id, name, description, runner_type,
              resource_policy, config_path, source_experiment_id,
              memory_source_experiment_id, display_post_filename,
              display_environment_type, display_memory_strategy,
              display_hide_speaker, display_speaker_as_json,
              display_source_run_id, created_at, updated_at, archived_at
            FROM experiments
            """
        )

    def create_target_mapping(
        self, *, mapping_id: str, name: str, filename: str,
        mapping: dict[str, str], content_sha256: str,
    ) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            try:
                connection.execute(
                    "INSERT INTO platform_target_mappings "
                    "(mapping_id, name, filename, content_json, content_sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mapping_id, name, filename, json.dumps(mapping, ensure_ascii=False, sort_keys=True),
                     content_sha256, _now()),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise FileExistsError(f"Target mapping already exists: {mapping_id}") from exc
        result = self.get_target_mapping(mapping_id)
        assert result is not None
        return result

    def list_target_mappings(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM platform_target_mappings ORDER BY created_at DESC, mapping_id"
            ).fetchall()
        return [self._target_mapping_record(row) for row in rows]

    def get_target_mapping(self, mapping_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM platform_target_mappings WHERE mapping_id = ?", (mapping_id,)
            ).fetchone()
        return self._target_mapping_record(row) if row is not None else None

    def update_target_mapping_name(self, mapping_id: str, *, name: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE platform_target_mappings SET name = ? WHERE mapping_id = ?",
                (name, mapping_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(mapping_id)
            connection.commit()
        result = self.get_target_mapping(mapping_id)
        assert result is not None
        return result

    def set_group_target_mapping(self, group_id: str, mapping_id: str | None) -> None:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE experiment_groups SET target_mapping_id = ?, updated_at = ? "
                "WHERE group_id = ? AND archived_at IS NULL",
                (mapping_id, _now(), group_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(group_id)
            connection.commit()

    @staticmethod
    def _target_mapping_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "mapping_id": row["mapping_id"], "name": row["name"],
            "filename": row["filename"], "mapping": json.loads(row["content_json"]),
            "content_sha256": row["content_sha256"], "created_at": row["created_at"],
        }

    def create_experiment_group(
        self,
        *,
        group_id: str,
        name: str,
        description: str,
        queries: dict[str, Any] | None = None,
        mcp: dict[str, Any] | None = None,
        query_revision_id: str | None = None,
        mcp_revision_id: str | None = None,
        target_mapping_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with closing(self._connect()) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO experiment_groups(
                      group_id, name, description, queries_json, mcp_json,
                      created_at, updated_at, archived_at, target_mapping_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        group_id,
                        name,
                        description,
                        json.dumps(queries, ensure_ascii=False, sort_keys=True)
                        if queries is not None else None,
                        json.dumps(mcp, ensure_ascii=False, sort_keys=True)
                        if mcp is not None else None,
                        now,
                        now,
                        target_mapping_id,
                    ),
                )
                for role, revision_id in (
                    ("queries", query_revision_id),
                    ("mcp", mcp_revision_id),
                ):
                    if revision_id is not None:
                        connection.execute(
                            """
                            INSERT INTO experiment_group_resource_refs(
                              group_id, role, revision_id
                            ) VALUES (?, ?, ?)
                            """,
                            (group_id, role, revision_id),
                        )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise FileExistsError(f"Experiment group already exists: {group_id}") from exc
        group = self.get_experiment_group(group_id)
        assert group is not None
        return group

    def create_resource(
        self,
        *,
        resource_id: str,
        kind: str,
        name: str,
        description: str,
        revision_id: str,
        filename: str,
        environment_type: str | None,
        content: dict[str, Any],
        content_sha256: str,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO platform_resources(
                      resource_id, kind, name, description,
                      created_at, updated_at, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (resource_id, kind, name, description, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO platform_resource_revisions(
                      revision_id, resource_id, revision_number, filename,
                      environment_type, content_json, content_sha256,
                      summary_json, created_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        resource_id,
                        filename,
                        environment_type,
                        json.dumps(content, ensure_ascii=False, sort_keys=True),
                        content_sha256,
                        json.dumps(summary, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise FileExistsError(
                    f"Platform resource already exists: {resource_id}"
                ) from exc
        resource = self.get_resource(resource_id)
        assert resource is not None
        return resource

    def add_resource_revision(
        self,
        *,
        resource_id: str,
        revision_id: str,
        filename: str,
        environment_type: str | None,
        content: dict[str, Any],
        content_sha256: str,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            resource = connection.execute(
                "SELECT archived_at FROM platform_resources WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
            if resource is None:
                raise KeyError(resource_id)
            if resource["archived_at"] is not None:
                raise PermissionError("archived resources cannot receive new revisions")
            revision_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision_number), 0) + 1 "
                    "FROM platform_resource_revisions WHERE resource_id = ?",
                    (resource_id,),
                ).fetchone()[0]
            )
            try:
                connection.execute(
                    """
                    INSERT INTO platform_resource_revisions(
                      revision_id, resource_id, revision_number, filename,
                      environment_type, content_json, content_sha256,
                      summary_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        resource_id,
                        revision_number,
                        filename,
                        environment_type,
                        json.dumps(content, ensure_ascii=False, sort_keys=True),
                        content_sha256,
                        json.dumps(summary, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE platform_resources SET updated_at = ? WHERE resource_id = ?",
                    (now, resource_id),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise FileExistsError(
                    f"Platform resource revision already exists: {revision_id}"
                ) from exc
        revision = self.get_resource_revision(revision_id)
        assert revision is not None
        return revision

    def list_resources(
        self,
        *,
        kind: str | None = None,
        archived: bool | None = False,
        include_content: bool = True,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if kind is not None:
            where.append("r.kind = ?")
            params.append(kind)
        if archived is True:
            where.append("r.archived_at IS NOT NULL")
        elif archived is False:
            where.append("r.archived_at IS NULL")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT r.*,
                       COUNT(v.revision_id) AS revision_count,
                       MAX(v.revision_number) AS latest_revision_number
                FROM platform_resources AS r
                LEFT JOIN platform_resource_revisions AS v
                  ON v.resource_id = r.resource_id
                {where_sql}
                GROUP BY r.resource_id
                ORDER BY r.updated_at DESC, r.resource_id
                """,
                params,
            ).fetchall()
        resources = [_resource_record(row) for row in rows]
        for resource in resources:
            resource["revisions"] = self.list_resource_revisions(
                str(resource["resource_id"]),
                include_content=include_content,
            )
        return resources

    def get_resource(
        self, resource_id: str, *, include_content: bool = True
    ) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT r.*,
                       COUNT(v.revision_id) AS revision_count,
                       MAX(v.revision_number) AS latest_revision_number
                FROM platform_resources AS r
                LEFT JOIN platform_resource_revisions AS v
                  ON v.resource_id = r.resource_id
                WHERE r.resource_id = ?
                GROUP BY r.resource_id
                """,
                (resource_id,),
            ).fetchone()
        if row is None:
            return None
        resource = _resource_record(row)
        resource["revisions"] = self.list_resource_revisions(
            resource_id, include_content=include_content
        )
        return resource

    def list_resource_revisions(
        self, resource_id: str, *, include_content: bool = True
    ) -> list[dict[str, Any]]:
        revision_columns = _resource_revision_select_columns(
            include_content=include_content
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT {revision_columns}, r.kind, r.name AS resource_name,
                       r.archived_at AS resource_archived_at
                FROM platform_resource_revisions AS v
                JOIN platform_resources AS r USING(resource_id)
                WHERE v.resource_id = ?
                ORDER BY v.revision_number DESC
                """,
                (resource_id,),
            ).fetchall()
        return [
            _resource_revision_record(row, include_content=include_content)
            for row in rows
        ]

    def get_resource_revision(
        self, revision_id: str, *, include_content: bool = True
    ) -> dict[str, Any] | None:
        revision_columns = _resource_revision_select_columns(
            include_content=include_content
        )
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"""
                SELECT {revision_columns}, r.kind, r.name AS resource_name,
                       r.archived_at AS resource_archived_at
                FROM platform_resource_revisions AS v
                JOIN platform_resources AS r USING(resource_id)
                WHERE v.revision_id = ?
                """,
                (revision_id,),
            ).fetchone()
        return (
            _resource_revision_record(row, include_content=include_content)
            if row is not None else None
        )

    def update_resource_metadata(
        self, resource_id: str, *, name: str, description: str
    ) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE platform_resources SET name = ?, description = ?, updated_at = ? "
                "WHERE resource_id = ?",
                (name, description, _now(), resource_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(resource_id)
            connection.commit()
        resource = self.get_resource(resource_id, include_content=False)
        assert resource is not None
        return resource

    def set_resource_archived(
        self, resource_id: str, *, archived: bool
    ) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE platform_resources SET archived_at = ?, updated_at = ? "
                "WHERE resource_id = ?",
                (_now() if archived else None, _now(), resource_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(resource_id)
            connection.commit()
        resource = self.get_resource(resource_id)
        assert resource is not None
        return resource

    def get_experiment_group_resource_refs(
        self, group_id: str, *, include_content: bool = True
    ) -> dict[str, dict[str, Any]]:
        revision_columns = _resource_revision_select_columns(
            include_content=include_content
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT refs.role, {revision_columns}, r.kind,
                       r.name AS resource_name,
                       r.archived_at AS resource_archived_at
                FROM experiment_group_resource_refs AS refs
                JOIN platform_resource_revisions AS v USING(revision_id)
                JOIN platform_resources AS r USING(resource_id)
                WHERE refs.group_id = ?
                """,
                (group_id,),
            ).fetchall()
        return {
            str(row["role"]): _resource_revision_record(
                row, include_content=include_content
            )
            for row in rows
        }

    def get_experiment_group_resource_refs_for_groups(
        self,
        group_ids: list[str],
        *,
        include_content: bool = False,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        unique_ids = list(dict.fromkeys(group_ids))
        result: dict[str, dict[str, dict[str, Any]]] = {
            group_id: {} for group_id in unique_ids
        }
        if not unique_ids:
            return result
        placeholders = ", ".join("?" for _ in unique_ids)
        revision_columns = _resource_revision_select_columns(
            include_content=include_content
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT refs.group_id, refs.role, {revision_columns}, r.kind,
                       r.name AS resource_name,
                       r.archived_at AS resource_archived_at
                FROM experiment_group_resource_refs AS refs
                JOIN platform_resource_revisions AS v USING(revision_id)
                JOIN platform_resources AS r USING(resource_id)
                WHERE refs.group_id IN ({placeholders})
                """,
                unique_ids,
            ).fetchall()
        for row in rows:
            result[str(row["group_id"])][str(row["role"])] = (
                _resource_revision_record(
                    row, include_content=include_content
                )
            )
        return result

    def get_experiment_resource_refs(
        self, experiment_id: str
    ) -> dict[str, dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT refs.role, v.*, r.kind, r.name AS resource_name,
                       r.archived_at AS resource_archived_at
                FROM experiment_resource_refs AS refs
                JOIN platform_resource_revisions AS v USING(revision_id)
                JOIN platform_resources AS r USING(resource_id)
                WHERE refs.experiment_id = ?
                """,
                (experiment_id,),
            ).fetchall()
        return {str(row["role"]): _resource_revision_record(row) for row in rows}

    def list_experiment_groups(
        self,
        *,
        archived: bool | None = False,
        include_resources: bool = True,
    ) -> list[dict[str, Any]]:
        where_sql = ""
        if archived is True:
            where_sql = "WHERE g.archived_at IS NOT NULL"
        elif archived is False:
            where_sql = "WHERE g.archived_at IS NULL"
        group_columns = (
            "g.*"
            if include_resources
            else """
                  g.group_id, g.name, g.description, g.target_mapping_id,
                  g.created_at, g.updated_at, g.archived_at,
                  g.queries_json IS NOT NULL AS has_queries,
                  json_extract(g.queries_json, '$.filename') AS queries_filename,
                  json_extract(
                    g.queries_json, '$.dataset.query_count'
                  ) AS query_count,
                  g.mcp_json IS NOT NULL AS has_mcp,
                  json_extract(g.mcp_json, '$.filename') AS mcp_filename,
                  json_extract(
                    g.mcp_json, '$.dataset.server_count'
                  ) AS server_count,
                  json_extract(
                    g.mcp_json, '$.dataset.tool_count'
                  ) AS tool_count
            """
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT
                  {group_columns},
                  COUNT(DISTINCT e.experiment_id) AS experiment_count,
                  COUNT(DISTINCT CASE
                    WHEN e.runner_type = 'tool_selection' THEN e.experiment_id
                  END) AS stage_two_experiment_count,
                  COUNT(DISTINCT CASE
                    WHEN e.runner_type != 'tool_selection' THEN e.experiment_id
                  END) AS stage_one_experiment_count,
                  COUNT(DISTINCT r.run_id) AS run_count,
                  COUNT(DISTINCT CASE
                    WHEN r.status IN ('queued', 'starting', 'running') THEN r.run_id
                  END) AS active_run_count
                FROM experiment_groups AS g
                LEFT JOIN experiment_summaries AS e ON e.group_id = g.group_id
                LEFT JOIN experiment_runs AS r ON r.experiment_id = e.experiment_id
                {where_sql}
                GROUP BY g.group_id
                ORDER BY g.updated_at DESC, g.group_id
                """
            ).fetchall()
        return [
            _experiment_group_record(
                row, include_resources=include_resources
            )
            for row in rows
        ]

    def get_experiment_group(self, group_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                  g.*,
                  COUNT(DISTINCT e.experiment_id) AS experiment_count,
                  COUNT(DISTINCT CASE
                    WHEN e.runner_type = 'tool_selection' THEN e.experiment_id
                  END) AS stage_two_experiment_count,
                  COUNT(DISTINCT CASE
                    WHEN e.runner_type != 'tool_selection' THEN e.experiment_id
                  END) AS stage_one_experiment_count,
                  COUNT(DISTINCT r.run_id) AS run_count,
                  COUNT(DISTINCT CASE
                    WHEN r.status IN ('queued', 'starting', 'running') THEN r.run_id
                  END) AS active_run_count
                FROM experiment_groups AS g
                LEFT JOIN experiment_summaries AS e ON e.group_id = g.group_id
                LEFT JOIN experiment_runs AS r ON r.experiment_id = e.experiment_id
                WHERE g.group_id = ?
                GROUP BY g.group_id
                """,
                (group_id,),
            ).fetchone()
        return _experiment_group_record(row) if row is not None else None

    def set_experiment_group_archived(
        self, group_id: str, *, archived: bool
    ) -> dict[str, Any]:
        """Atomically archive or restore a group and every member experiment."""
        if group_id == LEGACY_EXPERIMENT_GROUP_ID:
            raise PermissionError(
                "the legacy experiment group cannot be archived or restored as a whole"
            )
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            group = connection.execute(
                "SELECT archived_at FROM experiment_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if group is None:
                raise KeyError(group_id)
            if archived:
                active = connection.execute(
                    """
                    SELECT r.experiment_id
                    FROM experiment_runs AS r
                    JOIN experiments AS e USING(experiment_id)
                    WHERE e.group_id = ?
                      AND r.status IN ('queued', 'starting', 'running')
                    ORDER BY r.experiment_id
                    LIMIT 1
                    """,
                    (group_id,),
                ).fetchone()
                if active is not None:
                    raise PermissionError(
                        "experiment groups with active runs cannot be archived: "
                        + str(active["experiment_id"])
                    )
                connection.execute(
                    """
                    UPDATE experiments
                    SET archived_at = CASE
                          WHEN archived_at IS NULL THEN ? ELSE archived_at END,
                        updated_at = ?
                    WHERE group_id = ?
                    """,
                    (now, now, group_id),
                )
                connection.execute(
                    """
                    UPDATE experiment_summaries
                    SET archived_at = CASE
                          WHEN archived_at IS NULL THEN ? ELSE archived_at END,
                        updated_at = ?
                    WHERE group_id = ?
                    """,
                    (now, now, group_id),
                )
                connection.execute(
                    """
                    UPDATE experiment_groups
                    SET archived_at = CASE
                          WHEN archived_at IS NULL THEN ? ELSE archived_at END,
                        updated_at = ?
                    WHERE group_id = ?
                    """,
                    (now, now, group_id),
                )
            else:
                connection.execute(
                    "UPDATE experiments SET archived_at = NULL, updated_at = ? "
                    "WHERE group_id = ?",
                    (now, group_id),
                )
                connection.execute(
                    "UPDATE experiment_summaries "
                    "SET archived_at = NULL, updated_at = ? WHERE group_id = ?",
                    (now, group_id),
                )
                connection.execute(
                    "UPDATE experiment_groups SET archived_at = NULL, updated_at = ? "
                    "WHERE group_id = ?",
                    (now, group_id),
                )
            connection.commit()
        updated = self.get_experiment_group(group_id)
        assert updated is not None
        return updated

    def delete_experiment_group(
        self, group_id: str, *, expected_experiment_ids: list[str],
        require_archived: bool = True,
        expected_memory_cleanup: dict[str, Any] | None = None,
    ) -> int:
        """Atomically remove a group and its idle members after policy checks."""
        if group_id == LEGACY_EXPERIMENT_GROUP_ID:
            raise PermissionError("the legacy experiment group cannot be deleted")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            group = connection.execute(
                "SELECT archived_at FROM experiment_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if group is None:
                raise KeyError(group_id)
            if require_archived and group["archived_at"] is None:
                raise PermissionError("archive the experiment group before deletion")
            members = connection.execute(
                "SELECT experiment_id, archived_at FROM experiments WHERE group_id = ?",
                (group_id,),
            ).fetchall()
            actual_ids = {str(row["experiment_id"]) for row in members}
            if actual_ids != set(expected_experiment_ids):
                raise RuntimeError("experiment group members changed during deletion")
            unarchived = next(
                (str(row["experiment_id"]) for row in members
                 if row["archived_at"] is None), None,
            )
            if require_archived and unarchived is not None:
                raise PermissionError("group member must remain archived: " + unarchived)
            active = connection.execute(
                """
                SELECT r.experiment_id FROM experiment_runs AS r
                JOIN experiments AS e USING(experiment_id)
                WHERE e.group_id = ? AND r.status IN ('queued', 'starting', 'running')
                LIMIT 1
                """,
                (group_id,),
            ).fetchone()
            if active is not None:
                raise PermissionError(
                    "experiment groups with active runs cannot be deleted: "
                    + str(active["experiment_id"])
                )
            memory_cleanup = self._experiment_group_memory_cleanup(connection, group_id)
            if expected_memory_cleanup is not None and memory_cleanup != expected_memory_cleanup:
                raise RuntimeError("experiment group memory artifacts changed during deletion")
            for table, key, values in (
                ("memory_jobs", "job_id", memory_cleanup["job_ids"]),
                ("memory_replicas", "replica_id", memory_cleanup["replica_ids"]),
                ("memory_pull_cache", "cache_key", memory_cleanup["cache_keys"]),
            ):
                if values:
                    connection.executemany(f"DELETE FROM {table} WHERE {key} = ?", [(value,) for value in values])
            connection.execute(
                "DELETE FROM experiments WHERE group_id = ?", (group_id,)
            )
            deleted = connection.execute(
                "DELETE FROM experiment_groups WHERE group_id = ?", (group_id,)
            )
            if deleted.rowcount != 1:
                raise RuntimeError("experiment group was not deleted")
            connection.commit()
        return len(actual_ids)

    def experiment_group_memory_cleanup(self, group_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            return self._experiment_group_memory_cleanup(connection, group_id)

    def experiment_deletion_index(self) -> list[dict[str, Any]]:
        """Return deletion dependency metadata without decoding config blobs."""
        with closing(self._connect()) as connection:
            versions = {
                str(row["experiment_id"]): row["summary_version"]
                for row in connection.execute("SELECT experiment_id, summary_version FROM experiments")
            }
            rows_by_id = {
                str(row["experiment_id"]): dict(row)
                for row in connection.execute(
                    "SELECT experiment_id, group_id, runner_type, config_path, "
                    "source_experiment_id, memory_source_experiment_id, "
                    "NULL AS dependency_config_json FROM experiment_summaries"
                )
                if row["experiment_id"] in versions
            }
            for experiment_id, version in versions.items():
                if version == 1 and experiment_id in rows_by_id:
                    continue
                row = connection.execute(
                "SELECT experiment_id, group_id, runner_type, config_path, "
                "source_experiment_id, memory_source_experiment_id, "
                "CASE WHEN summary_version IS NULL OR summary_version != 1 "
                    "THEN config_json ELSE NULL END AS dependency_config_json FROM experiments "
                    "WHERE experiment_id = ?", (experiment_id,),
                ).fetchone()
                rows_by_id[experiment_id] = dict(row)
        result = []
        for row in rows_by_id.values():
            source_ids = {
                str(row[key]).strip().lower()
                for key in ("source_experiment_id", "memory_source_experiment_id")
                if isinstance(row[key], str) and str(row[key]).strip()
            }
            if not source_ids and row["dependency_config_json"] is not None:
                summary = _experiment_config_summary(json.loads(row["dependency_config_json"]), row["runner_type"])
                source_ids.update(summary[key] for key in ("source_experiment_id", "memory_source_experiment_id")
                                  if summary[key])
            result.append({
                "experiment_id": row["experiment_id"], "group_id": row["group_id"],
                "runner_type": row["runner_type"], "config_path": row["config_path"],
                "source_experiment_ids": sorted(source_ids),
            })
        return result

    def registered_artifact_paths(self, *, exclude_group_id: str) -> list[tuple[str, str]]:
        """Read ownership paths without run configs, metrics or resource joins."""
        experiments = self.experiment_deletion_index()
        outside_ids = {str(item["experiment_id"]) for item in experiments
                       if item["group_id"] != exclude_group_id}
        paths = [(str(item["experiment_id"]), str(item["config_path"]))
                 for item in experiments if item["experiment_id"] in outside_ids]
        with closing(self._connect()) as connection:
            for row in connection.execute(
                "SELECT run_id, experiment_id, output_dir, worker_config_path, log_path "
                "FROM experiment_runs"
            ):
                if row["experiment_id"] in outside_ids:
                    paths.extend((str(row["run_id"]), str(row[key]))
                                 for key in ("output_dir", "worker_config_path", "log_path"))
        return paths

    @staticmethod
    def _experiment_group_memory_cleanup(connection: sqlite3.Connection, group_id: str) -> dict[str, Any]:
        """Find local memory records owned by group runs, preserving shared files."""
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        run_ids = {str(row[0]) for row in connection.execute(
            "SELECT r.run_id FROM experiment_runs AS r JOIN experiments AS e USING(experiment_id) WHERE e.group_id = ?",
            (group_id,),
        )}
        if not run_ids:
            return {"job_ids": [], "cache_keys": [], "replica_ids": [],
                    "package_files": [], "protected_package_files": []}
        selected_jobs: list[tuple[str, dict[str, Any]]] = []
        other_jobs: list[dict[str, Any]] = []
        if "memory_jobs" in tables:
            for row in connection.execute("SELECT job_id, source_run_id, status, metadata_json, lease_owner, lease_until FROM memory_jobs"):
                metadata = json.loads(row["metadata_json"])
                metadata.setdefault("job_id", row["job_id"])
                if row["source_run_id"] in run_ids:
                    if (row["status"] in {"queued", "running", "verifying"}
                            or row["lease_owner"] and (row["lease_until"] or 0) > time.time()):
                        raise PermissionError("experiment groups with active memory transfers cannot be deleted: " + str(row["job_id"]))
                    selected_jobs.append((str(row["job_id"]), metadata))
                else:
                    other_jobs.append(metadata)
        cache_keys = {(metadata.get("request") or {}).get("cache_key") for _, metadata in selected_jobs}
        cache_keys.difference_update((metadata.get("request") or {}).get("cache_key") for metadata in other_jobs)
        cache_keys.discard(None)
        package_files = {
            str(metadata.get("package_file") or f"packages/{job_id}.json")
            for job_id, metadata in selected_jobs
        }
        protected_package_files = {
            str(metadata.get("package_file") or f"packages/{metadata['job_id']}.json")
            for metadata in other_jobs
        }
        existing_cache_keys: set[str] = set()
        if "memory_pull_cache" in tables:
            for row in connection.execute("SELECT cache_key, metadata_json FROM memory_pull_cache"):
                metadata = json.loads(row["metadata_json"])
                if row["cache_key"] in cache_keys:
                    existing_cache_keys.add(str(row["cache_key"]))
                    if metadata.get("package_file"):
                        package_files.add(str(metadata["package_file"]))
                elif metadata.get("package_file"):
                    protected_package_files.add(str(metadata["package_file"]))
        replica_ids = []
        if "memory_replicas" in tables:
            replica_ids = [str(row["replica_id"]) for row in connection.execute(
                "SELECT replica_id, source_run_id FROM memory_replicas"
            ) if row["source_run_id"] in run_ids]
        return {
            "job_ids": sorted(job_id for job_id, _ in selected_jobs),
            "cache_keys": sorted(existing_cache_keys), "replica_ids": sorted(replica_ids),
            "package_files": sorted(package_files),
            "protected_package_files": sorted(protected_package_files),
        }

    def upsert_experiment(
        self,
        *,
        experiment_id: str,
        name: str,
        description: str,
        runner_type: str,
        config_path: Path,
        config: dict[str, Any],
        group_id: str = LEGACY_EXPERIMENT_GROUP_ID,
        resource_policy: str = "legacy",
        resource_refs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        summary = _experiment_config_summary(config, runner_type)
        with closing(self._connect()) as connection:
            group = connection.execute(
                "SELECT group_id FROM experiment_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if group is None:
                raise KeyError(group_id)
            connection.execute(
                """
                INSERT INTO experiments(
                  experiment_id, group_id, name, description, runner_type,
                  resource_policy, config_path, config_json,
                  source_experiment_id, memory_source_experiment_id,
                  display_post_filename, display_environment_type,
                  display_memory_strategy, display_hide_speaker,
                  display_speaker_as_json, display_source_run_id,
                  summary_version, created_at, updated_at
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?
                )
                ON CONFLICT(experiment_id) DO UPDATE SET
                  name = excluded.name,
                  description = excluded.description,
                  runner_type = excluded.runner_type,
                  resource_policy = excluded.resource_policy,
                  config_path = excluded.config_path,
                  config_json = excluded.config_json,
                  source_experiment_id = excluded.source_experiment_id,
                  memory_source_experiment_id = excluded.memory_source_experiment_id,
                  display_post_filename = excluded.display_post_filename,
                  display_environment_type = excluded.display_environment_type,
                  display_memory_strategy = excluded.display_memory_strategy,
                  display_hide_speaker = excluded.display_hide_speaker,
                  display_speaker_as_json = excluded.display_speaker_as_json,
                  display_source_run_id = excluded.display_source_run_id,
                  summary_version = excluded.summary_version,
                  updated_at = excluded.updated_at
                """,
                (
                    experiment_id,
                    group_id,
                    name,
                    description,
                    runner_type,
                    resource_policy,
                    str(config_path.resolve()),
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    summary["source_experiment_id"],
                    summary["memory_source_experiment_id"],
                    summary["display_post_filename"],
                    summary["display_environment_type"],
                    summary["display_memory_strategy"],
                    summary["display_hide_speaker"],
                    summary["display_speaker_as_json"],
                    summary["display_source_run_id"],
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO experiment_summaries(
                  experiment_id, group_id, name, description, runner_type,
                  resource_policy, config_path, source_experiment_id,
                  memory_source_experiment_id, display_post_filename,
                  display_environment_type, display_memory_strategy,
                  display_hide_speaker, display_speaker_as_json,
                  display_source_run_id, created_at, updated_at, archived_at
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL
                )
                ON CONFLICT(experiment_id) DO UPDATE SET
                  name = excluded.name,
                  description = excluded.description,
                  runner_type = excluded.runner_type,
                  resource_policy = excluded.resource_policy,
                  config_path = excluded.config_path,
                  source_experiment_id = excluded.source_experiment_id,
                  memory_source_experiment_id = excluded.memory_source_experiment_id,
                  display_post_filename = excluded.display_post_filename,
                  display_environment_type = excluded.display_environment_type,
                  display_memory_strategy = excluded.display_memory_strategy,
                  display_hide_speaker = excluded.display_hide_speaker,
                  display_speaker_as_json = excluded.display_speaker_as_json,
                  display_source_run_id = excluded.display_source_run_id,
                  updated_at = excluded.updated_at
                """,
                (
                    experiment_id,
                    group_id,
                    name,
                    description,
                    runner_type,
                    resource_policy,
                    str(config_path.resolve()),
                    summary["source_experiment_id"],
                    summary["memory_source_experiment_id"],
                    summary["display_post_filename"],
                    summary["display_environment_type"],
                    summary["display_memory_strategy"],
                    summary["display_hide_speaker"],
                    summary["display_speaker_as_json"],
                    summary["display_source_run_id"],
                    now,
                    now,
                ),
            )
            if resource_refs is not None:
                connection.execute(
                    "DELETE FROM experiment_resource_refs WHERE experiment_id = ?",
                    (experiment_id,),
                )
                for role, revision_id in resource_refs.items():
                    connection.execute(
                        """
                        INSERT INTO experiment_resource_refs(
                          experiment_id, role, revision_id
                        ) VALUES (?, ?, ?)
                        """,
                        (experiment_id, role, revision_id),
                    )
            connection.commit()
        experiment = self.get_experiment(experiment_id)
        assert experiment is not None
        return experiment

    def list_experiments(
        self,
        *,
        archived: bool | None = False,
        group_id: str | None = None,
        stage: str | None = None,
        include_config: bool = True,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if archived is True:
            where.append("e.archived_at IS NOT NULL")
        elif archived is False:
            where.append("e.archived_at IS NULL")
        if group_id is not None:
            where.append("e.group_id = ?")
            params.append(group_id)
        if stage == "tool_selection":
            where.append("e.runner_type = 'tool_selection'")
        elif stage == "interaction":
            where.append(
                "e.runner_type IN "
                "('moltnet_thread', 'moltnet_forum', 'interactive_thread')"
            )
        elif stage is not None:
            raise ValueError("stage must be interaction or tool_selection")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        experiment_columns = (
            "e.*"
            if include_config
            else """
                  e.experiment_id, e.group_id, e.name, e.description,
                  e.runner_type, e.resource_policy, e.config_path,
                  e.created_at, e.updated_at, e.archived_at,
                  e.source_experiment_id, e.memory_source_experiment_id,
                  e.display_environment_type, e.display_memory_strategy
            """
        )
        experiment_table = (
            "experiments" if include_config else "experiment_summaries"
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT
                  {experiment_columns},
                  g.name AS group_name,
                  g.archived_at AS group_archived_at,
                  COUNT(r.run_id) AS run_count,
                  SUM(
                    CASE
                      WHEN r.status IN ('queued', 'starting', 'running') THEN 1
                      ELSE 0
                    END
                  ) AS active_run_count,
                  (
                    SELECT latest.status
                    FROM experiment_runs AS latest
                    WHERE latest.experiment_id = e.experiment_id
                    ORDER BY latest.created_at DESC
                    LIMIT 1
                  ) AS latest_run_status,
                  (
                    SELECT latest.run_id
                    FROM experiment_runs AS latest
                    WHERE latest.experiment_id = e.experiment_id
                    ORDER BY latest.created_at DESC
                    LIMIT 1
                  ) AS latest_run_id
                FROM {experiment_table} AS e
                LEFT JOIN experiment_groups AS g
                  ON g.group_id = e.group_id
                LEFT JOIN experiment_runs AS r
                  ON r.experiment_id = e.experiment_id
                {where_sql}
                GROUP BY e.experiment_id
                ORDER BY e.updated_at DESC, e.experiment_id
                """,
                params,
            ).fetchall()
        return [_experiment_record(row) for row in rows]

    def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                  e.*,
                  g.name AS group_name,
                  g.archived_at AS group_archived_at,
                  COUNT(r.run_id) AS run_count,
                  SUM(
                    CASE
                      WHEN r.status IN ('queued', 'starting', 'running') THEN 1
                      ELSE 0
                    END
                  ) AS active_run_count,
                  (
                    SELECT latest.status
                    FROM experiment_runs AS latest
                    WHERE latest.experiment_id = e.experiment_id
                    ORDER BY latest.created_at DESC
                    LIMIT 1
                  ) AS latest_run_status,
                  (
                    SELECT latest.run_id
                    FROM experiment_runs AS latest
                    WHERE latest.experiment_id = e.experiment_id
                    ORDER BY latest.created_at DESC
                    LIMIT 1
                  ) AS latest_run_id
                FROM experiments AS e
                LEFT JOIN experiment_groups AS g
                  ON g.group_id = e.group_id
                LEFT JOIN experiment_runs AS r
                  ON r.experiment_id = e.experiment_id
                WHERE e.experiment_id = ?
                GROUP BY e.experiment_id
                """,
                (experiment_id,),
            ).fetchone()
        if row is None:
            return None
        experiment = _experiment_record(row)
        experiment["resource_refs"] = self.get_experiment_resource_refs(experiment_id)
        return experiment

    def delete_experiment_if_unused(self, experiment_id: str) -> bool:
        """Remove a discovered definition only when it has no registered runs."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                DELETE FROM experiments
                WHERE experiment_id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM experiment_runs
                    WHERE experiment_runs.experiment_id = experiments.experiment_id
                  )
                """,
                (experiment_id,),
            )
            connection.commit()
        return cursor.rowcount == 1

    def set_experiment_archived(
        self, experiment_id: str, *, archived: bool
    ) -> dict[str, Any]:
        return self.set_experiments_archived(
            [experiment_id], archived=archived
        )[0]

    def set_experiments_archived(
        self, experiment_ids: list[str], *, archived: bool
    ) -> list[dict[str, Any]]:
        experiment_ids = list(dict.fromkeys(experiment_ids))
        if not experiment_ids:
            raise ValueError("select at least one experiment")
        now = _now()
        placeholders = ", ".join("?" for _ in experiment_ids)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = {
                str(row["experiment_id"])
                for row in connection.execute(
                    f"SELECT experiment_id FROM experiments "
                    f"WHERE experiment_id IN ({placeholders})",
                    experiment_ids,
                )
            }
            missing = [
                experiment_id
                for experiment_id in experiment_ids
                if experiment_id not in existing
            ]
            if missing:
                raise KeyError(missing[0])
            if archived:
                active = connection.execute(
                    f"""
                    SELECT experiment_id
                    FROM experiment_runs
                    WHERE experiment_id IN ({placeholders})
                      AND status IN ('queued', 'starting', 'running')
                    ORDER BY experiment_id
                    LIMIT 1
                    """,
                    experiment_ids,
                ).fetchone()
                if active is not None:
                    raise PermissionError(
                        "experiments with active runs cannot be archived: "
                        + str(active["experiment_id"])
                    )
                connection.execute(
                    f"""
                    UPDATE experiments
                    SET archived_at = CASE
                          WHEN archived_at IS NULL THEN ?
                          ELSE archived_at
                        END,
                        updated_at = ?
                    WHERE experiment_id IN ({placeholders})
                    """,
                    [now, now, *experiment_ids],
                )
                connection.execute(
                    f"""
                    UPDATE experiment_summaries
                    SET archived_at = CASE
                          WHEN archived_at IS NULL THEN ?
                          ELSE archived_at
                        END,
                        updated_at = ?
                    WHERE experiment_id IN ({placeholders})
                    """,
                    [now, now, *experiment_ids],
                )
            else:
                archived_group = connection.execute(
                    f"""
                    SELECT e.experiment_id
                    FROM experiments AS e
                    JOIN experiment_groups AS g ON g.group_id = e.group_id
                    WHERE e.experiment_id IN ({placeholders})
                      AND g.archived_at IS NOT NULL
                    ORDER BY e.experiment_id
                    LIMIT 1
                    """,
                    experiment_ids,
                ).fetchone()
                if archived_group is not None:
                    raise PermissionError(
                        "restore the experiment group before restoring a member experiment: "
                        + str(archived_group["experiment_id"])
                    )
                connection.execute(
                    f"""
                    UPDATE experiments
                    SET archived_at = NULL, updated_at = ?
                    WHERE experiment_id IN ({placeholders})
                    """,
                    [now, *experiment_ids],
                )
                connection.execute(
                    f"""
                    UPDATE experiment_summaries
                    SET archived_at = NULL, updated_at = ?
                    WHERE experiment_id IN ({placeholders})
                    """,
                    [now, *experiment_ids],
                )
            connection.commit()
        experiments = []
        for experiment_id in experiment_ids:
            experiment = self.get_experiment(experiment_id)
            assert experiment is not None
            experiments.append(experiment)
        return experiments

    def delete_experiment(self, experiment_id: str) -> bool:
        """Delete one archived, idle experiment and cascade runs and metrics."""
        try:
            return self.delete_experiments([experiment_id]) == 1
        except KeyError:
            return False

    def delete_experiments(self, experiment_ids: list[str]) -> int:
        """Atomically delete archived, idle experiments and their run data."""
        experiment_ids = list(dict.fromkeys(experiment_ids))
        if not experiment_ids:
            raise ValueError("select at least one experiment")
        placeholders = ", ".join("?" for _ in experiment_ids)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT experiment_id, archived_at
                FROM experiments
                WHERE experiment_id IN ({placeholders})
                """,
                experiment_ids,
            ).fetchall()
            records = {str(row["experiment_id"]): row for row in rows}
            missing = [
                experiment_id
                for experiment_id in experiment_ids
                if experiment_id not in records
            ]
            if missing:
                raise KeyError(missing[0])
            not_archived = next(
                (
                    experiment_id
                    for experiment_id in experiment_ids
                    if records[experiment_id]["archived_at"] is None
                ),
                None,
            )
            if not_archived is not None:
                raise PermissionError(
                    "experiment must remain archived during deletion: "
                    + not_archived
                )
            active = connection.execute(
                f"""
                SELECT experiment_id
                FROM experiment_runs
                WHERE experiment_id IN ({placeholders})
                  AND status IN ('queued', 'starting', 'running')
                ORDER BY experiment_id
                LIMIT 1
                """,
                experiment_ids,
            ).fetchone()
            if active is not None:
                raise PermissionError(
                    "experiments with active runs cannot be deleted: "
                    + str(active["experiment_id"])
                )
            cursor = connection.execute(
                f"DELETE FROM experiments WHERE experiment_id IN ({placeholders})",
                experiment_ids,
            )
            if cursor.rowcount != len(experiment_ids):
                connection.rollback()
                raise RuntimeError("not every related experiment was deleted")
            connection.commit()
        return cursor.rowcount

    def create_run(
        self,
        *,
        run_id: str,
        experiment_id: str,
        worker_config_path: Path,
        output_dir: Path,
        log_path: Path,
        resource_refs: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO experiment_runs(
                  run_id, experiment_id, status, worker_config_path,
                  output_dir, log_path, created_at
                )
                SELECT ?, e.experiment_id, 'queued', ?, ?, ?, ?
                FROM experiments AS e
                JOIN experiment_groups AS g ON g.group_id = e.group_id
                WHERE e.experiment_id = ?
                  AND e.archived_at IS NULL
                  AND g.archived_at IS NULL
                """,
                (
                    run_id,
                    str(worker_config_path.resolve()),
                    str(output_dir.resolve()),
                    str(log_path.resolve()),
                    _now(),
                    experiment_id,
                ),
            )
            if cursor.rowcount != 1:
                experiment = connection.execute(
                    """
                    SELECT e.archived_at, g.archived_at AS group_archived_at
                    FROM experiments AS e
                    LEFT JOIN experiment_groups AS g ON g.group_id = e.group_id
                    WHERE e.experiment_id = ?
                    """,
                    (experiment_id,),
                ).fetchone()
                if experiment is None:
                    raise KeyError(experiment_id)
                if experiment["group_archived_at"] is not None:
                    raise PermissionError("archived experiment groups cannot launch runs")
                raise PermissionError("archived experiments cannot launch runs")
            for role, revision in (resource_refs or {}).items():
                connection.execute(
                    """
                    INSERT INTO run_resource_refs(
                      run_id, role, revision_id, content_sha256
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        role,
                        revision["revision_id"],
                        revision["content_sha256"],
                    ),
                )
            connection.commit()
        run = self.get_run(run_id)
        assert run is not None
        return run

    def update_run(self, run_id: str, **updates: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "screen_session",
            "started_at",
            "completed_at",
            "error",
            "output_dir",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Unknown run update fields: {sorted(unknown)}")
        if not updates:
            run = self.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            return run
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [updates[key] for key in updates]
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                f"UPDATE experiment_runs SET {assignments} WHERE run_id = ?",
                [*values, run_id],
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            connection.commit()
        run = self.get_run(run_id)
        assert run is not None
        return run

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT r.*, e.name AS experiment_name, e.runner_type AS runner_type,
                       e.group_id AS group_id, g.name AS group_name,
                       e.archived_at AS experiment_archived_at,
                       g.archived_at AS group_archived_at
                FROM experiment_runs AS r
                JOIN experiments AS e USING(experiment_id)
                LEFT JOIN experiment_groups AS g ON g.group_id = e.group_id
                WHERE r.run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        run = _run_record(row)
        run["resource_refs"] = self.get_run_resource_refs(run_id)
        run["metrics"] = self.get_metrics(run_id)
        return run

    def get_run_resource_refs(
        self, run_id: str, *, include_content: bool = True
    ) -> dict[str, dict[str, Any]]:
        revision_columns = _resource_revision_select_columns(
            include_content=include_content
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT refs.role, refs.content_sha256 AS frozen_content_sha256,
                       {revision_columns}, r.kind, r.name AS resource_name,
                       r.archived_at AS resource_archived_at
                FROM run_resource_refs AS refs
                JOIN platform_resource_revisions AS v USING(revision_id)
                JOIN platform_resources AS r USING(resource_id)
                WHERE refs.run_id = ?
                """,
                (run_id,),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            revision = _resource_revision_record(
                row, include_content=include_content
            )
            revision["frozen_content_sha256"] = row["frozen_content_sha256"]
            result[str(row["role"])] = revision
        return result

    def get_run_resource_refs_for_runs(
        self,
        run_ids: list[str],
        *,
        include_content: bool = False,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        unique_ids = list(dict.fromkeys(run_ids))
        result: dict[str, dict[str, dict[str, Any]]] = {
            run_id: {} for run_id in unique_ids
        }
        if not unique_ids:
            return result
        placeholders = ", ".join("?" for _ in unique_ids)
        revision_columns = _resource_revision_select_columns(
            include_content=include_content
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT refs.run_id, refs.role,
                       refs.content_sha256 AS frozen_content_sha256,
                       {revision_columns}, r.kind, r.name AS resource_name,
                       r.archived_at AS resource_archived_at
                FROM run_resource_refs AS refs
                JOIN platform_resource_revisions AS v USING(revision_id)
                JOIN platform_resources AS r USING(resource_id)
                WHERE refs.run_id IN ({placeholders})
                """,
                unique_ids,
            ).fetchall()
        for row in rows:
            revision = _resource_revision_record(
                row, include_content=include_content
            )
            revision["frozen_content_sha256"] = row["frozen_content_sha256"]
            result[str(row["run_id"])][str(row["role"])] = revision
        return result

    def delete_run(self, run_id: str) -> bool:
        """Delete a registered run and its metrics in one transaction."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM experiment_runs WHERE run_id = ?",
                (run_id,),
            )
            connection.commit()
        return cursor.rowcount == 1

    def delete_runs(self, run_ids: list[str]) -> int:
        """Delete terminal registered runs and their metrics atomically."""
        unique_ids = list(dict.fromkeys(run_ids))
        if not unique_ids:
            return 0
        placeholders = ", ".join("?" for _ in unique_ids)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT run_id, status
                FROM experiment_runs
                WHERE run_id IN ({placeholders})
                """,
                unique_ids,
            ).fetchall()
            records = {str(row["run_id"]): row for row in rows}
            missing = [run_id for run_id in unique_ids if run_id not in records]
            if missing:
                raise KeyError(missing[0])
            active = next(
                (
                    run_id
                    for run_id in unique_ids
                    if str(records[run_id]["status"])
                    in {"queued", "starting", "running"}
                ),
                None,
            )
            if active is not None:
                raise PermissionError(
                    f"active runs cannot be deleted: {active}"
                )
            cursor = connection.execute(
                f"DELETE FROM experiment_runs WHERE run_id IN ({placeholders})",
                unique_ids,
            )
            if cursor.rowcount != len(unique_ids):
                connection.rollback()
                raise RuntimeError("not every selected run was deleted")
            connection.commit()
        return cursor.rowcount

    def list_runs(
        self,
        *,
        experiment_id: str | None = None,
        group_id: str | None = None,
        runner_type: str | None = None,
        status: str | None = None,
        archived: bool | None = None,
        limit: int = 50,
        offset: int = 0,
        include_resource_content: bool = True,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("run limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("run offset cannot be negative")
        where: list[str] = []
        params: list[Any] = []
        if experiment_id:
            where.append("r.experiment_id = ?")
            params.append(experiment_id)
        if group_id:
            where.append("e.group_id = ?")
            params.append(group_id)
        if runner_type:
            where.append("e.runner_type = ?")
            params.append(runner_type)
        if status:
            where.append("r.status = ?")
            params.append(status)
        if archived is True:
            where.append("e.archived_at IS NOT NULL")
        elif archived is False:
            where.append("e.archived_at IS NULL")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT r.*, e.name AS experiment_name, e.runner_type AS runner_type,
                       e.group_id AS group_id, g.name AS group_name,
                       e.archived_at AS experiment_archived_at,
                       g.archived_at AS group_archived_at,
                       CASE WHEN e.runner_type = 'tool_selection'
                         THEN COALESCE(
                           source_e.display_post_filename,
                           source_post_revision.filename
                         )
                         ELSE COALESCE(
                           e.display_post_filename,
                           post_revision.filename
                         )
                       END AS _display_post_filename,
                       CASE WHEN e.runner_type = 'tool_selection'
                         THEN source_e.display_environment_type
                         ELSE e.display_environment_type
                       END AS _display_environment_type,
                       CASE WHEN e.runner_type = 'tool_selection'
                         THEN COALESCE(
                           e.display_memory_strategy,
                           source_e.display_memory_strategy
                         )
                         ELSE e.display_memory_strategy
                       END AS _display_memory_strategy,
                       CASE WHEN e.runner_type = 'tool_selection'
                         THEN source_e.display_hide_speaker
                         ELSE e.display_hide_speaker
                       END AS _display_hide_speaker,
                       CASE WHEN e.runner_type = 'tool_selection'
                         THEN source_e.display_speaker_as_json
                         ELSE e.display_speaker_as_json
                       END AS _display_speaker_as_json,
                       e.display_source_run_id AS _display_source_run_id
                FROM experiment_runs AS r
                JOIN experiment_summaries AS e USING(experiment_id)
                LEFT JOIN experiment_groups AS g ON g.group_id = e.group_id
                LEFT JOIN experiment_runs AS source_run
                  ON source_run.run_id = e.display_source_run_id
                LEFT JOIN experiment_summaries AS source_e
                  ON source_e.experiment_id = COALESCE(
                    e.source_experiment_id,
                    e.memory_source_experiment_id,
                    source_run.experiment_id
                  )
                LEFT JOIN experiment_resource_refs AS post_ref
                  ON post_ref.experiment_id = e.experiment_id
                 AND post_ref.role = 'posts'
                LEFT JOIN platform_resource_revisions AS post_revision
                  ON post_revision.revision_id = post_ref.revision_id
                LEFT JOIN experiment_resource_refs AS source_post_ref
                  ON source_post_ref.experiment_id = source_e.experiment_id
                 AND source_post_ref.role = 'posts'
                LEFT JOIN platform_resource_revisions AS source_post_revision
                  ON source_post_revision.revision_id = source_post_ref.revision_id
                {where_sql}
                ORDER BY r.created_at DESC, r.run_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        runs = [_run_record(row) for row in rows]
        run_ids = [str(run["run_id"]) for run in runs]
        metrics = self.get_metrics_for_runs(run_ids)
        refs = self.get_run_resource_refs_for_runs(
            run_ids, include_content=include_resource_content
        )
        for run in runs:
            run["metrics"] = metrics.get(run["run_id"], {})
            run["resource_refs"] = refs.get(str(run["run_id"]), {})
        return runs

    def dashboard_counts(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            experiment_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM experiment_summaries "
                    "WHERE archived_at IS NULL"
                ).fetchone()[0]
            )
            archived_experiment_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM experiment_summaries "
                    "WHERE archived_at IS NOT NULL"
                ).fetchone()[0]
            )
            run_count = int(
                connection.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0]
            )
            active_run_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM experiment_runs "
                    "WHERE status IN ('queued', 'starting', 'running')"
                ).fetchone()[0]
            )
            completed_run_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM experiment_runs "
                    "WHERE status IN ('completed', 'completed_with_errors')"
                ).fetchone()[0]
            )
        return {
            "experiments": experiment_count,
            "archived_experiments": archived_experiment_count,
            "runs": run_count,
            "active_runs": active_run_count,
            "completed_runs": completed_run_count,
        }

    def save_metrics(
        self,
        run_id: str,
        metrics: dict[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        if not source.strip():
            raise ValueError("metric source cannot be empty")
        now = _now()
        with closing(self._connect()) as connection:
            exists = connection.execute(
                "SELECT 1 FROM experiment_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(run_id)
            for key, value in metrics.items():
                normalized_key = str(key).strip()
                if not normalized_key:
                    raise ValueError("metric key cannot be empty")
                connection.execute(
                    """
                    INSERT INTO run_metrics(
                      run_id, metric_key, metric_value_json, source, recorded_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, metric_key) DO UPDATE SET
                      metric_value_json = excluded.metric_value_json,
                      source = excluded.source,
                      recorded_at = excluded.recorded_at
                    """,
                    (
                        run_id,
                        normalized_key,
                        json.dumps(value, ensure_ascii=False, sort_keys=True),
                        source,
                        now,
                    ),
                )
            connection.commit()
        return self.get_metrics(run_id)

    def get_metrics(self, run_id: str) -> dict[str, Any]:
        return self.get_metrics_for_runs([run_id]).get(run_id, {})

    def get_metrics_for_runs(
        self, run_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not run_ids:
            return {}
        placeholders = ", ".join("?" for _ in run_ids)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT run_id, metric_key, metric_value_json, source, recorded_at
                FROM run_metrics
                WHERE run_id IN ({placeholders})
                ORDER BY metric_key
                """,
                run_ids,
            ).fetchall()
        result: dict[str, dict[str, Any]] = {run_id: {} for run_id in run_ids}
        for row in rows:
            result[str(row["run_id"])][str(row["metric_key"])] = {
                "value": json.loads(row["metric_value_json"]),
                "source": row["source"],
                "recorded_at": row["recorded_at"],
            }
        return result


def _experiment_record(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    result = {
        "experiment_id": row["experiment_id"],
        "group_id": row["group_id"],
        "name": row["name"],
        "description": row["description"],
        "runner_type": row["runner_type"],
        "resource_policy": row["resource_policy"]
        if "resource_policy" in keys else "legacy",
        "config_path": row["config_path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
        "archived": row["archived_at"] is not None,
    }
    if "config_json" in keys:
        result["config"] = json.loads(row["config_json"])
    if "display_memory_strategy" in keys:
        result["display_config"] = {
            "environment_type": row["display_environment_type"],
            "memory_strategy": row["display_memory_strategy"],
        }
    source_ids = {
        str(row[key]).strip().lower()
        for key in ("source_experiment_id", "memory_source_experiment_id")
        if key in keys and isinstance(row[key], str) and str(row[key]).strip()
    }
    if source_ids:
        result["source_experiment_ids"] = sorted(source_ids)
    if "group_name" in keys:
        result["group_name"] = row["group_name"]
    if "group_archived_at" in keys:
        result["group_archived_at"] = row["group_archived_at"]
        result["group_archived"] = row["group_archived_at"] is not None
    for key in (
        "run_count",
        "active_run_count",
        "latest_run_status",
        "latest_run_id",
    ):
        if key in keys:
            result[key] = row[key]
    return result


def _experiment_group_record(
    row: sqlite3.Row, *, include_resources: bool = True
) -> dict[str, Any]:
    keys = set(row.keys())
    if include_resources:
        queries_json = row["queries_json"]
        mcp_json = row["mcp_json"]
        queries = json.loads(queries_json) if queries_json else None
        mcp = json.loads(mcp_json) if mcp_json else None
    else:
        queries = (
            {
                "filename": row["queries_filename"],
                "dataset": {"query_count": int(row["query_count"] or 0)},
            }
            if bool(row["has_queries"])
            else None
        )
        mcp = (
            {
                "filename": row["mcp_filename"],
                "dataset": {
                    "server_count": int(row["server_count"] or 0),
                    "tool_count": int(row["tool_count"] or 0),
                },
            }
            if bool(row["has_mcp"])
            else None
        )
    result: dict[str, Any] = {
        "group_id": row["group_id"],
        "name": row["name"],
        "description": row["description"],
        "target_mapping_id": row["target_mapping_id"] if "target_mapping_id" in keys else None,
        "queries": queries,
        "mcp": mcp,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
        "archived": row["archived_at"] is not None,
    }
    for key in (
        "experiment_count",
        "stage_one_experiment_count",
        "stage_two_experiment_count",
        "run_count",
        "active_run_count",
    ):
        if key in keys:
            result[key] = int(row[key] or 0)
    result["resources_ready"] = bool(result["queries"] and result["mcp"])
    return result


def _run_record(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    display_keys = {
        "_display_post_filename",
        "_display_environment_type",
        "_display_memory_strategy",
        "_display_hide_speaker",
        "_display_speaker_as_json",
        "_display_source_run_id",
    }
    result = {key: row[key] for key in row.keys() if key not in display_keys}
    if display_keys & keys:
        filename = row["_display_post_filename"]
        environment_type = row["_display_environment_type"]
        hidden = row["_display_hide_speaker"]
        as_json = row["_display_speaker_as_json"]
        result["display_config"] = {
            "post_filename": (
                str(filename).replace("\\", "/").rsplit("/", 1)[-1]
                if isinstance(filename, str) and filename else None
            ),
            "post_source": (
                "file" if filename else
                "database" if environment_type in {
                    "moltnet_thread", "moltnet_forum"
                } else None
            ),
            "memory_strategy": row["_display_memory_strategy"],
            "hide_speaker": bool(hidden) if hidden is not None else None,
            "speaker_as_json": bool(as_json) if as_json is not None else None,
            "source_run_id": row["_display_source_run_id"],
        }
    return result


def _experiment_config_summary(
    config: Any, runner_type: str
) -> dict[str, Any]:
    def section(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def text(value: Any, *, lower: bool = False) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        normalized = value.strip()
        return normalized.lower() if lower else normalized

    config = section(config)
    environment = section(config.get("environment"))
    validation = section(config.get("validation"))
    memory_source = section(validation.get("memory_source"))
    memory = section(config.get("memory"))
    file_value = section(environment.get("file"))
    strategy = text(memory.get("strategy"))
    if runner_type == "tool_selection":
        strategy = text(memory_source.get("memory_strategy")) or strategy
    elif environment and strategy is None:
        strategy = "langmem_official"
    hidden = environment.get("hide_speaker", True) if environment else None
    as_json = environment.get("speaker_as_json", False) if environment else None
    return {
        "source_experiment_id": text(
            validation.get("source_experiment_id"), lower=True
        ),
        "memory_source_experiment_id": text(
            memory_source.get("experiment_id"), lower=True
        ),
        "display_post_filename": text(file_value.get("filename")),
        "display_environment_type": text(environment.get("type")),
        "display_memory_strategy": strategy,
        "display_hide_speaker": (
            int(hidden) if type(hidden) is bool else None
        ),
        "display_speaker_as_json": (
            int(as_json) if type(as_json) is bool else None
        ),
        "display_source_run_id": (
            text(validation.get("source_run_id"))
            or text(memory_source.get("run_id"))
        ),
    }


def _resource_record(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "resource_id": row["resource_id"],
        "kind": row["kind"],
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
        "archived": row["archived_at"] is not None,
        "revision_count": int(row["revision_count"] or 0)
        if "revision_count" in keys else 0,
        "latest_revision_number": int(row["latest_revision_number"] or 0)
        if "latest_revision_number" in keys else 0,
    }


def _resource_revision_select_columns(
    *, include_content: bool, alias: str = "v"
) -> str:
    columns = [
        "revision_id",
        "resource_id",
        "revision_number",
        "filename",
        "environment_type",
        "content_sha256",
        "summary_json",
        "created_at",
    ]
    if include_content:
        columns.insert(5, "content_json")
    return ", ".join(f"{alias}.{column}" for column in columns)


def _resource_revision_record(
    row: sqlite3.Row, *, include_content: bool = True
) -> dict[str, Any]:
    keys = set(row.keys())
    result = {
        "revision_id": row["revision_id"],
        "resource_id": row["resource_id"],
        "revision_number": int(row["revision_number"]),
        "filename": row["filename"],
        "environment_type": row["environment_type"],
        "content_sha256": row["content_sha256"],
        "summary": json.loads(row["summary_json"]),
        "created_at": row["created_at"],
    }
    if include_content:
        result["content"] = json.loads(row["content_json"])
    if "kind" in keys:
        result["kind"] = row["kind"]
    if "resource_name" in keys:
        result["resource_name"] = row["resource_name"]
    if "resource_archived_at" in keys:
        result["resource_archived_at"] = row["resource_archived_at"]
        result["resource_archived"] = row["resource_archived_at"] is not None
    return result


def _now() -> str:
    return datetime.now().astimezone().isoformat()


__all__ = [
    "LEGACY_EXPERIMENT_GROUP_ID",
    "PlatformStore",
    "TERMINAL_RUN_STATUSES",
]
