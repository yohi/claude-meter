# claude-meter

Local-only analyzer for ClaudeCode usage and estimated AWS Bedrock cost.

## Quick start

With `uv` (one command — initializes on first run, then launches the UI with
background log watching):

```bash
uv run claude-meter start   # or: uv run cm start
```

With `uvx` (running directly from the built package or repository on Bitbucket):

<!-- markdownlint-disable MD013 -->
```bash
# Run using the published tar.gz package.
# (Replace 0.1.0 with your target version, and specify your workspace and repository)
uvx --from https://bitbucket.org/<BITBUCKET_WORKSPACE_NAME>/<BITBUCKET_REPOSITORY_NAME>/raw/master/packages/claude-meter/claude-meter-0.1.0.tar.gz claude-meter start

# Or run directly from the Git repository (Replace <BITBUCKET_WORKSPACE_NAME> with your workspace)
uvx --from git+https://bitbucket.org/<BITBUCKET_WORKSPACE_NAME>/claude-meter.git claude-meter start
```
<!-- markdownlint-enable MD013 -->

With `pip`:

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
bodies. Usage metrics (tokens, cost) and request metadata (project, session,
model, timestamp, etc.) are still retained. Response time (`response_time_ms`) is
always calculated from timestamps, regardless of the `store_prompts` setting.

Project names are derived from the `cwd` in each JSONL record (`.git/config` or
directory name). The default Claude data directory's `history.jsonl`
(e.g. `~/.claude/history.jsonl` on macOS/Linux,
`%LOCALAPPDATA%\Claude\history.jsonl` on Windows) is used as an optional hint
for project display names; missing history data never blocks collection.

Records without a `uuid` are assigned a deterministic synthetic ID so the
`(session_id, request_id)` uniqueness constraint remains valid.

## What it does

- Parses ClaudeCode JSONL logs from the configured projects directory
  (default `~/.claude/projects/*/*.jsonl` on macOS/Linux,
  `%LOCALAPPDATA%\Claude\projects\...` on Windows) incrementally.
- Extracts prompt/response bodies and response times directly from the same
  JSONL files: `response_text` is the concatenation of `type == "text"` blocks
  from each `assistant` record's `message.content`; `prompt_text` is the nearest
  human `user` utterance found by walking the `parentUuid` chain; `response_time_ms`
  is the timestamp delta between the input and assistant records.
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
| `claude-meter collect --reparse` | Re-ingest all JSONL files from start |
| `claude-meter watch` | Watch configured data dir (`watchdog` or polling) |
| `claude-meter ui` | Launch the Streamlit UI |
| `claude-meter ui --watch [--poll N]` | Watch logs while UI runs |
| `claude-meter start` | First-run init, then launch the UI with log watching |
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

## Bitbucket deployment setup

To allow GitHub Actions to build and deploy packages to the Bitbucket
repository automatically, set up the following authentication settings:

### 1. Issue an App password in Bitbucket

1. Log in to Bitbucket, click on your profile icon in the top right, and
   select **Personal settings**.
2. Select **App passwords** under **Access management** on the left menu.
3. Click **Create app password**.
4. Enter the following details:
   - **Label**: `GitHub Actions Deploy` (or any descriptive label)
   - **Permissions**:
     - **Repositories**: **Write** (and Read)
5. Click **Create** and copy the generated password.

### 2. Register the token in GitHub Secrets

1. Open the `claude-meter` repository on GitHub.
2. Go to **Settings** -> **Secrets and variables** -> **Actions** in the left menu.
3. Click **New repository secret**.
4. Add the secret with:
   - **Name**: `BITBUCKET_API_TOKEN`
   - **Secret**: The copied **Bitbucket App password**
5. Click **Add secret** to save.

### 3. Register Repository Variables in GitHub

1. In the same **Settings** -> **Secrets and variables** -> **Actions** page,
   click the **Variables** tab (next to the Secrets tab).
2. Click **New repository variable**.
3. Add the following variables:
   - **BITBUCKET_WORKSPACE_NAME**: e.g. `dh_ohi` (Your Bitbucket Workspace name)
   - **BITBUCKET_REPOSITORY_NAME**: e.g. `claude-plugins`
     (Your Bitbucket target repository name)
