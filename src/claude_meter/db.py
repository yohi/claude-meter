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
    -- the most recent human-utterance prompt text (already truncated) and its
    -- timestamp, plus the timestamp of the most recent input record, so a later
    -- batch can resolve prompt_text/response_time_ms for an assistant whose
    -- parent user record was ingested in an earlier batch.
    pending_prompt_text TEXT,
    pending_prompt_ts DATETIME,
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
        conn.commit()


# Columns added to sync_state after the initial release. Existing databases
# created before these columns must be migrated in place (CREATE TABLE IF NOT
# EXISTS never alters an existing table).
_SYNC_STATE_CONTEXT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("pending_prompt_text", "TEXT"),
    ("pending_prompt_ts", "DATETIME"),
    ("last_input_ts", "DATETIME"),
)


def migrate_sync_state(conn: sqlite3.Connection) -> None:
    """Add the batch-boundary context columns to a pre-existing sync_state table.

    Idempotent: each column is only added when absent, so repeated calls (and
    fresh databases that already have the columns from SCHEMA_SQL) are no-ops.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sync_state)")}
    for name, decl in _SYNC_STATE_CONTEXT_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE sync_state ADD COLUMN {name} {decl}")
