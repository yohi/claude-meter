import sqlite3
from pathlib import Path


from claude_meter.db import get_connection, init_db, migrate_sync_state


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


_NEW_SYNC_STATE_COLUMNS = {"pending_prompt_text", "pending_prompt_ts", "last_input_ts"}


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
