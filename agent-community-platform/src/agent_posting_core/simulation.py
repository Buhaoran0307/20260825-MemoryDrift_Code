from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_COMMUNITY = "moltbook-demo"


CREATE_POST_SCHEMA: dict[str, Any] = {
    "name": "create_post",
    "description": "Create a text post in a community on behalf of one hosted agent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "community": {"type": "string"},
            "title": {"type": "string", "maxLength": 120},
            "body": {"type": "string", "maxLength": 2000},
        },
        "required": ["agent_id", "community", "title", "body"],
        "additionalProperties": False,
    },
}


LIST_RECENT_POSTS_SCHEMA: dict[str, Any] = {
    "name": "list_recent_posts",
    "description": "Read recent posts from a community for shared round snapshots.",
    "input_schema": {
        "type": "object",
        "properties": {
            "community": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["community"],
        "additionalProperties": False,
    },
}


CREATE_COMMENT_SCHEMA: dict[str, Any] = {
    "name": "create_comment",
    "description": "Reply to one hosted post or comment on behalf of an agent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "post_id": {"type": "integer"},
            "parent_comment_id": {"type": ["integer", "null"]},
            "body": {"type": "string", "maxLength": 4000},
        },
        "required": ["agent_id", "post_id", "body"],
        "additionalProperties": False,
    },
}


GET_THREAD_SCHEMA: dict[str, Any] = {
    "name": "get_thread",
    "description": "Read one post and its chronological comment history.",
    "input_schema": {
        "type": "object",
        "properties": {"post_id": {"type": "integer"}},
        "required": ["post_id"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class AgentProfile:
    agent_id: str
    display_name: str
    persona: str
    topic: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoricalPost:
    source: str
    source_post_id: str
    agent_id: str
    community: str
    title: str
    body: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)
    upvotes: int | None = None
    downvotes: int | None = None


@dataclass(frozen=True)
class HistoricalComment:
    source_comment_id: str
    agent_id: str
    body: str
    created_at: str
    parent_source_comment_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]

    def as_openai_like(self) -> dict[str, Any]:
        return {
            "type": "function_call",
            "call_id": self.call_id,
            "name": self.name,
            "arguments": json.dumps(self.arguments, ensure_ascii=False),
        }

    def as_mcp_like(self) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": self.call_id,
            "method": "tools/call",
            "params": {"name": self.name, "arguments": self.arguments},
        }


class ForumStore:
    """Small SQLite-backed forum store used by the demo platform adapter."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                  agent_id TEXT PRIMARY KEY,
                  display_name TEXT NOT NULL,
                  persona TEXT NOT NULL,
                  topic TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS posts (
                  post_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  agent_id TEXT NOT NULL,
                  community TEXT NOT NULL,
                  title TEXT NOT NULL,
                  body TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  source TEXT,
                  source_post_id TEXT,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  upvotes INTEGER,
                  downvotes INTEGER,
                  FOREIGN KEY(agent_id) REFERENCES agents(agent_id)
                );

                CREATE TABLE IF NOT EXISTS comments (
                  comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  post_id INTEGER NOT NULL,
                  parent_comment_id INTEGER,
                  agent_id TEXT NOT NULL,
                  body TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  source TEXT,
                  source_comment_id TEXT,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  FOREIGN KEY(post_id) REFERENCES posts(post_id) ON DELETE CASCADE,
                  FOREIGN KEY(parent_comment_id) REFERENCES comments(comment_id),
                  FOREIGN KEY(agent_id) REFERENCES agents(agent_id)
                );

                CREATE TABLE IF NOT EXISTS agent_memory_snapshots (
                  post_id INTEGER NOT NULL,
                  agent_id TEXT NOT NULL,
                  strategy TEXT NOT NULL,
                  revision INTEGER NOT NULL,
                  snapshot_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(post_id, agent_id, strategy),
                  FOREIGN KEY(post_id) REFERENCES posts(post_id) ON DELETE CASCADE,
                  FOREIGN KEY(agent_id) REFERENCES agents(agent_id)
                );

                CREATE TABLE IF NOT EXISTS environment_memory_snapshots (
                  scope_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  strategy TEXT NOT NULL,
                  revision INTEGER NOT NULL,
                  snapshot_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(scope_id, agent_id, strategy),
                  FOREIGN KEY(agent_id) REFERENCES agents(agent_id)
                );

                CREATE TABLE IF NOT EXISTS simulation_rounds (
                  run_id TEXT NOT NULL,
                  round_index INTEGER NOT NULL,
                  post_id INTEGER,
                  environment_type TEXT NOT NULL DEFAULT 'moltnet_thread',
                  scope_id TEXT,
                  memory_strategy TEXT NOT NULL,
                  status TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  completed_at TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  PRIMARY KEY(run_id, round_index),
                  FOREIGN KEY(post_id) REFERENCES posts(post_id) ON DELETE CASCADE
                );
                """
            )
            self._ensure_column("agents", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("posts", "source", "TEXT")
            self._ensure_column("posts", "source_post_id", "TEXT")
            self._ensure_column("posts", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("posts", "upvotes", "INTEGER")
            self._ensure_column("posts", "downvotes", "INTEGER")
            self._migrate_simulation_rounds()
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_source_identity "
                "ON posts(source, source_post_id) WHERE source IS NOT NULL"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_comments_source_identity "
                "ON comments(source, source_comment_id) WHERE source IS NOT NULL"
            )
            self.conn.commit()

    def _migrate_simulation_rounds(self) -> None:
        columns = {
            str(row[1]): row
            for row in self.conn.execute("PRAGMA table_info(simulation_rounds)")
        }
        post_id = columns.get("post_id")
        if (
            post_id is not None
            and int(post_id[3]) == 0
            and {"environment_type", "scope_id"} <= set(columns)
        ):
            return
        self.conn.executescript(
            """
            ALTER TABLE simulation_rounds RENAME TO simulation_rounds_legacy;
            CREATE TABLE simulation_rounds (
              run_id TEXT NOT NULL,
              round_index INTEGER NOT NULL,
              post_id INTEGER,
              environment_type TEXT NOT NULL DEFAULT 'moltnet_thread',
              scope_id TEXT,
              memory_strategy TEXT NOT NULL,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL,
              completed_at TEXT NOT NULL,
              record_json TEXT NOT NULL,
              PRIMARY KEY(run_id, round_index),
              FOREIGN KEY(post_id) REFERENCES posts(post_id) ON DELETE CASCADE
            );
            INSERT INTO simulation_rounds(
              run_id, round_index, post_id, environment_type, scope_id,
              memory_strategy, status, started_at, completed_at, record_json
            )
            SELECT run_id, round_index, post_id, 'moltnet_thread', NULL,
                   memory_strategy, status, started_at, completed_at, record_json
            FROM simulation_rounds_legacy;
            DROP TABLE simulation_rounds_legacy;
            """
        )

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row[1]) for row in self.conn.execute(f'PRAGMA table_info("{table}")')
        }
        if column not in columns:
            self.conn.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "{column}" {declaration}'
            )

    def reset(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                DELETE FROM agent_memory_snapshots;
                DELETE FROM environment_memory_snapshots;
                DELETE FROM simulation_rounds;
                DELETE FROM comments;
                DELETE FROM posts;
                DELETE FROM agents;
                DELETE FROM sqlite_sequence WHERE name IN ('posts', 'comments');
                """
            )
            self.conn.commit()

    def save_simulation_round(
        self,
        *,
        run_id: str,
        round_index: int,
        post_id: int | None,
        memory_strategy: str,
        status: str,
        started_at: str,
        completed_at: str,
        record: dict[str, Any],
        environment_type: str = "moltnet_thread",
        scope_id: str | None = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO simulation_rounds(
                  run_id, round_index, post_id, environment_type, scope_id,
                  memory_strategy, status, started_at, completed_at, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, round_index) DO UPDATE SET
                  post_id = excluded.post_id,
                  environment_type = excluded.environment_type,
                  scope_id = excluded.scope_id,
                  status = excluded.status,
                  completed_at = excluded.completed_at,
                  record_json = excluded.record_json
                """,
                (
                    run_id,
                    int(round_index),
                    int(post_id) if post_id is not None else None,
                    environment_type,
                    scope_id,
                    memory_strategy,
                    status,
                    started_at,
                    completed_at,
                    json.dumps(record, ensure_ascii=False, sort_keys=True),
                ),
            )
            self.conn.commit()

    def list_simulation_rounds(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT run_id, round_index, post_id, environment_type, scope_id,
                       memory_strategy, status, started_at, completed_at, record_json
                FROM simulation_rounds
                WHERE run_id = ?
                ORDER BY round_index ASC
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "round_index": int(row["round_index"]),
                "post_id": (
                    int(row["post_id"]) if row["post_id"] is not None else None
                ),
                "environment_type": row["environment_type"],
                "scope_id": row["scope_id"],
                "memory_strategy": row["memory_strategy"],
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "record": json.loads(row["record_json"]),
            }
            for row in rows
        ]

    def upsert_agent(self, agent: AgentProfile) -> None:
        with self._lock:
            self._upsert_agent(agent)
            self.conn.commit()

    def _upsert_agent(self, agent: AgentProfile) -> None:
        self.conn.execute(
            """
            INSERT INTO agents(
              agent_id, display_name, persona, topic, created_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
              display_name = excluded.display_name,
              persona = excluded.persona,
              topic = excluded.topic,
              metadata_json = excluded.metadata_json
            """,
            (
                agent.agent_id,
                agent.display_name,
                agent.persona,
                agent.topic,
                utc_now(),
                json.dumps(agent.metadata, ensure_ascii=False, sort_keys=True),
            ),
        )

    def import_thread(
        self,
        agents: list[AgentProfile],
        post: HistoricalPost,
        comments: list[HistoricalComment],
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Atomically register participants and restore one historical thread."""
        if not agents:
            raise ValueError("historical thread must have at least one agent")
        agent_ids = {agent.agent_id for agent in agents}
        if post.agent_id not in agent_ids:
            raise ValueError("post author is missing from imported agents")
        if any(comment.agent_id not in agent_ids for comment in comments):
            raise ValueError("comment author is missing from imported agents")
        source_comment_ids = [comment.source_comment_id for comment in comments]
        if len(source_comment_ids) != len(set(source_comment_ids)):
            raise ValueError("historical comments contain duplicate source ids")

        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT post_id FROM posts WHERE source = ? AND source_post_id = ?",
                (post.source, post.source_post_id),
            ).fetchone()
            if existing is not None:
                if not replace:
                    raise FileExistsError(
                        f"Thread {post.source}:{post.source_post_id} is already imported"
                    )
                existing_post_id = int(existing["post_id"])
                self.conn.execute("DELETE FROM comments WHERE post_id = ?", (existing_post_id,))
                self.conn.execute("DELETE FROM posts WHERE post_id = ?", (existing_post_id,))

            for agent in agents:
                self._upsert_agent(agent)
            cursor = self.conn.execute(
                """
                INSERT INTO posts(
                  agent_id, community, title, body, created_at,
                  source, source_post_id, metadata_json, upvotes, downvotes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post.agent_id,
                    post.community,
                    post.title,
                    post.body,
                    post.created_at,
                    post.source,
                    post.source_post_id,
                    json.dumps(post.metadata, ensure_ascii=False, sort_keys=True),
                    post.upvotes,
                    post.downvotes,
                ),
            )
            platform_post_id = int(cursor.lastrowid)
            comment_ids: dict[str, int] = {}
            for comment in comments:
                cursor = self.conn.execute(
                    """
                    INSERT INTO comments(
                      post_id, parent_comment_id, agent_id, body, created_at,
                      source, source_comment_id, metadata_json
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        platform_post_id,
                        comment.agent_id,
                        comment.body,
                        comment.created_at,
                        post.source,
                        comment.source_comment_id,
                        json.dumps(comment.metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
                comment_ids[comment.source_comment_id] = int(cursor.lastrowid)
            for comment in comments:
                if comment.parent_source_comment_id is None:
                    continue
                parent_id = comment_ids.get(comment.parent_source_comment_id)
                if parent_id is not None:
                    self.conn.execute(
                        "UPDATE comments SET parent_comment_id = ? WHERE comment_id = ?",
                        (parent_id, comment_ids[comment.source_comment_id]),
                    )

        return {
            "post_id": platform_post_id,
            "source": post.source,
            "source_post_id": post.source_post_id,
            "agent_count": len(agents),
            "comment_count": len(comments),
        }

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT agent_id, display_name, persona, topic, created_at, metadata_json
                FROM agents
                ORDER BY agent_id ASC
                """
            ).fetchall()
            return [self._agent_dict(row) for row in rows]

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT agent_id, display_name, persona, topic, created_at, metadata_json
                FROM agents WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown agent_id: {agent_id}")
            return self._agent_dict(row)

    @staticmethod
    def _agent_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    def create_post(self, agent_id: str, community: str, title: str, body: str) -> dict[str, Any]:
        community = community.strip()
        title = title.strip()
        body = body.strip()
        if not self.agent_exists(agent_id):
            raise ValueError(f"Unknown agent_id: {agent_id}")
        if not community:
            raise ValueError("community cannot be empty")
        if not title:
            raise ValueError("title cannot be empty")
        if not body:
            raise ValueError("body cannot be empty")
        if len(title) > 120:
            raise ValueError("title cannot exceed 120 characters")
        if len(body) > 2000:
            raise ValueError("body cannot exceed 2000 characters")

        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO posts(
                  agent_id, community, title, body, created_at, upvotes, downvotes
                ) VALUES (?, ?, ?, ?, ?, 0, 0)
                """,
                (agent_id, community, title, body, utc_now()),
            )
            self.conn.commit()
            post_id = int(cursor.lastrowid)
            return self.get_post(post_id)

    def create_comment(
        self,
        agent_id: str,
        post_id: int,
        body: str,
        parent_comment_id: int | None = None,
    ) -> dict[str, Any]:
        body = body.strip()
        if not self.agent_exists(agent_id):
            raise ValueError(f"Unknown agent_id: {agent_id}")
        self.get_post(post_id)
        if not body:
            raise ValueError("body cannot be empty")
        if len(body) > 4000:
            raise ValueError("body cannot exceed 4000 characters")
        if parent_comment_id is not None:
            with self._lock:
                parent = self.conn.execute(
                    "SELECT post_id FROM comments WHERE comment_id = ?",
                    (parent_comment_id,),
                ).fetchone()
            if parent is None:
                raise ValueError(f"Unknown parent_comment_id: {parent_comment_id}")
            if int(parent["post_id"]) != int(post_id):
                raise ValueError("parent comment belongs to a different post")

        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO comments(post_id, parent_comment_id, agent_id, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (post_id, parent_comment_id, agent_id, body, utc_now()),
            )
            self.conn.commit()
            return self.get_comment(int(cursor.lastrowid))

    def agent_exists(self, agent_id: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            return row is not None

    def get_post(self, post_id: int) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT p.post_id, p.agent_id, a.display_name, p.community, p.title,
                       p.body, p.created_at, p.source, p.source_post_id, p.metadata_json,
                       p.upvotes, p.downvotes
                FROM posts p
                JOIN agents a ON a.agent_id = p.agent_id
                WHERE p.post_id = ?
                """,
                (post_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown post_id: {post_id}")
            return self._post_dict(row)

    @staticmethod
    def _post_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    @staticmethod
    def _comment_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    def get_comment(self, comment_id: int) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT c.comment_id, c.post_id, c.parent_comment_id, c.agent_id,
                       a.display_name, c.body, c.created_at, c.source,
                       c.source_comment_id, c.metadata_json
                FROM comments c
                JOIN agents a ON a.agent_id = c.agent_id
                WHERE c.comment_id = ?
                """,
                (comment_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown comment_id: {comment_id}")
            return self._comment_dict(row)

    def list_comments(self, post_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT c.comment_id, c.post_id, c.parent_comment_id, c.agent_id,
                       a.display_name, c.body, c.created_at, c.source,
                       c.source_comment_id, c.metadata_json
                FROM comments c
                JOIN agents a ON a.agent_id = c.agent_id
                WHERE c.post_id = ?
                ORDER BY c.created_at ASC, c.comment_id ASC
                """,
                (post_id,),
            ).fetchall()
            return [self._comment_dict(row) for row in rows]

    def get_thread(self, post_id: int) -> dict[str, Any]:
        return {"post": self.get_post(post_id), "comments": self.list_comments(post_id)}

    def save_memory_snapshot(
        self,
        *,
        post_id: int,
        agent_id: str,
        strategy: str,
        revision: int,
        snapshot: dict[str, Any],
    ) -> None:
        self.get_post(post_id)
        if not self.agent_exists(agent_id):
            raise ValueError(f"Unknown agent_id: {agent_id}")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO agent_memory_snapshots(
                  post_id, agent_id, strategy, revision, snapshot_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id, agent_id, strategy) DO UPDATE SET
                  revision = excluded.revision,
                  snapshot_json = excluded.snapshot_json,
                  updated_at = excluded.updated_at
                """,
                (
                    post_id,
                    agent_id,
                    strategy,
                    revision,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
            self.conn.commit()

    def get_memory_snapshot(
        self, *, post_id: int, agent_id: str, strategy: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT revision, snapshot_json, updated_at
                FROM agent_memory_snapshots
                WHERE post_id = ? AND agent_id = ? AND strategy = ?
                """,
                (post_id, agent_id, strategy),
            ).fetchone()
            if row is None:
                return None
            return {
                "revision": int(row["revision"]),
                "snapshot": json.loads(row["snapshot_json"]),
                "updated_at": row["updated_at"],
            }

    def save_environment_memory_snapshot(
        self,
        *,
        scope_id: str,
        agent_id: str,
        strategy: str,
        revision: int,
        snapshot: dict[str, Any],
    ) -> None:
        if not scope_id.strip():
            raise ValueError("memory scope_id cannot be empty")
        if not self.agent_exists(agent_id):
            raise ValueError(f"Unknown agent_id: {agent_id}")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO environment_memory_snapshots(
                  scope_id, agent_id, strategy, revision, snapshot_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id, agent_id, strategy) DO UPDATE SET
                  revision = excluded.revision,
                  snapshot_json = excluded.snapshot_json,
                  updated_at = excluded.updated_at
                """,
                (
                    scope_id,
                    agent_id,
                    strategy,
                    revision,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
            self.conn.commit()

    def get_environment_memory_snapshot(
        self, *, scope_id: str, agent_id: str, strategy: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT revision, snapshot_json, updated_at
                FROM environment_memory_snapshots
                WHERE scope_id = ? AND agent_id = ? AND strategy = ?
                """,
                (scope_id, agent_id, strategy),
            ).fetchone()
            if row is None:
                return None
            return {
                "revision": int(row["revision"]),
                "snapshot": json.loads(row["snapshot_json"]),
                "updated_at": row["updated_at"],
            }

    def list_recent_posts(self, community: str, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT p.post_id, p.agent_id, a.display_name, p.community, p.title,
                       p.body, p.created_at, p.source, p.source_post_id, p.metadata_json,
                       p.upvotes, p.downvotes
                FROM posts p
                JOIN agents a ON a.agent_id = p.agent_id
                WHERE p.community = ?
                ORDER BY p.post_id DESC
                LIMIT ?
                """,
                (community, limit),
            ).fetchall()
            return [self._post_dict(row) for row in rows]

    def list_all_posts(
        self, community: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            if community:
                rows = self.conn.execute(
                    """
                    SELECT p.post_id, p.agent_id, a.display_name, p.community, p.title,
                           p.body, p.created_at, p.source, p.source_post_id, p.metadata_json,
                           p.upvotes, p.downvotes
                    FROM posts p
                    JOIN agents a ON a.agent_id = p.agent_id
                    WHERE p.community = ?
                    ORDER BY p.post_id ASC
                    LIMIT ?
                    """,
                    (community, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """
                    SELECT p.post_id, p.agent_id, a.display_name, p.community, p.title,
                           p.body, p.created_at, p.source, p.source_post_id, p.metadata_json,
                           p.upvotes, p.downvotes
                    FROM posts p
                    JOIN agents a ON a.agent_id = p.agent_id
                    ORDER BY p.post_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [self._post_dict(row) for row in rows]

    def list_forum_posts(
        self,
        communities: list[str] | tuple[str, ...],
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        selected = list(dict.fromkeys(value.strip() for value in communities if value.strip()))
        if not selected:
            return []
        limit = max(1, min(int(limit), 500))
        placeholders = ", ".join("?" for _ in selected)
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT p.post_id, p.agent_id, a.display_name, p.community, p.title,
                       p.body, p.created_at, p.source, p.source_post_id, p.metadata_json,
                       p.upvotes, p.downvotes
                FROM posts p
                JOIN agents a ON a.agent_id = p.agent_id
                WHERE p.community IN ({placeholders})
                ORDER BY p.post_id DESC
                LIMIT ?
                """,
                [*selected, limit],
            ).fetchall()
            return [self._post_dict(row) for row in rows]

    def forum_counts(
        self, communities: list[str] | tuple[str, ...]
    ) -> dict[str, int]:
        selected = list(dict.fromkeys(value.strip() for value in communities if value.strip()))
        if not selected:
            return {"posts": 0, "comments": 0}
        placeholders = ", ".join("?" for _ in selected)
        with self._lock:
            post_count = int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM posts WHERE community IN ({placeholders})",
                    selected,
                ).fetchone()[0]
            )
            comment_count = int(
                self.conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM comments c
                    JOIN posts p ON p.post_id = c.post_id
                    WHERE p.community IN ({placeholders})
                    """,
                    selected,
                ).fetchone()[0]
            )
        return {"posts": post_count, "comments": comment_count}


class ToolRegistry:
    """Executes validated tool calls on behalf of agents."""

    def __init__(self, store: ForumStore):
        self.schemas = [
            CREATE_POST_SCHEMA,
            CREATE_COMMENT_SCHEMA,
            LIST_RECENT_POSTS_SCHEMA,
            GET_THREAD_SCHEMA,
        ]
        self._handlers: dict[str, Callable[..., Any]] = {
            "create_post": store.create_post,
            "create_comment": store.create_comment,
            "list_recent_posts": store.list_recent_posts,
            "get_thread": store.get_thread,
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return self.schemas

    def execute(self, call: ToolCall, *, caller_agent_id: str | None = None) -> dict[str, Any]:
        if call.name not in self._handlers:
            raise ValueError(f"Unsupported tool: {call.name}")

        if call.name in {"create_post", "create_comment"}:
            arg_agent_id = call.arguments.get("agent_id")
            if caller_agent_id is not None and arg_agent_id != caller_agent_id:
                raise PermissionError(
                    f"Agent {caller_agent_id} cannot post as {arg_agent_id}"
                )

        result = self._handlers[call.name](**call.arguments)
        return {"tool": call.name, "result": result}


class DeterministicPostingAgent:
    def __init__(self, profile: AgentProfile):
        self.profile = profile

    def plan(self, round_index: int, community: str, snapshot: list[dict[str, Any]]) -> ToolCall:
        title = f"{self.profile.display_name}: round {round_index} note on {self.profile.topic}"
        referenced = next(
            (post for post in snapshot if post["agent_id"] != self.profile.agent_id),
            snapshot[0] if snapshot else None,
        )
        if referenced:
            body = (
                f"I noticed #{referenced['post_id']} by {referenced['display_name']}: "
                f"'{referenced['title']}'. From my perspective as {self.profile.persona}, "
                f"the next useful question is how this affects {self.profile.topic}."
            )
        else:
            body = (
                f"I am starting a thread from the perspective of {self.profile.persona}. "
                f"My current focus is {self.profile.topic}, and I want other agents to react."
            )

        return ToolCall(
            call_id=f"{self.profile.agent_id}-r{round_index}-post",
            name="create_post",
            arguments={
                "agent_id": self.profile.agent_id,
                "community": community,
                "title": title,
                "body": body,
            },
        )


class ThreeAgentSimulation:
    def __init__(self, db_path: Path):
        self.store = ForumStore(db_path)
        self.tools = ToolRegistry(self.store)
        self.agents = default_agents()
        self.agent_runtimes = [DeterministicPostingAgent(agent) for agent in self.agents]
        self._round_index = 0
        self._round_log: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.store.reset()
        self._round_index = 0
        self._round_log = []
        self.bootstrap()

    def bootstrap(self) -> None:
        for agent in self.agents:
            self.store.upsert_agent(agent)

    def adopt_registered_agents(self) -> bool:
        """Use profiles already present in a restored platform database."""
        rows = self.store.list_agents()
        if not rows:
            return False
        self.agents = [
            AgentProfile(
                agent_id=row["agent_id"],
                display_name=row["display_name"],
                persona=row["persona"],
                topic=row["topic"],
                metadata=row["metadata"],
            )
            for row in rows
        ]
        self.agent_runtimes = [DeterministicPostingAgent(agent) for agent in self.agents]
        return True

    def next_round_index(self) -> int:
        self._round_index += 1
        return self._round_index

    def list_rounds(self) -> list[dict[str, Any]]:
        return list(self._round_log)

    def run_round(
        self, round_index: int | None = None, community: str = DEFAULT_COMMUNITY
    ) -> dict[str, Any]:
        if round_index is None:
            round_index = self.next_round_index()
        else:
            self._round_index = max(self._round_index, round_index)

        snapshot_call = ToolCall(
            call_id=f"round-{round_index}-snapshot",
            name="list_recent_posts",
            arguments={"community": community, "limit": 5},
        )
        snapshot = self.tools.execute(snapshot_call)["result"]
        executions: list[dict[str, Any]] = []

        for runtime in self.agent_runtimes:
            call = runtime.plan(round_index, community, snapshot)
            execution = self.tools.execute(call, caller_agent_id=runtime.profile.agent_id)
            executions.append(
                {
                    "agent": runtime.profile.__dict__,
                    "openai_like_call": call.as_openai_like(),
                    "mcp_like_call": call.as_mcp_like(),
                    "execution": execution,
                }
            )

        record = {
            "round_index": round_index,
            "community": community,
            "started_snapshot": snapshot,
            "executions": executions,
            "posts": self.store.list_all_posts(community=community),
            "created_at": utc_now(),
        }
        self._round_log.append(record)
        return record

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "agents": len(self.agents),
            "posts": len(self.store.list_all_posts()),
            "rounds": len(self._round_log),
        }

    def close(self) -> None:
        self.store.close()


def default_agents() -> list[AgentProfile]:
    return [
        AgentProfile(
            agent_id="agent_researcher",
            display_name="Researcher",
            persona="a careful AI-research analyst",
            topic="memory mechanisms",
        ),
        AgentProfile(
            agent_id="agent_builder",
            display_name="Builder",
            persona="a pragmatic platform engineer",
            topic="posting API reliability",
        ),
        AgentProfile(
            agent_id="agent_critic",
            display_name="Critic",
            persona="a skeptical experiment reviewer",
            topic="latency and cost controls",
        ),
    ]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AgentProfile",
    "CREATE_POST_SCHEMA",
    "CREATE_COMMENT_SCHEMA",
    "DEFAULT_COMMUNITY",
    "DeterministicPostingAgent",
    "ForumStore",
    "LIST_RECENT_POSTS_SCHEMA",
    "GET_THREAD_SCHEMA",
    "HistoricalComment",
    "HistoricalPost",
    "ThreeAgentSimulation",
    "ToolCall",
    "ToolRegistry",
    "default_agents",
    "utc_now",
]
