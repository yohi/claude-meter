# claude-meter

Local-only analyzer for ClaudeCode usage and estimated AWS Bedrock cost.

## Quick start

```bash
pip install -e .
claude-meter init
claude-meter collect
claude-meter ui
```

All data is stored in `~/.claude-meter/`. The only external network call is to
refresh Bedrock pricing; everything else stays on your machine.

## Privacy

By default, `store_prompts: true` records prompt/response text alongside usage
metrics and metadata. Set `store_prompts: false` to skip storing prompt/response
bodies and to stop reading transcripts. Usage metrics (tokens, cost) and request
metadata (project, session, model, timestamp, etc.) are still retained. Because
transcripts are not read, `response_time_ms` is also omitted.

Project names are derived from the `cwd` in each JSONL record (`.git/config` or
directory name). The default Claude data directory's `history.jsonl`
(e.g. `~/.claude/history.jsonl` on macOS/Linux,
`%LOCALAPPDATA%\\Claude\\history.jsonl` on Windows) is used as an optional hint
for project display names; missing history data never blocks collection.

Records without a `requestId` are assigned a deterministic synthetic ID so the
`(session_id, request_id)` uniqueness constraint remains valid.

## What it does

- Parses ClaudeCode JSONL logs from the configured projects directory
  (default `~/.claude/projects/*/*.jsonl` on macOS/Linux,
  `%LOCALAPPDATA%\\Claude\\projects\\...` on Windows) incrementally.
- Pairs prompt/response bodies from the configured transcripts directory
  (default `~/.claude/transcripts/*.jsonl` on macOS/Linux,
  `%LOCALAPPDATA%\\Claude\\transcripts\\*.jsonl` on Windows) when
  `store_prompts: true`, and estimates `response_time_ms` from the
  and estimates `response_time_ms` from the user/assistant timestamp delta for
  the same `requestId`.
- Estimates AWS Bedrock cost using cached per-model, per-region pricing.
- Stores everything locally in `~/.claude-meter/data.db` (`requests`, `pricing`,
  `sync_state`, and `daily_summary` tables).
- Provides a Streamlit dashboard at `http://127.0.0.1:8501` with Overview,
  Project Breakdown, Model Breakdown, Session Explorer, Pricing Settings, and
  Config pages.
- Runs on Windows, macOS, and Ubuntu using `pathlib` for OS-agnostic paths.
- Normalizes ClaudeCode internal model names and Bedrock ARN-style IDs via a
  normalization layer so costs can be matched across region-prefixed variants.

## CLI

| Command | Description |
| --- | --- |
| `claude-meter init` | Create config and SQLite database |
| `claude-meter collect` | Parse JSONL logs once and backfill costs |
| `claude-meter watch` | Watch configured data dir (`watchdog` or polling) |
| `claude-meter ui` | Launch the Streamlit UI |
| `claude-meter pricing update [--force]` | Refresh Bedrock pricing cache |
| `claude-meter config` | Show the config file path |

## Configuration

`~/.claude-meter/config.yaml`:

```yaml
claude:
  projects_dir: null      # default: OS-specific (see below)
  transcripts_dir: null     # default: OS-specific (see below)
  region: "us-east-1"     # region used for cost calculation

storage:
  db_path: "~/.claude-meter/data.db"

pricing:
  primary_source: "models_dev"
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

### OS-specific Claude data directories

| OS | Default path |
| --- | --- |
| macOS / Linux | `~/.claude` |
| Windows | `%LOCALAPPDATA%\Claude` |

For full architecture, data sources, SQLite schema, model normalization,
UI page details, and non-goals, see [SPEC.md](SPEC.md).

## Cost calculation

Cost is calculated per component:

```text
input_cost  = input_tokens × input_price_per_1k / 1000
output_cost = output_tokens × output_price_per_1k / 1000
cache_cost  = cache_creation_input_tokens × cache_creation_price_per_1k / 1000
            + cache_read_input_tokens × cache_read_price_per_1k / 1000
total_cost  = input_cost + output_cost + cache_cost

When a token component is non-zero but the corresponding price is unknown,
`cost_usd` is set to `NULL` (rather than 0) to avoid underestimation.

Unknown models result in `cost_usd = NULL` and are displayed as "Unknown model"
in the UI.

## Pricing sources

1. **`models.dev` API** (`https://models.dev/api.json`) — primary source,
   returns ARN-style Bedrock model IDs.
2. **AWS Bedrock pricing JSON** — secondary source, accepted only when it
   yields ARN-style keys.
3. **Built-in fallback JSON** — used when no external source is available.

Source order is configurable and validated as `Literal["models_dev",
"aws_bedrock_json"]`.

Cache files under `~/.claude-meter/`:

| File | Purpose |
| --- | --- |
| `pricing.json` | Cached pricing records |
| `pricing-meta.yaml` | Cache timestamp for TTL management |
| `pricing-overrides.json` | User-edited price overrides (via UI) |

Cache is written atomically (tempfile + `os.replace`) for concurrent safety.
When all external sources fail, stale cache (if ARN-style) is used before the
built-in fallback. TTL is configurable (default 24 hours).

## Development

```bash
# POSIX / macOS / Linux
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy --strict src

# Windows
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m mypy --strict src
```

## Tech stack

Python 3.10+, SQLite, Streamlit, watchdog, requests, pydantic /
pydantic-settings, Altair, pandas, Click.

## Non-goals

- AWS CloudTrail / Bedrock log analysis
- IAM-requiring Cost Explorer integration
- Cloud-based data sharing or aggregation
