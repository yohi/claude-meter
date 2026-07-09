# AGENTS.md

Local-only Python CLI and Streamlit dashboard that parses ClaudeCode JSONL usage
logs, estimates AWS Bedrock cost from cached pricing, and stores everything in
SQLite.

## Project essentials

- **Package manager / runtime**: Python 3.10+, `uv` or `pip`, virtualenv at
  `.venv`
- **Source layout**: `src/claude_meter/` package, tests in `tests/`
- **Entry points**: `claude-meter` / `cm` console scripts
  (`src/claude_meter/cli.py:main`)
- **UI**: Streamlit multi-page app under `src/claude_meter/ui/`, launched via
  `claude-meter ui`

## How to verify changes

Always run the quality gates after code changes:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
markdownlint-cli2 README.md
```

## Conventions

- Use `pathlib` for all filesystem paths; no hard-coded absolute paths in
  committed code.
- Keep all user data under `~/.claude-meter/`; only pricing data is fetched
  externally.
- Follow existing type-annotated Python style; `mypy --strict` must pass.
- Prefer small, focused changes. OpenCode skills exist under `.agents/skills/`;
  use them when the task matches.

## Progressive disclosure

- For detailed usage, architecture, and cost-calculation logic: see `README.md`.
- For implementation plans and historical design decisions: inspect
  `docs/superpowers/` in Git history (the directory has been removed; design
  docs now live only in history).
