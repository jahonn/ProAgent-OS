from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
from uuid import uuid4

from proagent_os.config import get_settings


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str = "") -> str:
    uid = uuid4().hex[:12]
    return f"{prefix}_{uid}" if prefix else uid


# Baseline DDL shipped as migration ``0001_baseline``.
# A database that already has these tables and indexes is stamped at version 1;
# this script is not executed again. Add later changes in
# ``proagent_os/migrations.py`` instead of editing this string.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_key TEXT NOT NULL UNIQUE,
    goals TEXT DEFAULT '',
    constraints TEXT DEFAULT '',
    tone TEXT DEFAULT 'professional',
    created_at TEXT NOT NULL,
    key_status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS tenant_api_key_audit (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    key_prefix TEXT NOT NULL DEFAULT '',
    key_sha256 TEXT NOT NULL DEFAULT '',
    previous_prefix TEXT NOT NULL DEFAULT '',
    previous_sha256 TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_api_key_audit
    ON tenant_api_key_audit(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    conversation_id TEXT,
    status TEXT NOT NULL,
    input TEXT NOT NULL,
    plan_json TEXT DEFAULT '[]',
    result TEXT DEFAULT '',
    trace_json TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS kb_documents (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT DEFAULT '',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    tokens_estimate INTEGER DEFAULT 0,
    FOREIGN KEY (document_id) REFERENCES kb_documents(id)
);

CREATE TABLE IF NOT EXISTS kb_chunk_embeddings (
    chunk_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    dims INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    model TEXT NOT NULL,
    FOREIGN KEY (chunk_id) REFERENCES kb_chunks(id)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_embeddings_tenant
    ON kb_chunk_embeddings(tenant_id);

CREATE TABLE IF NOT EXISTS monitors (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    config_json TEXT NOT NULL,
    interval_seconds INTEGER DEFAULT 60,
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS recommendations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    monitor_id TEXT,
    task_id TEXT,
    title TEXT NOT NULL,
    rationale TEXT DEFAULT '',
    action_payload_json TEXT DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS recommendation_audit (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    recommendation_id TEXT NOT NULL,
    event TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor TEXT DEFAULT '',
    detail_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    FOREIGN KEY (recommendation_id) REFERENCES recommendations(id)
);

CREATE INDEX IF NOT EXISTS idx_recommendation_audit_rec
    ON recommendation_audit(tenant_id, recommendation_id, created_at);

CREATE TABLE IF NOT EXISTS outcomes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    meta_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS skill_weights (
    tenant_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    score REAL DEFAULT 1.0,
    updates INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, skill_id)
);

CREATE TABLE IF NOT EXISTS tenant_memory (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
"""


class Database:
    def __init__(self, path: Optional[Path] = None) -> None:
        settings = get_settings()
        self.path = Path(path) if path else settings.db_path_resolved
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migration_changes: list = []
        self._init()

    def _init(self) -> None:
        with self.connect() as conn:
            # Lazy import: migrations imports SCHEMA from this module.
            from proagent_os.migrations import apply_migrations

            self.migration_changes = apply_migrations(conn)

    @contextmanager
    def connect(
        self,
        *,
        initializer: Optional[Callable[[sqlite3.Connection], None]] = None,
    ) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success, and always close it.

        ``initializer`` runs after ``PRAGMA foreign_keys`` and before the
        caller uses the connection. The sqlite-vec store passes a loader
        here. The default path does not.
        """
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            if initializer is not None:
                initializer(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.connect() as conn:
            return conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def insert(self, table: str, data: Dict[str, Any]) -> None:
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                tuple(data.values()),
            )

    def update(self, table: str, data: Dict[str, Any], where: str, params: tuple) -> int:
        sets = ", ".join(f"{k}=?" for k in data.keys())
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE {table} SET {sets} WHERE {where}",
                tuple(data.values()) + params,
            )
            return cur.rowcount


_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def reset_db(path: Optional[Path] = None) -> Database:
    global _db
    _db = Database(path)
    return _db


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: str, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default
