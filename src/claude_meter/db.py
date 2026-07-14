"""SQLite database schema and connection helpers."""

import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS requests (
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
    cache_creation_5m_tokens INTEGER,
    cache_creation_1h_tokens INTEGER,
    web_search_requests INTEGER,
    web_fetch_requests INTEGER,
    service_tier TEXT,
    speed TEXT,
    inference_geo TEXT,
    message_id TEXT,
    response_time_ms INTEGER,
    cost_usd REAL,
    prompt_text TEXT,
    response_text TEXT,
    source_file TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (session_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_requests_project ON requests(project);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);

CREATE TABLE IF NOT EXISTS pricing (
    model TEXT NOT NULL,
    region TEXT NOT NULL,
    input_price_per_1k REAL,
    output_price_per_1k REAL,
    cache_creation_price_per_1k REAL,
    cache_read_price_per_1k REAL,
    source TEXT,
    updated_at DATETIME,
    PRIMARY KEY (model, region)
);

CREATE TABLE IF NOT EXISTS sync_state (
    file_path TEXT PRIMARY KEY,
    last_size INTEGER,
    last_line INTEGER,
    last_modified DATETIME,
    -- Batch-boundary carry-over context (see collector.parse_incremental):
    -- the most recent human-utterance prompt text (already truncated), plus the
    -- timestamp of the most recent input record, so a later batch can resolve
    -- prompt_text/response_time_ms for an assistant whose parent user record was
    -- ingested in an earlier batch.
    pending_prompt_text TEXT,
    last_input_ts DATETIME
);

-- project is part of the composite PRIMARY KEY and MUST remain NOT NULL.
-- requests.project is nullable, so any aggregation pipeline that writes into
-- daily_summary MUST normalize NULL project values (e.g. COALESCE(project, ''))
-- before insert. Do NOT relax this to `project TEXT` (nullable): SQLite's
-- PRIMARY KEY does not imply NOT NULL for non-rowid/composite keys, and its
-- UNIQUE constraint treats multiple NULLs as distinct, which would silently
-- allow duplicate (date, NULL, model) rows and break the summary's uniqueness.
CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT NOT NULL,
    project TEXT NOT NULL,
    model TEXT NOT NULL,
    total_input_tokens INTEGER,
    total_output_tokens INTEGER,
    total_cache_creation_input_tokens INTEGER,
    total_cache_read_input_tokens INTEGER,
    total_cost_usd REAL,
    request_count INTEGER,
    avg_response_time_ms REAL,
    PRIMARY KEY (date, project, model)
);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: Path) -> None:
    with closing(get_connection(db_path)) as conn:
        conn.executescript(SCHEMA_SQL)
        migrate_sync_state(conn)
        migrate_requests(conn)
        conn.commit()


# Columns added to sync_state after the initial release. Existing databases
# created before these columns must be migrated in place (CREATE TABLE IF NOT
# EXISTS never alters an existing table).
_SYNC_STATE_CONTEXT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("pending_prompt_text", "TEXT"),
    ("last_input_ts", "DATETIME"),
)


def migrate_sync_state(conn: sqlite3.Connection) -> None:
    """Add the batch-boundary context columns to a pre-existing sync_state table.

    Idempotent: each column is only added when absent, so repeated calls (and
    fresh databases that already have the columns from SCHEMA_SQL) are no-ops.

    Also drops the obsolete ``pending_prompt_ts`` column left behind by an
    earlier release. ``DROP COLUMN`` requires SQLite 3.35.0+; on older engines
    the ``OperationalError`` is swallowed and the unused column simply lingers.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sync_state)")}
    for name, decl in _SYNC_STATE_CONTEXT_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE sync_state ADD COLUMN {name} {decl}")
    if "pending_prompt_ts" in existing:
        try:
            conn.execute("ALTER TABLE sync_state DROP COLUMN pending_prompt_ts")
        except sqlite3.OperationalError:
            # SQLite < 3.35.0 lacks DROP COLUMN; the unused column is harmless.
            pass



# Columns added to requests after the initial release (extended per-request usage:
# 5m/1h cache-write split, server tool use counts, service tier, speed, inference
# geo). Existing databases created before these columns must be migrated in place
# (CREATE TABLE IF NOT EXISTS never alters an existing table). A later
# `collect --reparse` backfills the values for rows ingested before the migration.
_REQUESTS_EXTENDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("cache_creation_5m_tokens", "INTEGER"),
    ("cache_creation_1h_tokens", "INTEGER"),
    ("web_search_requests", "INTEGER"),
    ("web_fetch_requests", "INTEGER"),
    ("service_tier", "TEXT"),
    ("speed", "TEXT"),
    ("inference_geo", "TEXT"),
    ("message_id", "TEXT"),
)


def migrate_requests(conn: sqlite3.Connection) -> None:
    """Add the extended per-request usage columns to a pre-existing requests table.

    Idempotent: each column is only added when absent, so repeated calls (and fresh
    databases that already have the columns from SCHEMA_SQL) are no-ops. Existing
    rows keep NULL in the new columns until a ``collect --reparse`` backfills them.

    Tolerant of concurrent migrations: if another process (e.g. a separately
    launched ``collect``/``watch``/UI invocation) already added the column between
    our existence check and our ``ALTER TABLE``, SQLite raises ``OperationalError:
    duplicate column name``; that specific error is swallowed since the end state
    is identical to a successful migration. Unrelated ``OperationalError``s (e.g. a
    genuinely broken schema) are re-raised.

    Also (re)creates ``idx_requests_message_id``. This index cannot live in
    ``SCHEMA_SQL`` because ``message_id`` is only guaranteed to exist once this
    function has run: on a pre-existing database created before this column was
    introduced, ``CREATE TABLE IF NOT EXISTS`` in ``SCHEMA_SQL`` is a no-op, so
    indexing the column there would fail with ``OperationalError: no such column:
    message_id`` before the ``ALTER TABLE`` below has a chance to add it.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    for name, decl in _REQUESTS_EXTENDED_COLUMNS:
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
                # Another process already added this column concurrently; the
                # migration is idempotent by design, so treat this as a no-op.
                pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_message_id ON requests(message_id)")
