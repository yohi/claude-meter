import sqlite3
from pathlib import Path

import pytest

from claude_meter.db import get_connection, init_db, migrate_requests, migrate_sync_state


def test_init_db_creates_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row[0] for row in cursor.fetchall()}
    assert tables >= {"requests", "pricing", "sync_state", "daily_summary"}
    conn.close()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row[0] for row in cursor.fetchall()}
    assert "requests" in tables
    conn.close()


_NEW_SYNC_STATE_COLUMNS = {"pending_prompt_text", "last_input_ts"}


def _sync_state_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(sync_state)")}


def test_init_db_creates_sync_state_context_columns(tmp_path: Path) -> None:
    """Fresh databases get the batch-boundary context columns from SCHEMA_SQL."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sync_state)")}
    finally:
        conn.close()
    assert _NEW_SYNC_STATE_COLUMNS <= columns


def test_migrate_sync_state_adds_columns_idempotently(tmp_path: Path) -> None:
    """An existing DB whose sync_state predates the new columns is migrated in
    place, and re-running the migration is a no-op (idempotent)."""
    db_path = tmp_path / "legacy.db"
    conn = get_connection(db_path)
    try:
        # Recreate the pre-migration sync_state schema (no context columns).
        conn.execute(
            """CREATE TABLE sync_state (
                file_path TEXT PRIMARY KEY,
                last_size INTEGER,
                last_line INTEGER,
                last_modified DATETIME
            )"""
        )
        conn.execute(
            "INSERT INTO sync_state (file_path, last_size, last_line) VALUES (?, ?, ?)",
            ("/some/file.jsonl", 10, 1),
        )
        conn.commit()
        assert _NEW_SYNC_STATE_COLUMNS.isdisjoint(_sync_state_columns(conn))

        migrate_sync_state(conn)
        assert _NEW_SYNC_STATE_COLUMNS <= _sync_state_columns(conn)

        # Second call must not raise (duplicate column) and must not change schema.
        columns_after_first = _sync_state_columns(conn)
        migrate_sync_state(conn)
        assert _sync_state_columns(conn) == columns_after_first

        # Existing rows survive the migration; new columns default to NULL.
        row = conn.execute(
            "SELECT last_line, pending_prompt_text, last_input_ts "
            "FROM sync_state WHERE file_path = ?",
            ("/some/file.jsonl",),
        ).fetchone()
        assert row["last_line"] == 1
        assert row["pending_prompt_text"] is None
        assert row["last_input_ts"] is None
    finally:
        conn.close()


@pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 35, 0),
    reason="ALTER TABLE ... DROP COLUMN requires SQLite 3.35.0+",
)
def test_migrate_sync_state_drops_legacy_pending_prompt_ts(tmp_path: Path) -> None:
    """A DB that still carries the obsolete pending_prompt_ts column (added by an
    earlier release) has it dropped by migrate_sync_state()."""
    db_path = tmp_path / "legacy_ts.db"
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        conn.execute("ALTER TABLE sync_state ADD COLUMN pending_prompt_ts DATETIME")
        conn.commit()
        assert "pending_prompt_ts" in _sync_state_columns(conn)

        migrate_sync_state(conn)
        assert "pending_prompt_ts" not in _sync_state_columns(conn)
        # The live context columns are untouched by the drop.
        assert _NEW_SYNC_STATE_COLUMNS <= _sync_state_columns(conn)
    finally:
        conn.close()



_NEW_REQUESTS_COLUMNS = {
    "cache_creation_5m_tokens",
    "cache_creation_1h_tokens",
    "service_tier",
    "speed",
    "web_search_requests",
    "web_fetch_requests",
    "inference_geo",
}


def _requests_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(requests)")}


def test_init_db_creates_requests_extended_columns(tmp_path: Path) -> None:
    """Fresh databases get the extended usage columns from SCHEMA_SQL."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    finally:
        conn.close()
    assert _NEW_REQUESTS_COLUMNS <= columns


def test_migrate_requests_adds_columns_idempotently(tmp_path: Path) -> None:
    """An existing DB whose requests table predates the extended usage columns is
    migrated in place, and re-running the migration is a no-op (idempotent)."""
    db_path = tmp_path / "legacy_requests.db"
    conn = get_connection(db_path)
    try:
        # Recreate a pre-migration requests schema (no extended usage columns).
        conn.execute(
            """CREATE TABLE requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER,
                cache_creation_input_tokens INTEGER,
                UNIQUE (session_id, request_id)
            )"""
        )
        conn.execute(
            "INSERT INTO requests (timestamp, session_id, request_id, model, "
            "input_tokens, cache_creation_input_tokens) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-07-08T10:00:00Z", "s-1", "r-1", "claude-opus-4-8", 10, 200),
        )
        conn.commit()
        assert _NEW_REQUESTS_COLUMNS.isdisjoint(_requests_columns(conn))

        migrate_requests(conn)
        assert _NEW_REQUESTS_COLUMNS <= _requests_columns(conn)

        # Second call must not raise (duplicate column) and must not change schema.
        columns_after_first = _requests_columns(conn)
        migrate_requests(conn)
        assert _requests_columns(conn) == columns_after_first

        # Existing rows survive the migration; new columns default to NULL.
        row = conn.execute(
            "SELECT input_tokens, cache_creation_5m_tokens, service_tier "
            "FROM requests WHERE request_id = ?",
            ("r-1",),
        ).fetchone()
        assert row["input_tokens"] == 10
        assert row["cache_creation_5m_tokens"] is None
        assert row["service_tier"] is None
    finally:
        conn.close()


def test_init_db_on_legacy_database_without_message_id_column(tmp_path: Path) -> None:
    """Regression test: init_db() must not attempt to index message_id via
    SCHEMA_SQL before migrate_requests() has had a chance to add the column to a
    pre-existing database that predates it. CREATE TABLE IF NOT EXISTS is a
    no-op on such a database, so indexing message_id in the same script would
    fail with OperationalError: no such column: message_id."""
    db_path = tmp_path / "legacy_no_message_id.db"
    conn = get_connection(db_path)
    try:
        # Recreate a pre-migration requests/sync_state schema (no message_id
        # column at all, predating even the extended-columns migration).
        conn.execute(
            """CREATE TABLE requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                project TEXT,
                git_repository TEXT,
                model TEXT NOT NULL,
                region TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_creation_input_tokens INTEGER,
                cache_read_input_tokens INTEGER,
                response_time_ms INTEGER,
                cost_usd REAL,
                prompt_text TEXT,
                response_text TEXT,
                source_file TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (session_id, request_id)
            )"""
        )
        conn.execute(
            "CREATE TABLE sync_state (file_path TEXT PRIMARY KEY, last_size INTEGER, "
            "last_line INTEGER, last_modified DATETIME)"
        )
        conn.commit()
    finally:
        conn.close()

    # Must not raise.
    init_db(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        assert "message_id" in _requests_columns(conn)
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='requests'"
            )
        }
        assert "idx_requests_message_id" in indexes
    finally:
        conn.close()