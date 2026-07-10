# claude-meter Specification

## Overview

`claude-meter` is a local-only Python tool that parses ClaudeCode (AWS Bedrock)
JSONL usage logs, estimates Bedrock costs from cached per-model pricing, stores
everything in SQLite, and exposes the data through a Streamlit Web UI and a
small CLI.

### Target users

- Developers using ClaudeCode via AWS Bedrock
- Organizations that want to keep all usage data local (no external leakage)
- Users who want to track their own or their team's ClaudeCode cost

## Goals

- Measure ClaudeCode token usage (input, output, cache creation, cache read)
- Estimate cost using Bedrock per-model, per-region pricing
- Analyze usage over time, by project, and by model
- Optionally store prompt/response text and response time for detailed
  investigation
- Run on Windows, macOS, and Ubuntu

## Absolute conditions

- **All data is stored locally** under `~/.claude-meter/`
- No external data transmission except pricing fetch
- Pricing is fetched from `models.dev` or AWS public pricing JSON; if both are
  unavailable, a built-in fallback table is used

## Data sources

### Primary: `<claude-data>/projects/<project-name>/<session-id>.jsonl`

Default: `~/.claude/projects/<project-name>/<session-id>.jsonl` on macOS/Linux,
`%LOCALAPPDATA%\\Claude\\projects\\...` on Windows.

ClaudeCode creates one JSONL file per project per session. `assistant`-type
records contain the inference result and token usage:

```json
{
  "type": "assistant",
  "timestamp": "2026-05-02T19:12:26.067Z",
  "cwd": "/home/user/project/opencode-cursor-plugin",
  "sessionId": "1d5edb59-e626-4cb0-b7c7-8506fbe48624",
  "requestId": "req_011CaeGoapfoopFq5gARLr7Q",
  "message": {
    "model": "claude-haiku-4-5-20251001",
    "usage": {
      "input_tokens": 10,
      "cache_creation_input_tokens": 36963,
      "cache_read_input_tokens": 0,
      "output_tokens": 621
    }
  }
}
```

### Prompts and response times: extracted from project JSONL files

Prompt/response bodies and response times are derived entirely from the project
JSONL files (`~/.claude/projects/*/*.jsonl`). Each `assistant` record is one
billing row:

- `response_text` is the concatenation of `type == "text"` blocks from the
  record's `message.content` (thinking and tool_use blocks are excluded).
- `prompt_text` is the nearest human `user` utterance found by walking the
  `parentUuid` chain backward (tool_result-only user messages are skipped).
- `response_time_ms` is the timestamp delta between the input record and the
  assistant record, computed regardless of the `store_prompts` privacy setting.

These fields are only populated when `privacy.store_prompts` is `true`; when
`false`, `prompt_text` and `response_text` are `NULL`, but `response_time_ms`
remains available.

### Auxiliary: `<claude-data>/history.jsonl`

Default: `~/.claude/history.jsonl` on macOS/Linux,
`%LOCALAPPDATA%\\Claude\\history.jsonl` on Windows.

UI display text and project path hints. Used as an optional hint for project
display names; missing or malformed history data never blocks collection.

## Collection method

- The collector scans the configured projects directory
  (`~/.claude/projects/*/*.jsonl` on macOS/Linux,
  `%LOCALAPPDATA%\Claude\projects\...` on Windows).
- File changes are detected via `watchdog` (with polling fallback).
- A `sync_state` table tracks the last parsed position per file for
  **incremental parsing**.
- Only new or changed lines are written to SQLite.
- File shrinkage (rotation/truncation) resets the parse position to zero.
- JSONL lines with `UnicodeDecodeError` are handled with `errors="replace"` for
  partial-fault tolerance.
- Batch-boundary context (the most recent human-utterance prompt text and its
  timestamp) is carried over in `sync_state` so that an `assistant` record
  whose parent `user` record was ingested in an earlier batch can still resolve
  its prompt text.

### Response time calculation

`response_time_ms` is calculated from the timestamp delta between the input
record (the most recent `user` record) and the `assistant` record. This
calculation is performed regardless of the `store_prompts` privacy setting,
since it depends only on timestamps, not on message content.

### Synthetic request IDs

Records with a `uuid` field use that as the `request_id`. Records without a
`uuid` are assigned a deterministic synthetic ID (`missing-{file_name}-{line_no}`)
to satisfy the `(session_id, request_id)` uniqueness constraint.

## SQLite schema

Database path: `~/.claude-meter/data.db`

SQLite is opened with `PRAGMA journal_mode = WAL` and
`PRAGMA busy_timeout = 5000` for concurrent CLI/UI/watcher access.

### `requests`

```sql
CREATE TABLE requests (
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
```

Indexes on `timestamp`, `project`, `model`, and `session_id`.

### `pricing`

```sql
CREATE TABLE pricing (
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
```

The `region` used for cost calculation comes from `config.yaml`
(`claude.region`, default `us-east-1`) since ClaudeCode JSONL does not include
region information.

### `sync_state`

```sql
CREATE TABLE sync_state (
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
```

### `daily_summary`

```sql
CREATE TABLE daily_summary (
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
```

`project` is `NOT NULL` in this table. Aggregation pipelines must normalize
`NULL` project values (e.g. `COALESCE(project, '')`) before insert.

## Pricing

### Source priority

1. **`models.dev` API** (`https://models.dev/api.json`) — primary source,
   returns ARN-style Bedrock model IDs with per-1M-token prices (converted to
   per-1k internally). IAM permissions not required.
2. **AWS Bedrock pricing JSON**
   (`https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/index.json`)
   — secondary source, accepted only when it yields ARN-style model keys.
   Human-readable model names (e.g. "Claude 2.1") are rejected.
3. **Built-in fallback JSON** (`pricing_fallback.json` bundled with the
   package) — used when no external source is available.

Source order is configurable via `pricing.primary_source` and
`pricing.fallback_source` (validated as `Literal["models_dev",
"aws_bedrock_json"]`).

### Cache files

| File | Purpose |
| --- | --- |
| `~/.claude-meter/pricing.json` | Cached pricing records |
| `~/.claude-meter/pricing-meta.yaml` | Cache timestamp for TTL management |
| `~/.claude-meter/pricing-overrides.json` | User-edited price overrides |

- TTL is 24 hours by default, configurable via `pricing.cache_ttl_hours`
- Cache files are written atomically (tempfile + `os.replace`) for concurrent
  safety
- When all external sources fail, stale cache (if ARN-style) is used before
  falling back to the built-in table

### Price overrides

Users can edit fallback prices via the Pricing Settings UI page. Overrides are
saved to `pricing-overrides.json` and merged with the built-in/fetched pricing
on every load.

### Cost calculation

```text
input_cost  = input_tokens × input_price_per_1k / 1000
output_cost = output_tokens × output_price_per_1k / 1000
cache_cost  = cache_creation_input_tokens × cache_creation_price_per_1k / 1000
            + cache_read_input_tokens × cache_read_price_per_1k / 1000
total_cost  = input_cost + output_cost + cache_cost
```

When a token component has non-zero tokens but the corresponding price is
`None`, `cost_usd` is set to `NULL` (rather than 0) to avoid underestimation.

### Model name normalization

ClaudeCode internal names (e.g. `claude-haiku-4-5-20251001`) and Bedrock
ARN-style IDs (e.g. `anthropic.claude-3-5-sonnet-20241022-v2:0`) are both
supported via a normalization layer:

- **`normalize_model_name()`** — returns a canonical key for recognized models,
  `None` for unknown
- **`model_to_arn_keys()`** — maps a normalized name to Bedrock ARN-style
  price keys
- **`canonical_model_key()`** — strips inference-profile region prefixes
  (e.g. `eu.`, `us.`, `global.`), the `anthropic.` provider prefix, and
  trailing version suffixes (`-v1:0`) to produce a comparable core key

A **canonical pricing index** (`build_canonical_pricing_index()`) precomputes
`(canonical_key, region) → PricingRecord` so that a region-prefixed model ID
from models.dev can match a bare ARN key in O(1).

Models that cannot be normalized result in `cost_usd = NULL` and display as
"Unknown model" in the UI.

## Streamlit UI

Launched via `claude-meter ui` at `http://127.0.0.1:8501`.

| Page | Path | Purpose |
| --- | --- | --- |
| Overview | `/` | Summary stats, daily cost trend, project/model breakdowns |
| Project Breakdown | `/project-breakdown` | Per-project cost and tokens |
| Model Breakdown | `/model-breakdown` | Per-model usage distribution |
| Session Explorer | `/session-explorer` | Session list, request details |
| Pricing Settings | `/pricing-settings` | Source status, refresh, overrides |
| Config | `/config` | Configuration view and editing |

### Overview page

- Total cost, input tokens, output tokens (metrics)
- Period selector (Today / Last 7 days / Last 30 days / Custom date range)
- Daily cost trend (line chart)
- Cost by project (bar chart)
- Token distribution by model (pie chart)
- Average response time trend (line chart)
- Top 10 costly prompts (table; prompt text shown only when
  `privacy.show_prompts_in_ui` is true)

### Session Explorer page

- Session list table (request count, total cost, first/last seen)
- Select a session to view per-request details
- Full-text search across prompt/response text (when enabled)

### Privacy controls

- `privacy.store_prompts` — when `false`, prompt/response text is not stored;
  tokens, cost, response time, and request metadata are still retained.
- `privacy.show_prompts_in_ui` — toggles visibility in the UI independently of
  storage

## CLI

Tool name: `claude-meter` (alias `cm`)

| Command | Description |
| --- | --- |
| `claude-meter init` | Create config file and SQLite database |
| `claude-meter collect` | Parse JSONL logs once and backfill costs |
| `claude-meter collect --reparse` | Re-ingest all JSONL files from start |
| `claude-meter watch [--poll N]` | Watch configured data dir for new data |
| `claude-meter ui [--port N] [--host H]` | Launch the Streamlit UI |
| `claude-meter pricing update [--force]` | Refresh Bedrock pricing cache |
| `claude-meter config` | Show the config file path |

## Configuration

`~/.claude-meter/config.yaml`:

```yaml
claude:
  projects_dir: null        # default: OS-specific (see below)
  transcripts_dir: null     # default: OS-specific (see below)
  region: "us-east-1"       # region used for cost calculation

storage:
  db_path: "~/.claude-meter/data.db"

pricing:
  primary_source: "models_dev"    # Literal["models_dev", "aws_bedrock_json"]
  fallback_source: "aws_bedrock_json"
  cache_ttl_hours: 24

privacy:
  store_prompts: true
  max_prompt_length: 10000
  max_response_length: 10000
  show_prompts_in_ui: true

ui:
  port: 8501
  host: "127.0.0.1"
```

## Multi-OS support

| OS | Default Claude data path |
| --- | --- |
| macOS | `~/.claude` |
| Linux | `~/.claude` |
| Windows | `%LOCALAPPDATA%\Claude` |

Paths can be overridden via `claude.projects_dir` and `claude.transcripts_dir`
in `config.yaml`. All path handling uses `pathlib`.

## Technology stack

- Python 3.10+
- SQLite (standard library)
- Streamlit (Web UI)
- watchdog (filesystem watching)
- requests (pricing fetch)
- pydantic / pydantic-settings (config and data validation)
- Altair (charts)
- pandas (data manipulation)
- Click (CLI)
- pytest / ruff / mypy (development)

## Local-first guarantee

- All data is stored under `~/.claude-meter/`
- The only external network calls are pricing fetches
- Prompt/response text stays in the local SQLite database
- Setting `store_prompts: false` records tokens and cost only

## Non-goals (out of scope)

- AWS CloudTrail / Bedrock log analysis
- IAM-requiring Cost Explorer integration
- Cloud-based data sharing or aggregation
- Real-time AWS-side cost monitoring (Cost Explorer is next-day)

## Future extensibility

- InfluxDB + Grafana data integration (collector is swappable)
- Slack / Teams notifications (local webhook)
- Team-wide aggregation (shared SQLite or S3)
- Non-Bedrock provider support
