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
    last_modified DATETIME
);

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
    return conn


def init_db(db_path: Path) -> None:
    with closing(get_connection(db_path)) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
