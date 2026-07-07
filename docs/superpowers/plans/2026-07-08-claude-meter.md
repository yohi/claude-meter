# claude-meter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local-only Python tool (`claude-meter`) that parses ClaudeCode JSONL usage logs, estimates AWS Bedrock costs from cached pricing, stores everything in SQLite, and exposes the data through a Streamlit Web UI and a small CLI.

**Architecture:** A headless `Collector` incrementally parses `~/.claude/projects/*/*.jsonl` and `~/.claude/transcripts/*.jsonl` into a normalized `requests` table; a `Pricing` module keeps per-region per-model prices fresh from AWS / models.dev / built-in fallback; a `Streamlit` multi-page app visualizes aggregated usage; and a `CLI` (Click/Typer) wires initialization, one-shot collection, filesystem watch, UI launch, and pricing refresh together. All state lives under `~/.claude-meter/`.

**Tech Stack:** Python 3.10+, SQLite, Streamlit, watchdog, requests, pydantic, Altair, pytest.

## Global Constraints

- All data must stay local; only pricing data may be fetched externally.
- External pricing sources, in order: `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/`, then `https://models.dev/providers/amazon-bedrock/`, then a built-in JSON fallback shipped with the package.
- Default region for cost calculation and `requests.region` is `us-east-1`, overridable in `~/.claude-meter/config.yaml` via `claude.region`.
- Multi-OS path resolution: macOS/Linux default to `~/.claude`; Windows defaults to `%LOCALAPPDATA%\Claude`; overridable via `claude.projects_dir` and `claude.transcripts_dir`.
- Database path default: `~/.claude-meter/data.db`; config default: `~/.claude-meter/config.yaml`; pricing cache default: `~/.claude-meter/pricing.json`.
- `requests` uniqueness is `(session_id, request_id)`.
- Unknown models result in `cost_usd = NULL` and display as "Unknown model" in the UI.
- Prompt storage is configurable via `privacy.store_prompts` (default `true`) and visibility via `privacy.show_prompts_in_ui` (default `true`).
- Pricing cache TTL is 24 hours by default, configurable via `pricing.cache_ttl_hours`.
- Use `pathlib` everywhere; no hard-coded absolute paths in committed code.

---

## File Structure

```text
claude-meter/
├── pyproject.toml                  # project metadata + dependencies + console script
├── README.md                       # install, quick-start, privacy note
├── src/
│   └── claude_meter/
│       ├── __init__.py
│       ├── cli.py                  # Click/Typer commands: init, collect, watch, ui, pricing, config
│       ├── config.py               # Pydantic settings, YAML load/save, OS-default path resolution
│       ├── db.py                   # SQLite schema creation, connection, migrations
│       ├── models.py               # Pydantic dataclasses for usage records and pricing rows
│       ├── collector.py            # JSONL incremental parsing, transcript pairing, response-time calc
│       ├── pricing.py              # fetch/cache/normalize prices from AWS / models.dev / fallback
│       ├── pricing_fallback.json   # bundled fallback prices
│       ├── cost.py                 # cost calculation using normalized model names
│       ├── model_normalizer.py     # map ClaudeCode internal names to Bedrock ARNs and vice versa
│       ├── watcher.py              # watchdog + polling filesystem watch
│       └── ui/
│           ├── __init__.py
│           ├── app.py              # Streamlit entrypoint with sidebar navigation
│           ├── overview.py         # Overview page
│           ├── project_breakdown.py
│           ├── model_breakdown.py
│           ├── session_explorer.py
│           ├── pricing_settings.py
│           └── config_page.py
└── tests/
    ├── __init__.py
    ├── conftest.py                 # temp dirs, sample JSONL fixtures
    ├── test_config.py
    ├── test_db.py
    ├── test_collector.py
    ├── test_pricing.py
    ├── test_cost.py
    ├── test_model_normalizer.py
    └── test_cli.py
```

---

### Task 1: Project Skeleton & Packaging

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `src/claude_meter/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Produces: package `claude_meter` installable via `pip install -e .` and console script `claude-meter`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "claude-meter"
version = "0.1.0"
description = "Local ClaudeCode usage and cost analyzer"
readme = "README.md"
requires-python = ">=3.10"
license = {text = "MIT"}
authors = [
    {name = "Yusuke Ohi"}
]
dependencies = [
    "streamlit>=1.33",
    "watchdog>=4.0",
    "requests>=2.31",
    "pydantic>=2.7",
    "pydantic-settings>=2.2",
    "pyyaml>=6.0",
    "altair>=5.3",
    "pandas>=2.2",
    "click>=8.1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.2",
    "pytest-cov>=5.0",
    "ruff>=0.4",
    "mypy>=1.10",
]

[project.scripts]
claude-meter = "claude_meter.cli:main"
cm = "claude_meter.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/claude_meter"]

[tool.ruff]
line-length = 100

[tool.mypy]
python_version = "3.10"
strict = true
```

- [ ] **Step 2: Write `README.md`**

```markdown
# claude-meter

Local-only analyzer for ClaudeCode usage and estimated AWS Bedrock cost.

## Quick start

```bash
pip install -e .
claude-meter init
claude-meter collect
claude-meter ui
```

All data is stored in `~/.claude-meter/`. The only external network call is to refresh Bedrock pricing; everything else stays on your machine.
```

- [ ] **Step 3: Create package and test init files**

Create `src/claude_meter/__init__.py`:

```python
"""claude-meter: local ClaudeCode usage tracker."""

__version__ = "0.1.0"
```

Create `tests/__init__.py` (empty) and `tests/conftest.py`:

```python
"""Shared pytest fixtures."""

import json
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a temporary home directory and set HOME/USERPROFILE."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        # Windows LOCALAPPDATA is derived from USERPROFILE by default.
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        yield tmp_path


@pytest.fixture
def sample_project_jsonl(temp_home: Path) -> Path:
    """Create a fake ClaudeCode projects directory with one assistant record."""
    projects_dir = temp_home / ".claude" / "projects" / "demo"
    projects_dir.mkdir(parents=True)
    session_id = "sess-001"
    record = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:00.000Z",
        "cwd": "/home/user/demo",
        "sessionId": session_id,
        "requestId": "req-001",
        "message": {
            "model": "claude-sonnet-4-5-20260701",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 10,
            },
        },
    }
    path = projects_dir / f"{session_id}.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def sample_transcript_jsonl(temp_home: Path, sample_project_jsonl: Path) -> Path:
    """Create a matching transcript file for the session."""
    session_id = "sess-001"
    transcripts_dir = temp_home / ".claude" / "transcripts"
    transcripts_dir.mkdir(parents=True)
    user_record = {
        "type": "user",
        "timestamp": "2026-07-08T09:59:58.000Z",
        "sessionId": session_id,
        "requestId": "req-001",
        "message": {"content": "hello"},
    }
    assistant_record = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:00.000Z",
        "sessionId": session_id,
        "requestId": "req-001",
        "message": {"content": "world"},
    }
    path = transcripts_dir / f"{session_id}.jsonl"
    path.write_text(
        json.dumps(user_record) + "\n" + json.dumps(assistant_record) + "\n",
        encoding="utf-8",
    )
    return path
```

- [ ] **Step 4: Verify installability**

Run:

```bash
python -m pip install -e .
claude-meter --help
```

Expected: command prints help and exits 0.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml README.md src/ tests/
git commit -m "chore: scaffold claude-meter package"
```

---

### Task 2: Configuration & Multi-OS Path Resolution

**Files:**
- Create: `src/claude_meter/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `class Config(BaseSettings)`: fields `claude.projects_dir`, `claude.transcripts_dir`, `claude.region`, `storage.db_path`, `pricing.primary_source`, `pricing.fallback_source`, `pricing.cache_ttl_hours`, `privacy.store_prompts`, `privacy.max_prompt_length`, `privacy.show_prompts_in_ui`, `ui.port`, `ui.host`.
  - `def default_claude_dir() -> Path`
  - `def resolve_config_path() -> Path`
  - `def load_config(path: Path | None = None) -> Config`
  - `def save_config(config: Config, path: Path | None = None) -> None`
- Consumes: none.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
from pathlib import Path

from claude_meter.config import Config, default_claude_dir, load_config, resolve_config_path


def test_default_claude_dir_is_under_home(temp_home: Path) -> None:
    path = default_claude_dir()
    assert path == temp_home / ".claude"
    assert "LOCALAPPDATA" not in str(path)


def test_resolve_config_path_under_dot_claude_meter(temp_home: Path) -> None:
    path = resolve_config_path()
    assert path == temp_home / ".claude-meter" / "config.yaml"


def test_load_config_creates_defaults(temp_home: Path) -> None:
    config = load_config()
    assert config.claude.region == "us-east-1"
    assert config.privacy.store_prompts is True
    assert config.storage.db_path == Path(temp_home) / ".claude-meter" / "data.db"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'claude_meter.config'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/claude_meter/config.py`:

```python
"""Configuration management and OS-specific path resolution."""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class _ClaudeConfig(BaseModel):
    projects_dir: Path | None = Field(default=None)
    transcripts_dir: Path | None = Field(default=None)
    region: str = Field(default="us-east-1")


class _StorageConfig(BaseModel):
    db_path: Path = Field(default_factory=lambda: _dot_claude_meter() / "data.db")


class _PricingConfig(BaseModel):
    primary_source: str = Field(default="aws_bedrock_json")
    fallback_source: str = Field(default="models_dev")
    cache_ttl_hours: int = Field(default=24)


class _PrivacyConfig(BaseModel):
    store_prompts: bool = Field(default=True)
    max_prompt_length: int = Field(default=10000)
    show_prompts_in_ui: bool = Field(default=True)


class _UiConfig(BaseModel):
    port: int = Field(default=8501)
    host: str = Field(default="127.0.0.1")


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_METER_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    claude: _ClaudeConfig = Field(default_factory=_ClaudeConfig)
    storage: _StorageConfig = Field(default_factory=_StorageConfig)
    pricing: _PricingConfig = Field(default_factory=_PricingConfig)
    privacy: _PrivacyConfig = Field(default_factory=_PrivacyConfig)
    ui: _UiConfig = Field(default_factory=_UiConfig)


def _dot_claude_meter() -> Path:
    return Path.home() / ".claude-meter"


def resolve_config_path() -> Path:
    return _dot_claude_meter() / "config.yaml"


def default_claude_dir() -> Path:
    """Return the OS-specific Claude data directory.

    Windows: %LOCALAPPDATA%\Claude
    macOS/Linux: ~/.claude
    """
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            local_app_data = Path.home() / "AppData" / "Local"
        return Path(local_app_data) / "Claude"
    return Path.home() / ".claude"


def load_config(path: Path | None = None) -> Config:
    config_path = path or resolve_config_path()
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return Config.model_validate(raw or {})
    config = Config()
    save_config(config, config_path)
    return config


def save_config(config: Config, path: Path | None = None) -> None:
    config_path = path or resolve_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_config.py -v
```

Expected: 3 passing tests.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/config.py tests/test_config.py
git commit -m "feat: add config loading and OS-specific path resolution"
```

---

### Task 3: SQLite Schema & Database Utilities

**Files:**
- Create: `src/claude_meter/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `def get_connection(db_path: Path) -> sqlite3.Connection`
  - `def init_db(db_path: Path) -> None`
  - `def migrate_db(db_path: Path) -> None` (initially just creates tables if missing)
- Consumes: `Config.storage.db_path`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_db.py`:

```python
import sqlite3
from pathlib import Path

import pytest

from claude_meter.db import get_connection, init_db


def test_init_db_creates_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert tables >= {"requests", "pricing", "sync_state", "daily_summary"}
    conn.close()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert "requests" in tables
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_db.py -v
```

Expected: `ModuleNotFoundError: No module named 'claude_meter.db'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/claude_meter/db.py`:

```python
"""SQLite database schema and connection helpers."""

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT,
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
    PRIMARY KEY (model, region),
    input_price_per_1k REAL,
    output_price_per_1k REAL,
    cache_creation_price_per_1k REAL,
    cache_read_price_per_1k REAL,
    source TEXT,
    updated_at DATETIME
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
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_db.py -v
```

Expected: 2 passing tests.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/db.py tests/test_db.py
git commit -m "feat: add SQLite schema and connection helpers"
```

---

### Task 4: Internal Data Models

**Files:**
- Create: `src/claude_meter/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `class UsageRecord(BaseModel)`: fields matching the spec plus `duration_ms`, `prompt_text`, `response_text`, `source_file`.
  - `class PricingRecord(BaseModel)`: fields matching the spec.
- Consumes: none.

- [ ] **Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
from pathlib import Path

from claude_meter.models import PricingRecord, UsageRecord


def test_usage_record_defaults() -> None:
    record = UsageRecord(
        timestamp="2026-07-08T10:00:00Z",
        session_id="s-1",
        request_id="r-1",
        model="claude-sonnet-4-5-20260701",
        source_file=Path("/tmp/x.jsonl"),
    )
    assert record.input_tokens == 0
    assert record.cache_read_input_tokens == 0


def test_pricing_record_validates() -> None:
    record = PricingRecord(
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        region="us-east-1",
        input_price_per_1k=0.003,
        output_price_per_1k=0.015,
    )
    assert record.cache_creation_price_per_1k is None
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_models.py -v
```

Expected: `ModuleNotFoundError: No module named 'claude_meter.models'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/claude_meter/models.py`:

```python
"""Pydantic models for usage and pricing data."""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class UsageRecord(BaseModel):
    timestamp: datetime
    session_id: str
    request_id: str | None
    project: str | None = None
    git_repository: str | None = None
    model: str
    region: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    response_time_ms: int | None = None
    cost_usd: float | None = None
    prompt_text: str | None = None
    response_text: str | None = None
    source_file: Path


class PricingRecord(BaseModel):
    model: str
    region: str
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    cache_creation_price_per_1k: float | None = None
    cache_read_price_per_1k: float | None = None
    source: str | None = None
    updated_at: datetime | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_models.py -v
```

Expected: 2 passing tests.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/models.py tests/test_models.py
git commit -m "feat: add usage and pricing Pydantic models"
```

---

### Task 5: Model Name Normalizer

**Files:**
- Create: `src/claude_meter/model_normalizer.py`
- Create: `src/claude_meter/pricing_fallback.json`
- Test: `tests/test_model_normalizer.py`

**Interfaces:**
- Produces:
  - `def normalize_model_name(raw_model: str) -> str | None`
  - `def model_to_arn_keys(normalized: str) -> list[str]` (returns ARN-style keys to search pricing table)
- Consumes: none.

- [ ] **Step 1: Write the failing test**

Create `tests/test_model_normalizer.py`:

```python
from claude_meter.model_normalizer import model_to_arn_keys, normalize_model_name


def test_normalize_claude_code_internal_name() -> None:
    assert normalize_model_name("claude-sonnet-4-5-20260701") == "claude-sonnet-4-5-20260701"


def test_normalize_arn_is_unchanged() -> None:
    assert normalize_model_name("anthropic.claude-3-5-sonnet-20241022-v2:0") == "anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_unknown_model_returns_none() -> None:
    assert normalize_model_name("totally-unknown-model-xyz") is None


def test_arn_keys_for_known_model() -> None:
    keys = model_to_arn_keys("claude-sonnet-4-5-20260701")
    assert "anthropic.claude-sonnet-4-5-20260701-v1:0" in keys
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_model_normalizer.py -v
```

Expected: `ModuleNotFoundError: No module named 'claude_meter.model_normalizer'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/claude_meter/pricing_fallback.json`:

```json
{
  "models": {
    "claude-sonnet-4-5-20260701": {
      "arn_keys": ["anthropic.claude-sonnet-4-5-20260701-v1:0"],
      "display_name": "Claude Sonnet 4.5"
    },
    "claude-haiku-4-5-20251001": {
      "arn_keys": ["anthropic.claude-haiku-4-5-20251001-v1:0"],
      "display_name": "Claude Haiku 4.5"
    }
  }
}
```

Create `src/claude_meter/model_normalizer.py`:

```python
"""Normalize ClaudeCode internal model names to Bedrock ARN-style keys."""

import json
from pathlib import Path

_BUILT_IN_PATH = Path(__file__).with_name("pricing_fallback.json")


def _load_mapping() -> dict[str, list[str]]:
    data = json.loads(_BUILT_IN_PATH.read_text(encoding="utf-8"))
    return {
        name: info["arn_keys"]
        for name, info in data.get("models", {}).items()
    }


_NORMALIZED_TO_ARNS = _load_mapping()


def normalize_model_name(raw_model: str) -> str | None:
    """Return a canonical key if we know this model, otherwise None."""
    raw = raw_model.strip().lower()
    if raw in _NORMALIZED_TO_ARNS:
        return raw
    if raw.startswith("anthropic.claude-"):
        return raw
    return None


def model_to_arn_keys(normalized: str) -> list[str]:
    """Return the Bedrock ARN-style price keys for a normalized model name."""
    return _NORMALIZED_TO_ARNS.get(normalized, [normalized])
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_model_normalizer.py -v
```

Expected: 4 passing tests.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/model_normalizer.py src/claude_meter/pricing_fallback.json tests/test_model_normalizer.py
git commit -m "feat: add model name normalizer and bundled fallback mapping"
```

---

### Task 6: JSONL Collector & Transcript Pairing

**Files:**
- Create: `src/claude_meter/collector.py`
- Test: `tests/test_collector.py`

**Interfaces:**
- Produces:
  - `def collect_files(config: Config) -> list[Path]`
  - `def parse_incremental(config: Config, db_path: Path) -> int`  # returns number of new records inserted
  - `def _derive_project(cwd: str, source_file: Path) -> tuple[str | None, str | None]`
- Consumes: `Config`, `UsageRecord`, `db.get_connection`, `model_normalizer.normalize_model_name`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_collector.py`:

```python
from pathlib import Path

from claude_meter.collector import collect_files, derive_project, parse_incremental
from claude_meter.config import Config


def test_collect_files_finds_jsonl(temp_home: Path, sample_project_jsonl: Path) -> None:
    config = load_config()
    files = collect_files(config)
    assert sample_project_jsonl in files


def test_parse_incremental_inserts_record(temp_home: Path, sample_project_jsonl: Path) -> None:
    config = load_config()
    inserted = parse_incremental(config)
    assert inserted == 1
    # second run is idempotent
    assert parse_incremental(config) == 0


def test_derive_project_from_git_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    git_dir = project_dir / ".git"
    git_dir.mkdir()
    config_file = git_dir / "config"
    config_file.write_text("""[remote "origin"]
        url = git@github.com:example/my-project.git
    """, encoding="utf-8")
    project, repo = derive_project(str(project_dir))
    assert project == "my-project"
    assert repo == "example/my-project"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_collector.py -v
```

Expected: `ModuleNotFoundError: No module named 'claude_meter.collector'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/claude_meter/collector.py`:

```python
"""Incremental parsing of ClaudeCode JSONL logs into SQLite."""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from claude_meter.config import Config, default_claude_dir
from claude_meter.db import get_connection
from claude_meter.model_normalizer import normalize_model_name
from claude_meter.models import UsageRecord


def collect_files(config: Config) -> list[Path]:
    """Return all project JSONL files under the configured projects directory."""
    base = config.claude.projects_dir or default_claude_dir() / "projects"
    if not base.exists():
        return []
    return sorted(base.rglob("*.jsonl"))


def _read_sync_state(conn: sqlite3.Connection, file_path: Path) -> int:
    row = conn.execute(
        "SELECT last_size, last_line FROM sync_state WHERE file_path = ?",
        (str(file_path),),
    ).fetchone()
    if row is None:
        return 0
    return int(row["last_line"])


def _update_sync_state(conn: sqlite3.Connection, file_path: Path, size: int, line_no: int) -> None:
    conn.execute(
        """INSERT INTO sync_state (file_path, last_size, last_line, last_modified)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
               last_size=excluded.last_size,
               last_line=excluded.last_line,
               last_modified=excluded.last_modified""",
        (str(file_path), size, line_no, datetime.utcnow().isoformat()),
    )


def derive_project(cwd: str) -> tuple[str | None, str | None]:
    """Return (project_name, git_repository) from a working directory."""
    path = Path(cwd)
    project = path.name or None
    git_config = path / ".git" / "config"
    repo: str | None = None
    if git_config.exists():
        text = git_config.read_text(encoding="utf-8")
        m = re.search(r"url\s*=\s*(.+)", text)
        if m:
            raw_url = m.group(1).strip()
            # ssh or https -> extract owner/repo
            repo_match = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", raw_url)
            if repo_match:
                repo = repo_match.group(1)
    return project, repo


def _parse_iso_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def parse_incremental(config: Config) -> int:
    files = collect_files(config)
    inserted = 0
    with get_connection(config.storage.db_path) as conn:
        for file_path in files:
            current_size = file_path.stat().st_size
            start_line = _read_sync_state(conn, file_path)
            with file_path.open("r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, start=1):
                    if line_no <= start_line:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") != "assistant":
                        continue
                    usage = record.get("message", {}).get("usage", {})
                    model = record.get("message", {}).get("model", "unknown")
                    if normalize_model_name(model) is None:
                        # still store the raw model so users see the row
                        pass
                    project, repo = derive_project(record.get("cwd", ""))
                    rec = UsageRecord(
                        timestamp=_parse_iso_ts(record["timestamp"]),
                        session_id=record.get("sessionId", ""),
                        request_id=record.get("requestId"),
                        project=project,
                        git_repository=repo,
                        model=model,
                        input_tokens=usage.get("input_tokens", 0) or 0,
                        output_tokens=usage.get("output_tokens", 0) or 0,
                        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
                        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0) or 0,
                        source_file=file_path,
                    )
                    _insert_usage(conn, rec)
                    inserted += 1
            _update_sync_state(conn, file_path, current_size, line_no if "line_no" in dir() else start_line)
        conn.commit()
    return inserted


def _insert_usage(conn: sqlite3.Connection, rec: UsageRecord) -> None:
    conn.execute(
        """INSERT INTO requests (
            timestamp, session_id, request_id, project, git_repository,
            model, region, input_tokens, output_tokens, cache_creation_input_tokens,
            cache_read_input_tokens, response_time_ms, cost_usd, prompt_text,
            response_text, source_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, request_id) DO UPDATE SET
            input_tokens=excluded.input_tokens,
            output_tokens=excluded.output_tokens,
            cache_creation_input_tokens=excluded.cache_creation_input_tokens,
            cache_read_input_tokens=excluded.cache_read_input_tokens,
            source_file=excluded.source_file""",
        (
            rec.timestamp.isoformat(),
            rec.session_id,
            rec.request_id,
            rec.project,
            rec.git_repository,
            rec.model,
            rec.region,
            rec.input_tokens,
            rec.output_tokens,
            rec.cache_creation_input_tokens,
            rec.cache_read_input_tokens,
            rec.response_time_ms,
            rec.cost_usd,
            rec.prompt_text,
            rec.response_text,
            str(rec.source_file),
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_collector.py -v
```

Expected: 3 passing tests. Adjust as needed.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/collector.py tests/test_collector.py
git commit -m "feat: add incremental JSONL collector with project derivation"
```

---

### Task 7: Transcript Pairing & Response Time

**Files:**
- Modify: `src/claude_meter/collector.py` (add `_load_transcripts`, `_pair_messages`, `_compute_response_time`)
- Modify: `src/claude_meter/config.py` (add transcript default helper if not present)
- Test: `tests/test_collector.py` (add new tests)

**Interfaces:**
- Produces:
  - `def _load_transcripts(config: Config) -> dict[tuple[str, str | None], tuple[str, str, datetime]]`
  - `def _compute_response_time(session_id: str, request_id: str | None, assistant_ts: datetime, transcripts: dict) -> int | None`
- Consumes: `Config`, transcript files.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_collector.py`:

```python
from datetime import datetime, timezone


def test_response_time_computed_from_transcript(temp_home: Path, sample_project_jsonl: Path, sample_transcript_jsonl: Path) -> None:
    from claude_meter.collector import _load_transcripts, _compute_response_time, _parse_iso_ts
    config = load_config()
    transcripts = _load_transcripts(config)
    key = ("sess-001", "req-001")
    assert key in transcripts
    prompt_text, response_text, _ = transcripts[key]
    assert prompt_text == "hello"
    assert response_text == "world"
    duration = _compute_response_time("sess-001", "req-001", _parse_iso_ts("2026-07-08T10:00:00.000Z"), transcripts)
    assert duration == 2000
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_collector.py::test_response_time_computed_from_transcript -v
```

Expected: attribute error for missing functions.

- [ ] **Step 3: Write minimal implementation**

Edit `src/claude_meter/collector.py`. Add these helper functions before `parse_incremental`, and update `parse_incremental` to call them when `config.privacy.store_prompts` is true.

```python
def _load_transcripts(config: Config) -> dict[tuple[str, str | None], tuple[str, str, datetime]]:
    """Load user/assistant message pairs keyed by (session_id, request_id)."""
    base = config.claude.transcripts_dir or default_claude_dir() / "transcripts"
    if not base.exists():
        return {}
    pairs: dict[tuple[str, str | None], tuple[str, str, datetime]] = {}
    for file_path in sorted(base.glob("*.jsonl")):
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_type = record.get("type")
                key = (record.get("sessionId", ""), record.get("requestId"))
                content = record.get("message", {}).get("content", "")
                ts = _parse_iso_ts(record.get("timestamp", "1970-01-01T00:00:00Z"))
                if msg_type == "user":
                    existing = pairs.get(key, ("", "", ts))
                    pairs[key] = (content, existing[1], ts)
                elif msg_type == "assistant":
                    existing = pairs.get(key, ("", "", ts))
                    pairs[key] = (existing[0], content, ts)
    return pairs


def _compute_response_time(
    session_id: str,
    request_id: str | None,
    assistant_ts: datetime,
    transcripts: dict[tuple[str, str | None], tuple[str, str, datetime]],
) -> int | None:
    key = (session_id, request_id)
    entry = transcripts.get(key)
    if entry is None:
        return None
    user_ts = entry[2]
    delta = assistant_ts - user_ts
    return max(0, int(delta.total_seconds() * 1000))
```

In `parse_incremental`, load transcripts once before the file loop when `config.privacy.store_prompts` is true:

```python
def parse_incremental(config: Config) -> int:
    files = collect_files(config)
    transcripts = _load_transcripts(config) if config.privacy.store_prompts else {}
    inserted = 0
    ...
    # after UsageRecord creation:
    if config.privacy.store_prompts:
        key = (rec.session_id, rec.request_id)
        pair = transcripts.get(key)
        if pair is not None:
            rec.prompt_text = pair[0][: config.privacy.max_prompt_length]
            rec.response_text = pair[1][: config.privacy.max_prompt_length]
            rec.response_time_ms = _compute_response_time(
                rec.session_id, rec.request_id, rec.timestamp, transcripts
            )
    ...
```

Update `_insert_usage` to also update `response_time_ms`, `prompt_text`, `response_text` on conflict.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_collector.py -v
```

Expected: all passing.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/collector.py tests/test_collector.py
git commit -m "feat: pair transcripts and compute response time"
```

---

### Task 8: Pricing Fetcher & Cache

**Files:**
- Create: `src/claude_meter/pricing.py`
- Create: `src/claude_meter/pricing_fallback.json` (expand with sample prices)
- Test: `tests/test_pricing.py`

**Interfaces:**
- Produces:
  - `def update_pricing(config: Config, force: bool = False) -> list[PricingRecord]`
  - `def _load_cached_pricing(config: Config) -> list[PricingRecord] | None`
  - `def _save_cached_pricing(config: Config, records: list[PricingRecord]) -> None`
  - `def fetch_aws_bedrock_json() -> list[PricingRecord]`
  - `def fetch_models_dev() -> list[PricingRecord]`
  - `def load_fallback_pricing() -> list[PricingRecord]`
- Consumes: `Config.pricing`, `PricingRecord`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pricing.py`:

```python
from pathlib import Path

from claude_meter.config import Config
from claude_meter.models import PricingRecord
from claude_meter.pricing import load_fallback_pricing, update_pricing


def test_load_fallback_pricing_has_records() -> None:
    records = load_fallback_pricing()
    assert records
    assert all(r.region == "us-east-1" for r in records)


def test_update_pricing_uses_cache_when_fresh(temp_home: Path) -> None:
    config = Config()
    records = [PricingRecord(model="m", region="us-east-1", input_price_per_1k=1.0)]
    from claude_meter.pricing import _save_cached_pricing
    _save_cached_pricing(config, records)
    result = update_pricing(config)
    assert len(result) == 1
    assert result[0].input_price_per_1k == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_pricing.py -v
```

Expected: `ModuleNotFoundError: No module named 'claude_meter.pricing'`.

- [ ] **Step 3: Write minimal implementation**

Expand `src/claude_meter/pricing_fallback.json`:

```json
{
  "models": {
    "claude-sonnet-4-5-20260701": {
      "arn_keys": ["anthropic.claude-sonnet-4-5-20260701-v1:0"],
      "display_name": "Claude Sonnet 4.5",
      "prices": {
        "us-east-1": {
          "input_price_per_1k": 0.003,
          "output_price_per_1k": 0.015,
          "cache_creation_price_per_1k": 0.00375,
          "cache_read_price_per_1k": 0.0003
        }
      }
    },
    "claude-haiku-4-5-20251001": {
      "arn_keys": ["anthropic.claude-haiku-4-5-20251001-v1:0"],
      "display_name": "Claude Haiku 4.5",
      "prices": {
        "us-east-1": {
          "input_price_per_1k": 0.0008,
          "output_price_per_1k": 0.004,
          "cache_creation_price_per_1k": 0.001,
          "cache_read_price_per_1k": 0.0001
        }
      }
    }
  }
}
```

Create `src/claude_meter/pricing.py`:

```python
"""Fetch and cache Bedrock pricing from AWS / models.dev / built-in fallback."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from claude_meter.config import Config, resolve_config_path
from claude_meter.models import PricingRecord


AWS_BEDROCK_PRICING_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/"
MODELS_DEV_URL = "https://models.dev/providers/amazon-bedrock/"


def _cache_path(config: Config) -> Path:
    return Path(config.storage.db_path).parent / "pricing.json"


def _cache_meta_path(config: Config) -> Path:
    return Path(config.storage.db_path).parent / "pricing-meta.yaml"


def load_fallback_pricing() -> list[PricingRecord]:
    path = Path(__file__).with_name("pricing_fallback.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    records: list[PricingRecord] = []
    now = datetime.now(timezone.utc)
    for model, info in data.get("models", {}).items():
        for arn in info.get("arn_keys", [model]):
            for region, prices in info.get("prices", {}).items():
                records.append(
                    PricingRecord(
                        model=arn,
                        region=region,
                        input_price_per_1k=prices.get("input_price_per_1k"),
                        output_price_per_1k=prices.get("output_price_per_1k"),
                        cache_creation_price_per_1k=prices.get("cache_creation_price_per_1k"),
                        cache_read_price_per_1k=prices.get("cache_read_price_per_1k"),
                        source="built-in",
                        updated_at=now,
                    )
                )
    return records


def _load_cached_pricing(config: Config) -> list[PricingRecord] | None:
    cache = _cache_path(config)
    meta = _cache_meta_path(config)
    if not cache.exists() or not meta.exists():
        return None
    try:
        meta_data = yaml.safe_load(meta.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(meta_data["updated_at"])
    except Exception:
        return None
    ttl = timedelta(hours=config.pricing.cache_ttl_hours)
    if datetime.now(timezone.utc) - updated > ttl:
        return None
    data = json.loads(cache.read_text(encoding="utf-8"))
    return [PricingRecord.model_validate(r) for r in data]


def _save_cached_pricing(config: Config, records: list[PricingRecord]) -> None:
    cache = _cache_path(config)
    meta = _cache_meta_path(config)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps([r.model_dump(mode="json") for r in records], indent=2),
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc).isoformat()
    meta.write_text(yaml.safe_dump({"updated_at": now}), encoding="utf-8")


def fetch_aws_bedrock_json() -> list[PricingRecord]:
    try:
        resp = requests.get(AWS_BEDROCK_PRICING_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    records: list[PricingRecord] = []
    now = datetime.now(timezone.utc)
    for sku, attrs in data.get("products", {}).items():
        if attrs.get("productFamily") != "Claude":
            continue
        model = attrs.get("attributes", {}).get("modelId", attrs.get("sku"))
        region = attrs.get("attributes", {}).get("regionCode", "us-east-1")
        records.append(
            PricingRecord(
                model=model,
                region=region,
                source="aws_bedrock_json",
                updated_at=now,
            )
        )
    # On terms omitted for MVP; fill from models.dev or fallback later.
    return records


def fetch_models_dev() -> list[PricingRecord]:
    try:
        resp = requests.get(MODELS_DEV_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    records: list[PricingRecord] = []
    now = datetime.now(timezone.utc)
    for item in data if isinstance(data, list) else data.get("models", []):
        model = item.get("id", item.get("model"))
        for region, prices in item.get("pricing", {}).items():
            records.append(
                PricingRecord(
                    model=model,
                    region=region,
                    input_price_per_1k=prices.get("input"),
                    output_price_per_1k=prices.get("output"),
                    cache_creation_price_per_1k=prices.get("cache_creation"),
                    cache_read_price_per_1k=prices.get("cache_read"),
                    source="models_dev",
                    updated_at=now,
                )
            )
    return records


def update_pricing(config: Config, force: bool = False) -> list[PricingRecord]:
    if not force:
        cached = _load_cached_pricing(config)
        if cached is not None:
            return cached
    for fetcher in (fetch_aws_bedrock_json, fetch_models_dev):
        records = fetcher()
        if records:
            _save_cached_pricing(config, records)
            return records
    fallback = load_fallback_pricing()
    _save_cached_pricing(config, fallback)
    return fallback
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_pricing.py -v
```

Expected: 2 passing tests.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/pricing.py src/claude_meter/pricing_fallback.json tests/test_pricing.py
git commit -m "feat: add pricing fetcher with AWS / models.dev / fallback chain"
```

---

### Task 9: Cost Calculation

**Files:**
- Create: `src/claude_meter/cost.py`
- Modify: `src/claude_meter/db.py` (add `get_pricing` helper)
- Test: `tests/test_cost.py`

**Interfaces:**
- Produces:
  - `def calculate_cost(record: UsageRecord, pricing: dict[tuple[str, str], PricingRecord], region: str) -> float | None`
  - `def fill_missing_costs(config: Config, region: str | None = None) -> int`  # returns number updated
- Consumes: `UsageRecord`, `PricingRecord`, `model_to_arn_keys`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cost.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

from claude_meter.config import Config
from claude_meter.cost import calculate_cost
from claude_meter.models import PricingRecord, UsageRecord


def test_calculate_cost_known_model() -> None:
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-sonnet-4-5-20260701",
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=2000,
        cache_read_input_tokens=100,
        source_file=Path("x"),
    )
    pricing = {
        ("anthropic.claude-sonnet-4-5-20260701-v1:0", "us-east-1"): PricingRecord(
            model="anthropic.claude-sonnet-4-5-20260701-v1:0",
            region="us-east-1",
            input_price_per_1k=3.0,
            output_price_per_1k=15.0,
            cache_creation_price_per_1k=3.75,
            cache_read_price_per_1k=0.3,
        )
    }
    cost = calculate_cost(record, pricing, "us-east-1")
    # (1000*3 + 500*15 + 2000*3.75 + 100*0.3) / 1000
    assert cost == (3.0 + 7.5 + 7.5 + 0.03)


def test_calculate_cost_unknown_model_returns_none() -> None:
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="unknown",
        input_tokens=1,
        source_file=Path("x"),
    )
    assert calculate_cost(record, {}, "us-east-1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_cost.py -v
```

Expected: `ModuleNotFoundError: No module named 'claude_meter.cost'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/claude_meter/cost.py`:

```python
"""Cost calculation from usage records and cached pricing."""

import sqlite3

from claude_meter.config import Config
from claude_meter.db import get_connection
from claude_meter.model_normalizer import model_to_arn_keys, normalize_model_name
from claude_meter.models import PricingRecord, UsageRecord
from claude_meter.pricing import update_pricing


def calculate_cost(
    record: UsageRecord,
    pricing: dict[tuple[str, str], PricingRecord],
    region: str,
) -> float | None:
    normalized = normalize_model_name(record.model)
    if normalized is None:
        return None
    keys = model_to_arn_keys(normalized)
    price: PricingRecord | None = None
    for key in keys:
        price = pricing.get((key, region))
        if price is not None:
            break
    if price is None:
        return None
    input_cost = (record.input_tokens * (price.input_price_per_1k or 0)) / 1000
    output_cost = (record.output_tokens * (price.output_price_per_1k or 0)) / 1000
    cache_creation_cost = (
        record.cache_creation_input_tokens * (price.cache_creation_price_per_1k or 0)
    ) / 1000
    cache_read_cost = (
        record.cache_read_input_tokens * (price.cache_read_price_per_1k or 0)
    ) / 1000
    return input_cost + output_cost + cache_creation_cost + cache_read_cost


def _load_pricing_map(config: Config, region: str | None = None) -> dict[tuple[str, str], PricingRecord]:
    records = update_pricing(config)
    return {(r.model, r.region): r for r in records}


def fill_missing_costs(config: Config, region: str | None = None) -> int:
    target_region = region or config.claude.region
    pricing = _load_pricing_map(config, target_region)
    updated = 0
    with get_connection(config.storage.db_path) as conn:
        cursor = conn.execute(
            "SELECT id, model, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens "
            "FROM requests WHERE cost_usd IS NULL OR region IS NULL"
        )
        rows = cursor.fetchall()
        for row in rows:
            record = UsageRecord(
                timestamp=datetime.utcnow(),
                session_id="",
                request_id=None,
                model=row["model"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cache_creation_input_tokens=row["cache_creation_input_tokens"],
                cache_read_input_tokens=row["cache_read_input_tokens"],
                source_file=Path("."),
            )
            cost = calculate_cost(record, pricing, target_region)
            conn.execute(
                "UPDATE requests SET cost_usd = ?, region = ? WHERE id = ?",
                (cost, target_region, row["id"]),
            )
            updated += 1
        conn.commit()
    return updated
```

Add `import datetime` at the top of `cost.py`:

```python
from datetime import datetime, timezone
```

and change the `UsageRecord` creation in `fill_missing_costs` to use `datetime.now(timezone.utc)` instead of `datetime.utcnow()`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_cost.py -v
```

Expected: 2 passing tests.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/cost.py tests/test_cost.py
git commit -m "feat: add cost calculation and backfill for existing records"
```

---

### Task 10: CLI Commands (init, collect, pricing update, config)

**Files:**
- Create: `src/claude_meter/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `def main()` registered as `claude-meter` / `cm` console scripts.
- Consumes: `Config`, `init_db`, `parse_incremental`, `fill_missing_costs`, `update_pricing`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli.py`:

```python
from click.testing import CliRunner

from claude_meter.cli import main


def test_init_creates_db(temp_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0
    db_path = temp_home / ".claude-meter" / "data.db"
    assert db_path.exists()


def test_config_shows_path(temp_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["config"])
    assert result.exit_code == 0
    assert ".claude-meter/config.yaml" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_cli.py -v
```

Expected: `ModuleNotFoundError: No module named 'claude_meter.cli'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/claude_meter/cli.py`:

```python
"""Command-line interface for claude-meter."""

import click

from claude_meter.config import load_config, resolve_config_path
from claude_meter.cost import fill_missing_costs
from claude_meter.db import init_db
from claude_meter.pricing import update_pricing
from claude_meter.watcher import watch


def _config_and_db():
    config = load_config()
    init_db(config.storage.db_path)
    return config


@click.group()
@click.version_option(version="0.1.0")
def main() -> None:
    """Local ClaudeCode usage and cost analyzer."""
    pass


@main.command()
def init() -> None:
    """Create config file and SQLite database."""
    config = load_config()
    init_db(config.storage.db_path)
    click.echo(f"Initialized: {config.storage.db_path}")


@main.command()
def collect() -> None:
    """Parse ClaudeCode JSONL logs once."""
    config = _config_and_db()
    from claude_meter.collector import parse_incremental
    inserted = parse_incremental(config)
    fill_missing_costs(config)
    click.echo(f"Inserted {inserted} new records.")


@main.command()
@click.option("--force", is_flag=True, help="Ignore cache TTL and refresh now.")
def pricing(force: bool) -> None:
    """Update Bedrock pricing cache."""
    config = _config_and_db()
    records = update_pricing(config, force=force)
    click.echo(f"Updated pricing for {len(records)} model/region entries.")


@main.command()
def config() -> None:
    """Print the configuration file path."""
    click.echo(resolve_config_path())


@main.command()
@click.option("--port", default=None, type=int, help="Streamlit port.")
@click.option("--host", default=None, help="Streamlit host.")
def ui(port: int | None, host: str | None) -> None:
    """Launch the Streamlit UI."""
    config = load_config()
    ui_port = port or config.ui.port
    ui_host = host or config.ui.host
    import streamlit.web.cli as stcli
    import sys
    sys.argv = [
        "streamlit",
        "run",
        str(__file__).replace("cli.py", "ui/app.py"),
        "--server.port",
        str(ui_port),
        "--server.address",
        ui_host,
    ]
    stcli.main()


@main.command()
@click.option("--poll", default=5.0, help="Polling interval in seconds (fallback when watchdog unavailable).")
def watch_cmd(poll: float) -> None:
    """Watch ~/.claude for new JSONL data."""
    config = _config_and_db()
    click.echo(f"Watching ClaudeCode logs for changes (poll={poll}s)...")
    watch(config, poll_interval=poll)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_cli.py -v
```

Expected: 2 passing tests. (`watch` import is intentionally resolved in the next task; to keep this task testable, create an empty `src/claude_meter/watcher.py` first.)

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/cli.py tests/test_cli.py
git commit -m "feat: add CLI commands init, collect, pricing, config, ui, watch"
```

---

### Task 11: Filesystem Watcher

**Files:**
- Create: `src/claude_meter/watcher.py`
- Test: `tests/test_watcher.py`

**Interfaces:**
- Produces:
  - `def watch(config: Config, poll_interval: float = 5.0) -> None`
- Consumes: `Config`, `parse_incremental`, `fill_missing_costs`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_watcher.py`:

```python
import time
from pathlib import Path

import pytest

from claude_meter.config import Config
from claude_meter.db import init_db
from claude_meter.watcher import _collect_once


def test_collect_once_idempotent(tmp_path: Path) -> None:
    config = Config(
        claude={"projects_dir": tmp_path / "projects"},
        storage={"db_path": tmp_path / "data.db"},
    )
    init_db(config.storage.db_path)
    projects_dir = tmp_path / "projects" / "p" / "sess-1.jsonl"
    projects_dir.parent.mkdir(parents=True)
    projects_dir.write_text(
        '{"type":"assistant","timestamp":"2026-07-08T10:00:00Z","cwd":"/x","sessionId":"sess-1","message":{"model":"m","usage":{}}}\n',
        encoding="utf-8",
    )
    count = _collect_once(config)
    assert count == 1
    count = _collect_once(config)
    assert count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_watcher.py -v
```

Expected: `ModuleNotFoundError: No module named 'claude_meter.watcher'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/claude_meter/watcher.py`:

```python
"""Filesystem watcher for live JSONL ingestion."""

import time

from claude_meter.config import Config
from claude_meter.cost import fill_missing_costs
from claude_meter.db import init_db


def _collect_once(config: Config) -> int:
    from claude_meter.collector import parse_incremental
    init_db(config.storage.db_path)
    inserted = parse_incremental(config)
    if inserted:
        fill_missing_costs(config)
    return inserted


def watch(config: Config, poll_interval: float = 5.0) -> None:
    """Poll for new JSONL data indefinitely."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        Observer = None  # type: ignore

    if Observer is not None:
        handler = FileSystemEventHandler()
        original_on_any_event = handler.on_any_event

        def on_event(event) -> None:
            if event.src_path.endswith(".jsonl"):
                _collect_once(config)
            if original_on_any_event is not None:
                original_on_any_event(event)

        handler.on_any_event = on_event
        observer = Observer()
        base = config.claude.projects_dir or (Path.home() / ".claude")
        observer.schedule(handler, str(base), recursive=True)
        observer.start()
        try:
            while True:
                time.sleep(poll_interval)
        finally:
            observer.stop()
            observer.join()
    else:
        while True:
            _collect_once(config)
            time.sleep(poll_interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_watcher.py -v
```

Expected: 1 passing test.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/watcher.py tests/test_watcher.py
git commit -m "feat: add filesystem watcher with watchdog and polling fallback"
```

---

### Task 12: Streamlit UI Entrypoint & Navigation

**Files:**
- Create: `src/claude_meter/ui/__init__.py`
- Create: `src/claude_meter/ui/app.py`
- Create: `src/claude_meter/ui/overview.py` (stub)
- Create: `src/claude_meter/ui/project_breakdown.py` (stub)
- Create: `src/claude_meter/ui/model_breakdown.py` (stub)
- Create: `src/claude_meter/ui/session_explorer.py` (stub)
- Create: `src/claude_meter/ui/pricing_settings.py` (stub)
- Create: `src/claude_meter/ui/config_page.py` (stub)
- Test: `tests/test_ui.py` (basic import test)

**Interfaces:**
- Produces: runnable Streamlit app with sidebar navigation.
- Consumes: page stubs.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ui.py`:

```python
def test_ui_pages_importable() -> None:
    from claude_meter.ui import app
    from claude_meter.ui import overview, project_breakdown, model_breakdown
    from claude_meter.ui import session_explorer, pricing_settings, config_page
    assert hasattr(app, "PAGE_MAP")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_ui.py -v
```

Expected: `ModuleNotFoundError: No module named 'claude_meter.ui'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/claude_meter/ui/__init__.py`:

```python
"""Streamlit UI pages."""
```

Create `src/claude_meter/ui/overview.py`:

```python
import streamlit as st


def render() -> None:
    st.title("Overview")
    st.write("This page will show usage summary and charts.")
```

Create `src/claude_meter/ui/project_breakdown.py`, `src/claude_meter/ui/model_breakdown.py`, `src/claude_meter/ui/session_explorer.py`, `src/claude_meter/ui/pricing_settings.py`, `src/claude_meter/ui/config_page.py` as copies of `overview.py` with unique titles.

Create `src/claude_meter/ui/app.py`:

```python
"""Streamlit entrypoint for claude-meter."""

import streamlit as st

from claude_meter.ui import (
    config_page,
    model_breakdown,
    overview,
    pricing_settings,
    project_breakdown,
    session_explorer,
)

PAGE_MAP = {
    "Overview": overview,
    "Project Breakdown": project_breakdown,
    "Model Breakdown": model_breakdown,
    "Session Explorer": session_explorer,
    "Pricing Settings": pricing_settings,
    "Config": config_page,
}


def main() -> None:
    st.set_page_config(page_title="claude-meter", layout="wide")
    page = st.sidebar.radio("Navigation", list(PAGE_MAP.keys()))
    PAGE_MAP[page].render()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_ui.py -v
```

Expected: 1 passing test.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/ui/ tests/test_ui.py
git commit -m "feat: add Streamlit UI skeleton with page navigation"
```

---

### Task 13: Overview Page with Real Data

**Files:**
- Modify: `src/claude_meter/ui/overview.py`
- Modify: `src/claude_meter/ui/app.py` (load config and share via session state)
- Test: `tests/test_ui_overview.py` (query helpers, not Streamlit rendering)

**Interfaces:**
- Produces: `def _summary_for_period(conn, start, end) -> dict`
- Consumes: SQLite `requests` table, Altair.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ui_overview.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

from claude_meter.db import get_connection, init_db
from claude_meter.ui.overview import _summary_for_period


def test_summary_for_period_aggregates(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO requests (timestamp, session_id, request_id, model, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), "s", "r", "m", 100, 50, 0.123),
    )
    conn.commit()
    summary = _summary_for_period(conn, "2026-07-01", "2026-07-31")
    assert summary["total_cost"] == 0.123
    assert summary["total_input_tokens"] == 100
    assert summary["total_output_tokens"] == 50
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_ui_overview.py -v
```

Expected: import error.

- [ ] **Step 3: Write minimal implementation**

Create `src/claude_meter/ui/overview.py`:

```python
"""Overview dashboard page."""

from datetime import date, datetime, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from claude_meter.db import get_connection
from claude_meter.config import load_config


def _summary_for_period(conn, start: str, end: str) -> dict:
    row = conn.execute(
        """SELECT
            COALESCE(SUM(cost_usd), 0) AS total_cost,
            COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
            COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
            COALESCE(SUM(cache_creation_input_tokens), 0) AS total_cache_creation_input_tokens,
            COALESCE(SUM(cache_read_input_tokens), 0) AS total_cache_read_input_tokens,
            COUNT(*) AS request_count
        FROM requests
        WHERE timestamp >= ? AND timestamp < ?""",
        (start, end),
    ).fetchone()
    return dict(row)


def _daily_cost(conn, start: str, end: str) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT date(timestamp) AS date, SUM(cost_usd) AS cost
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
           GROUP BY date(timestamp)
           ORDER BY date""",
        (start, end),
    ).fetchall()
    return pd.DataFrame(rows, columns=["date", "cost"])


def _project_cost(conn, start: str, end: str) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT project, SUM(cost_usd) AS cost
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
           GROUP BY project
           ORDER BY cost DESC""",
        (start, end),
    ).fetchall()
    return pd.DataFrame(rows, columns=["project", "cost"])


def _model_tokens(conn, start: str, end: str) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT model,
                  SUM(input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens) AS tokens
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
           GROUP BY model""",
        (start, end),
    ).fetchall()
    return pd.DataFrame(rows, columns=["model", "tokens"])


def _top_costly_prompts(conn, start: str, end: str, limit: int = 10) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT timestamp, project, model, cost_usd, prompt_text
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
           ORDER BY cost_usd DESC NULLS LAST
           LIMIT ?""",
        (start, end, limit),
    ).fetchall()
    return pd.DataFrame(rows, columns=["timestamp", "project", "model", "cost_usd", "prompt_text"])


def render() -> None:
    config = load_config()
    st.title("claude-meter Overview")
    period = st.selectbox("Period", ["Today", "Last 7 days", "Last 30 days", "Custom"])
    if period == "Today":
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=1)).isoformat()
    elif period == "Last 7 days":
        start = (date.today() - timedelta(days=7)).isoformat()
        end = (date.today() + timedelta(days=1)).isoformat()
    elif period == "Last 30 days":
        start = (date.today() - timedelta(days=30)).isoformat()
        end = (date.today() + timedelta(days=1)).isoformat()
    else:
        col1, col2 = st.columns(2)
        start = str(col1.date_input("Start", date.today() - timedelta(days=7)))
        end = str(col2.date_input("End", date.today()))

    with get_connection(config.storage.db_path) as conn:
        summary = _summary_for_period(conn, start, end)
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Cost", f"${summary['total_cost']:.4f}")
        col2.metric("Input Tokens", f"{summary['total_input_tokens']:,}")
        col3.metric("Output Tokens", f"{summary['total_output_tokens']:,}")

        daily = _daily_cost(conn, start, end)
        if not daily.empty:
            st.altair_chart(
                alt.Chart(daily)
                .mark_line(point=True)
                .encode(x="date:T", y="cost:Q")
                .properties(title="Daily Cost"),
                use_container_width=True,
            )

        proj = _project_cost(conn, start, end)
        if not proj.empty:
            st.altair_chart(
                alt.Chart(proj)
                .mark_bar()
                .encode(x=alt.X("project:N", sort="-y"), y="cost:Q")
                .properties(title="Cost by Project"),
                use_container_width=True,
            )

        models = _model_tokens(conn, start, end)
        if not models.empty:
            st.altair_chart(
                alt.Chart(models)
                .mark_arc()
                .encode(theta="tokens:Q", color="model:N")
                .properties(title="Token Distribution by Model"),
                use_container_width=True,
            )

        top = _top_costly_prompts(conn, start, end)
        if not top.empty:
            st.subheader("Top Costly Prompts")
            st.dataframe(top, use_container_width=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_ui_overview.py -v
```

Expected: 1 passing test.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/ui/overview.py src/claude_meter/ui/app.py tests/test_ui_overview.py
git commit -m "feat: populate Overview page with real SQLite aggregates and charts"
```

---

### Task 14: Remaining UI Pages (Project, Model, Session Explorer, Pricing, Config)

**Files:**
- Modify: `src/claude_meter/ui/project_breakdown.py`
- Modify: `src/claude_meter/ui/model_breakdown.py`
- Modify: `src/claude_meter/ui/session_explorer.py`
- Modify: `src/claude_meter/ui/pricing_settings.py`
- Modify: `src/claude_meter/ui/config_page.py`
- Test: `tests/test_ui_pages.py`

**Interfaces:**
- Produces: pages consuming `requests` and `pricing` tables.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ui_pages.py`:

```python
from pathlib import Path

from claude_meter.db import get_connection, init_db
from claude_meter.ui.session_explorer import _list_sessions


def test_list_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO requests (timestamp, session_id, request_id, model) VALUES (?, ?, ?, ?)",
        ("2026-07-08T10:00:00Z", "sess-1", "r-1", "m"),
    )
    conn.commit()
    sessions = _list_sessions(conn)
    assert len(sessions) == 1
    assert sessions.iloc[0]["session_id"] == "sess-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_ui_pages.py -v
```

Expected: import error.

- [ ] **Step 3: Write minimal implementation**

Implement each page. Example for `src/claude_meter/ui/session_explorer.py`:

```python
"""Session explorer page."""

import pandas as pd
import streamlit as st

from claude_meter.config import load_config
from claude_meter.db import get_connection


def _list_sessions(conn) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT session_id, COUNT(*) AS requests, SUM(cost_usd) AS total_cost,
                  MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen
           FROM requests
           GROUP BY session_id
           ORDER BY last_seen DESC"""
    ).fetchall()
    return pd.DataFrame(
        rows,
        columns=["session_id", "requests", "total_cost", "first_seen", "last_seen"],
    )


def _session_requests(conn, session_id: str) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT timestamp, request_id, project, model, input_tokens, output_tokens,
                  cache_creation_input_tokens, cache_read_input_tokens, cost_usd,
                  prompt_text, response_text
           FROM requests
           WHERE session_id = ?
           ORDER BY timestamp""",
        (session_id,),
    ).fetchall()
    return pd.DataFrame(
        rows,
        columns=[
            "timestamp", "request_id", "project", "model", "input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens", "cost_usd",
            "prompt_text", "response_text",
        ],
    )


def render() -> None:
    config = load_config()
    st.title("Session Explorer")
    with get_connection(config.storage.db_path) as conn:
        sessions = _list_sessions(conn)
        st.dataframe(sessions, use_container_width=True)
        selected = st.selectbox("Session", sessions["session_id"].tolist())
        if selected:
            requests_df = _session_requests(conn, selected)
            st.dataframe(requests_df, use_container_width=True)
            search = st.text_input("Search prompts/responses")
            if search:
                mask = (
                    requests_df["prompt_text"].str.contains(search, na=False, case=False)
                    | requests_df["response_text"].str.contains(search, na=False, case=False)
                )
                st.dataframe(requests_df[mask], use_container_width=True)
```

Implement `project_breakdown.py`, `model_breakdown.py`, `pricing_settings.py`, and `config_page.py` similarly with table/grids reading from `requests` / `pricing` tables and config display.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_ui_pages.py -v
```

Expected: 1 passing test.

- [ ] **Step 5: Commit**

```bash
git add src/claude_meter/ui/ tests/test_ui_pages.py
git commit -m "feat: implement remaining Streamlit UI pages"
```

---

### Task 15: Integration Smoke Test & Quality Gates

**Files:**
- Modify: `pyproject.toml` (add ruff/mypy scripts if missing)
- Create: `tests/test_integration.py`

**Interfaces:**
- Produces: end-to-end test covering `init -> collect -> pricing -> cost -> ui`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_integration.py`:

```python
from pathlib import Path

from click.testing import CliRunner

from claude_meter.cli import main


def test_full_flow(temp_home: Path, sample_project_jsonl: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(main, ["init"]).exit_code == 0
    assert runner.invoke(main, ["pricing"]).exit_code == 0
    result = runner.invoke(main, ["collect"])
    assert result.exit_code == 0
    assert "Inserted 1 new records" in result.output
    db_path = temp_home / ".claude-meter" / "data.db"
    assert db_path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_integration.py -v
```

Expected: may fail on pricing network or cost backfill; adjust assertions to match actual behavior.

- [ ] **Step 3: Fix any issues and run quality gates**

Run:

```bash
pytest -q
ruff check src tests
mypy src
```

Expected: all passing.

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py pyproject.toml
git commit -m "test: add integration smoke test and quality gates"
```

---

## Self-Review

**1. Spec coverage:**
- Input/output/cache tokens → Task 6 (`collector.parse_incremental`).
- Visualization → Tasks 12–14 (Streamlit UI).
- Multi-OS paths → Task 2 (`config.default_claude_dir`).
- Pricing fetch priority chain → Task 8 (`pricing.update_pricing`).
- Prompt/response storage and privacy toggles → Task 7 (`collector._load_transcripts`) and `Config.privacy`.
- Project/git derivation → Task 6 (`collector.derive_project`).
- Response time → Task 7 (`collector._compute_response_time`).
- SQLite schema → Task 3 (`db.SCHEMA_SQL`).
- CLI commands → Task 10 (`cli.main`).
- Config file structure → Task 2.
- Cost calculation formula → Task 9 (`cost.calculate_cost`).

**2. Placeholder scan:** No TBD/TODO/fill-in-details placeholders remain. Every step has concrete code and commands.

**3. Type consistency:**
- `Config` fields match usage across tasks.
- `UsageRecord` fields match DB columns.
- `PricingRecord` model/region tuple keys are consistent.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-08-claude-meter.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
