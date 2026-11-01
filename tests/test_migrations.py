"""SQLite schema migrations: fresh files, stamped files, and one forward step."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from proagent_os.db import SCHEMA, get_db, reset_db, utcnow
from proagent_os.migrations import SchemaMigrationError, _TENANT_LIST_INDEXES_SQL, main


def _migration_rows(db):
    return db.fetchall(
        "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
    )


def _names(db):
    return {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master")}


def test_stamp_fingerprint_matches_baseline_schema():
    """The stamp check must describe SCHEMA, not a later migration."""
    import re

    import proagent_os.migrations as migrations

    tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA)
    indexes = re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)", SCHEMA)
    assert list(migrations._BASELINE_TABLES) == tables
    assert list(migrations._BASELINE_INDEXES) == indexes


def test_fresh_database_applies_baseline_and_additive_indexes():
    db = get_db()
    assert [(c.version, c.name, c.action) for c in db.migration_changes] == [
        (1, "0001_baseline", "applied"),
        (2, "0002_tenant_list_indexes", "applied"),
        (3, "0003_usage_events", "applied"),
        (4, "0004_tenant_role_overrides", "applied"),
    ]
    names = _names(db)
    for required in (
        "tenants",
        "tenant_api_key_audit",
        "kb_chunk_embeddings",
        "recommendation_audit",
        "schema_migrations",
        "idx_recommendation_audit_rec",
        "idx_tasks_tenant_created",
        "idx_kb_chunks_tenant",
        "idx_recommendations_tenant_status",
        "usage_events",
        "idx_usage_events_tenant_kind",
        "tenant_role_overrides",
    ):
        assert required in names
    assert "kb_vec_index" not in names
    assert "sqlite_vec" not in Path(sys.modules["proagent_os.migrations"].__file__).read_text()
    cols = {row["name"] for row in db.fetchall("PRAGMA table_info(tenants)")}
    assert "key_status" in cols

    db.insert(
        "tenants",
        {
            "id": "ten_fresh",
            "name": "Fresh Co",
            "api_key": "pk_fresh_secret_value",
            "goals": "",
            "constraints": "",
            "tone": "professional",
            "created_at": utcnow(),
            "key_status": "active",
        },
    )
    row = db.fetchone("SELECT name FROM tenants WHERE id=?", ("ten_fresh",))
    assert row["name"] == "Fresh Co"

    # A second open must not import sqlite-vec. Pop first so an earlier test
    # that loaded the optional module cannot hide a new import.
    sys.modules.pop("sqlite_vec", None)
    again = reset_db(db.path)
    assert again.migration_changes == []
    assert "sqlite_vec" not in sys.modules
    assert "kb_vec_index" not in _names(again)


def test_already_initialized_database_is_stamped_then_additive(monkeypatch):
    """A file built by today's CREATE IF NOT EXISTS path is stamped, not rebuilt."""
    import proagent_os.migrations as migrations

    calls = {"baseline": 0}
    real = migrations.apply_baseline_sql

    def wrapped(conn):
        calls["baseline"] += 1
        real(conn)

    monkeypatch.setattr(migrations, "apply_baseline_sql", wrapped)

    path = get_db().path.parent / "already.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE tenants ADD COLUMN note TEXT DEFAULT ''")
    conn.execute(
        """
        INSERT INTO tenants (
            id, name, api_key, goals, constraints, tone, created_at, key_status, note
        ) VALUES (?, ?, ?, '', '', 'professional', ?, 'active', 'keep-me')
        """,
        ("ten_keep", "Kept Co", "pk_keep_secret_value", utcnow()),
    )
    conn.commit()
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='idx_tasks_tenant_created'"
        ).fetchone()
        is None
    )
    conn.close()

    sys.modules.pop("sqlite_vec", None)
    db = reset_db(path)
    assert calls["baseline"] == 0
    assert [(c.version, c.name, c.action) for c in db.migration_changes] == [
        (1, "0001_baseline", "stamped"),
        (2, "0002_tenant_list_indexes", "applied"),
        (3, "0003_usage_events", "applied"),
        (4, "0004_tenant_role_overrides", "applied"),
    ]
    row = db.fetchone("SELECT api_key, note, key_status FROM tenants WHERE id=?", ("ten_keep",))
    assert row["api_key"] == "pk_keep_secret_value"
    assert row["note"] == "keep-me"
    assert row["key_status"] == "active"
    assert "idx_tasks_tenant_created" in _names(db)
    assert "idx_kb_chunks_tenant" in _names(db)
    assert "idx_recommendations_tenant_status" in _names(db)
    assert "usage_events" in _names(db)
    assert "tenant_role_overrides" in _names(db)
    assert "sqlite_vec" not in sys.modules
    assert "kb_vec_index" not in _names(db)


def test_partial_legacy_database_runs_baseline_instead_of_stamp(monkeypatch):
    import proagent_os.migrations as migrations

    calls = {"baseline": 0}
    real = migrations.apply_baseline_sql

    def wrapped(conn):
        calls["baseline"] += 1
        real(conn)

    monkeypatch.setattr(migrations, "apply_baseline_sql", wrapped)

    path = get_db().path.parent / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE tenants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            api_key TEXT NOT NULL UNIQUE,
            goals TEXT DEFAULT '',
            constraints TEXT DEFAULT '',
            tone TEXT DEFAULT 'professional',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO tenants (id, name, api_key, goals, constraints, tone, created_at)
        VALUES (?, ?, ?, '', '', 'professional', ?)
        """,
        ("ten_legacy", "Legacy Co", "pk_legacy_secret_value", utcnow()),
    )
    conn.commit()
    conn.close()

    db = reset_db(path)
    assert calls["baseline"] == 1
    assert [(c.version, c.action) for c in db.migration_changes] == [
        (1, "applied"),
        (2, "applied"),
        (3, "applied"),
        (4, "applied"),
    ]
    row = db.fetchone("SELECT key_status, api_key FROM tenants WHERE id=?", ("ten_legacy",))
    assert row["key_status"] == "active"
    assert row["api_key"] == "pk_legacy_secret_value"
    assert "kb_chunk_embeddings" in _names(db)
    assert "idx_tasks_tenant_created" in _names(db)


def test_repeat_startup_applies_nothing():
    before = _migration_rows(get_db())
    assert [row["version"] for row in before] == [1, 2, 3, 4]
    db = reset_db(get_db().path)
    assert db.migration_changes == []
    assert _migration_rows(db) == before


def test_future_schema_version_is_refused():
    path = get_db().path.parent / "future.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    now = utcnow()
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (1, '0001_baseline', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (2, '0002_tenant_list_indexes', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (99, 'from-the-future', ?)",
        (now,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SchemaMigrationError, match="newer than this build"):
        reset_db(path)


def test_gapped_schema_migrations_are_refused():
    path = get_db().path.parent / "gapped.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    now = utcnow()
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (2, '0002_tenant_list_indexes', ?)",
        (now,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SchemaMigrationError, match="not contiguous"):
        reset_db(path)


def test_version_2_database_applies_later_migrations():
    """A database that already recorded #22's migrations gains versions 3 and 4."""
    path = get_db().path.parent / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executescript(_TENANT_LIST_INDEXES_SQL)
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    now = utcnow()
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (1, '0001_baseline', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (2, '0002_tenant_list_indexes', ?)",
        (now,),
    )
    conn.commit()
    assert (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name='usage_events'").fetchone()
        is None
    )
    conn.close()

    db = reset_db(path)
    assert [(c.version, c.name, c.action) for c in db.migration_changes] == [
        (3, "0003_usage_events", "applied"),
        (4, "0004_tenant_role_overrides", "applied"),
    ]
    assert "usage_events" in _names(db)
    assert "idx_usage_events_tenant_kind" in _names(db)
    assert "tenant_role_overrides" in _names(db)
    text = Path(sys.modules["proagent_os.migrations"].__file__).read_text()
    assert "import opentelemetry" not in text


def test_migrate_cli_prints_recorded_versions(capsys: pytest.CaptureFixture[str]):
    main()
    out = capsys.readouterr().out
    assert "already current" in out
    assert "0001_baseline" in out
    assert "0002_tenant_list_indexes" in out
    assert "0003_usage_events" in out
    assert "0004_tenant_role_overrides" in out
    assert str(get_db().path) in out
