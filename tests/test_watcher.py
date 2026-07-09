from pathlib import Path

import pytest

from claude_meter.config import Config
from claude_meter.db import init_db
from claude_meter.watcher import _collect_once, watch


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


def test_collect_once_reprices_reprocessed_existing_record(temp_home: Path) -> None:
    import json
    from contextlib import closing

    from claude_meter.db import get_connection
    from claude_meter.pricing import _save_cached_pricing, load_fallback_pricing

    config = Config(
        claude={
            "projects_dir": temp_home / "projects",
            "transcripts_dir": temp_home / "transcripts",
        },
        storage={"db_path": temp_home / "data.db"},
    )
    init_db(config.storage.db_path)
    _save_cached_pricing(config, load_fallback_pricing())

    jsonl = temp_home / "projects" / "p" / "sess-1.jsonl"
    jsonl.parent.mkdir(parents=True)
    jsonl.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-07-08T10:00:00Z",
                "cwd": "/x",
                "sessionId": "sess-1",
                "requestId": "req-1",
                "message": {
                    "model": "claude-sonnet-4-5-20260701",
                    "usage": {"input_tokens": 1000},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert _collect_once(config) == 1
    with closing(get_connection(config.storage.db_path)) as conn:
        conn.execute("DELETE FROM sync_state")
        conn.commit()

    assert _collect_once(config) == 0
    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute("SELECT cost_usd, region FROM requests").fetchone()
    assert row is not None
    assert row["cost_usd"] is not None
    assert row["region"] == "us-east-1"


def test_watch_falls_back_to_polling_on_observer_failure(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When the watchdog Observer fails to start (inotify limit, permissions, etc.),
    # watch() must degrade to the polling loop instead of crashing outright.
    config = Config(
        claude={
            "projects_dir": temp_home / "projects",
            "transcripts_dir": temp_home / "transcripts",
        },
        storage={"db_path": temp_home / "data.db"},
    )
    init_db(config.storage.db_path)

    class _BoomObserver:
        def schedule(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("inotify watch limit reached")

        def start(self) -> None:  # pragma: no cover - not reached after schedule fails
            raise RuntimeError("start should not be called")

        def stop(self) -> None:
            pass

    # watch() imports Observer lazily via `from watchdog.observers import Observer`,
    # so patch the name in its source module.
    monkeypatch.setattr("watchdog.observers.Observer", _BoomObserver)

    calls: list[int] = []

    def _record(config: Config) -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr("claude_meter.watcher._safe_collect_once", _record)

    class _StopLoop(Exception):
        pass

    def _fake_sleep(_seconds: float) -> None:
        raise _StopLoop

    monkeypatch.setattr("claude_meter.watcher.time.sleep", _fake_sleep)

    with pytest.raises(_StopLoop):
        watch(config, poll_interval=0.01)

    # The polling loop calls _safe_collect_once once before the (patched) sleep.
    assert calls == [1]
