from pathlib import Path

import pytest

from claude_meter.config import Config
from claude_meter.db import init_db
from claude_meter.watcher import _collect_once


def test_collect_once_idempotent(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the test fully offline and isolated:
    # - temp_home isolates HOME so default_claude_dir() (used by history/transcript
    #   lookups) points at an empty temp dir, never the real ~/.claude.
    # - _collect_once() calls fill_missing_costs(), which would otherwise reach
    #   update_pricing() and attempt network fetches. Patch the name bound INSIDE
    #   the watcher module (it does `from claude_meter.cost import fill_missing_costs`).
    monkeypatch.setattr(
        "claude_meter.watcher.fill_missing_costs", lambda config, region=None: 0
    )

    config = Config(
        claude={
            "projects_dir": temp_home / "projects",
            "transcripts_dir": temp_home / "transcripts",
        },
        storage={"db_path": temp_home / "data.db"},
    )
    init_db(config.storage.db_path)

    jsonl = temp_home / "projects" / "p" / "sess-1.jsonl"
    jsonl.parent.mkdir(parents=True)
    jsonl.write_text(
        '{"type":"assistant","timestamp":"2026-07-08T10:00:00Z","cwd":"/x",'
        '"sessionId":"sess-1","message":{"model":"m","usage":{}}}\n',
        encoding="utf-8",
    )

    assert _collect_once(config) == 1
    # Second run must be idempotent (no new rows).
    assert _collect_once(config) == 0
