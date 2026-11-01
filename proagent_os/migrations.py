"""Versioned SQLite schema migrations.

Startup applies these steps when :class:`proagent_os.db.Database` opens the
file. The same entry point is available before boot::

    python -m proagent_os.migrations

``schema_migrations`` records each version. A version already recorded is
skipped. Version 1 is the historical ``CREATE TABLE IF NOT EXISTS`` schema.
A database that already has that schema, and has no migration rows yet, is
stamped at version 1 without re-running the baseline script. Later versions
still run.

sqlite-vec virtual tables are created by the vector store on demand. This
module does not import that package and does not create those tables.

Add a change by appending the next integer version. Do not edit a version
that has already been released: databases that recorded it will not run the
script again.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from dataclasses import dataclass
from typing import Callable, List, Sequence

from proagent_os.db import SCHEMA, utcnow

logger = logging.getLogger(__name__)

# Tables and indexes created by SCHEMA. Used only to recognize a database
# that already went through the pre-migration startup path.
_BASELINE_TABLES: Sequence[str] = (
    "tenants",
    "tenant_api_key_audit",
    "conversations",
    "messages",
    "tasks",
    "kb_documents",
    "kb_chunks",
    "kb_chunk_embeddings",
    "monitors",
    "recommendations",
    "recommendation_audit",
    "outcomes",
    "skill_weights",
    "tenant_memory",
)

_BASELINE_INDEXES: Sequence[str] = (
    "idx_tenant_api_key_audit",
    "idx_kb_chunk_embeddings_tenant",
    "idx_recommendation_audit_rec",
)

# Forward step after the stamp point. These indexes are not in SCHEMA, so a
# database created before migrations still receives them after version 1 is stamped.
_TENANT_LIST_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_tasks_tenant_created
    ON tasks(tenant_id, created_at);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_tenant
    ON kb_chunks(tenant_id);

CREATE INDEX IF NOT EXISTS idx_recommendations_tenant_status
    ON recommendations(tenant_id, status, created_at);
"""


class SchemaMigrationError(RuntimeError):
    """This build cannot apply the database's schema_migrations history."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationChange:
    version: int
    name: str
    action: str  # "applied" or "stamped"


def apply_baseline_sql(conn: sqlite3.Connection) -> None:
    """Create the baseline tables. Idempotent, for empty or partial files."""
    _exec_script(conn, SCHEMA)


def ensure_tenant_key_status(conn: sqlite3.Connection) -> None:
    """Add ``tenants.key_status`` when the table predates that column.

    ``CREATE TABLE IF NOT EXISTS`` does not alter a table that already exists.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tenants)")}
    if "key_status" not in cols:
        conn.execute(
            "ALTER TABLE tenants ADD COLUMN key_status TEXT NOT NULL DEFAULT 'active'"
        )


def _migration_0001_baseline(conn: sqlite3.Connection) -> None:
    apply_baseline_sql(conn)
    ensure_tenant_key_status(conn)


def _migration_0002_tenant_list_indexes(conn: sqlite3.Connection) -> None:
    _exec_script(conn, _TENANT_LIST_INDEXES_SQL)


# Usage metering stub. Not a payment ledger. OpenTelemetry is not imported here.
_USAGE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS usage_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_kind
    ON usage_events(tenant_id, kind, created_at);
"""


def _migration_0003_usage_events(conn: sqlite3.Connection) -> None:
    _exec_script(conn, _USAGE_EVENTS_SQL)


MIGRATIONS: List[Migration] = [
    Migration(1, "0001_baseline", _migration_0001_baseline),
    Migration(2, "0002_tenant_list_indexes", _migration_0002_tenant_list_indexes),
    Migration(3, "0003_usage_events", _migration_0003_usage_events),
]


def apply_migrations(conn: sqlite3.Connection) -> List[MigrationChange]:
    """Apply pending migrations. Caller commits.

    Expects ``PRAGMA foreign_keys`` to already be set, and expects no open
    transaction. Takes ``BEGIN IMMEDIATE`` so two processes opening the same
    file serialize on the write lock and the second sees the first's rows.
    """
    _check_registry()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = _read_applied(conn)
    changes: List[MigrationChange] = []

    if not applied and _has_baseline_schema(conn):
        baseline = MIGRATIONS[0]
        _record(conn, baseline)
        changes.append(MigrationChange(baseline.version, baseline.name, "stamped"))
        applied = [baseline.version]
        logger.info("schema migration stamped %s %s", baseline.version, baseline.name)

    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        if applied and migration.version != applied[-1] + 1:
            raise SchemaMigrationError(
                f"refusing to skip from schema version {applied[-1]} to {migration.version}"
            )
        migration.apply(conn)
        _record(conn, migration)
        changes.append(MigrationChange(migration.version, migration.name, "applied"))
        applied.append(migration.version)
        logger.info("schema migration applied %s %s", migration.version, migration.name)

    return changes


def _check_registry() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise SchemaMigrationError(
            f"migration registry versions {versions} are not 1..{len(versions)}"
        )


def _read_applied(conn: sqlite3.Connection) -> List[int]:
    rows = [
        int(row[0])
        for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]
    if not rows:
        return []
    known = len(MIGRATIONS)
    if rows[-1] > known:
        raise SchemaMigrationError(
            f"database schema version {rows[-1]} is newer than this build ({known}). "
            "Refusing to open it. Restore a backup written by this version, "
            "or upgrade ProAgent OS."
        )
    expected = list(range(1, rows[-1] + 1))
    if rows != expected:
        raise SchemaMigrationError(
            f"schema_migrations versions {rows} are not contiguous from 1. "
            "Refusing to guess which steps ran."
        )
    return rows


def _has_baseline_schema(conn: sqlite3.Connection) -> bool:
    """True when today's CREATE IF NOT EXISTS path already built this file."""
    present = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")
    }
    if not set(_BASELINE_TABLES).issubset(present):
        return False
    if not set(_BASELINE_INDEXES).issubset(present):
        return False
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tenants)")}
    return "key_status" in cols


def _record(conn: sqlite3.Connection, migration: Migration) -> None:
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
        (migration.version, migration.name, utcnow()),
    )


def _exec_script(conn: sqlite3.Connection, script: str) -> None:
    """Run SQL statements inside the caller's transaction.

    ``Connection.executescript`` commits before it runs, which would split a
    migration from the ``schema_migrations`` row that records it.
    """
    for statement in _split_sql(script):
        conn.execute(statement)


def _split_sql(script: str) -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buf).strip()
            if statement and statement != ";":
                parts.append(statement)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def main() -> None:
    """Apply migrations to ``PROAGENT_DB_PATH`` and print the recorded versions."""
    from proagent_os.db import Database

    try:
        db = Database()
    except SchemaMigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"database: {db.path}")
    if db.migration_changes:
        print("applied now:")
        for change in db.migration_changes:
            print(f"  {change.version} {change.name} ({change.action})")
    else:
        print("applied now: none (already current)")
    print("schema_migrations:")
    for row in db.fetchall(
        "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
    ):
        print(f"  {row['version']} {row['name']} {row['applied_at']}")


if __name__ == "__main__":
    main()
