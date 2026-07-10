"""Tests for CLI commands."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_meter.cli import main
from claude_meter.models import PricingRecord


def test_init_creates_db(temp_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0
    db_path = temp_home / ".claude-meter" / "data.db"
    config_path = temp_home / ".claude-meter" / "config.yaml"
    assert db_path.exists()
    assert config_path.exists()


def test_config_shows_path(temp_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["config"])
    assert result.exit_code == 0
    assert ".claude-meter/config.yaml" in result.output


def test_pricing_update_force_monkeypatched(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_update(config, force=False):
        return [
            PricingRecord(model="m1", region="us-east-1"),
            PricingRecord(model="m2", region="us-east-1"),
        ]

    monkeypatch.setattr("claude_meter.cli.update_pricing", fake_update)
    runner = CliRunner()
    result = runner.invoke(main, ["pricing", "update", "--force"])
    assert result.exit_code == 0
    assert "Updated pricing for 2" in result.output


def test_collect_inserts_records(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_parse_incremental(config, reparse=False):
        return 3

    def fake_fill_missing_costs(config, region=None):
        return 0

    monkeypatch.setattr("claude_meter.cli.parse_incremental", fake_parse_incremental)
    monkeypatch.setattr("claude_meter.cli.fill_missing_costs", fake_fill_missing_costs)
    runner = CliRunner()
    result = runner.invoke(main, ["collect"])
    assert result.exit_code == 0
    assert "Inserted 3 new records." in result.output


def test_collect_reparse_passes_flag(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`collect --reparse` は parse_incremental に reparse=True を渡すこと。"""
    captured: dict[str, bool] = {}

    def fake_parse_incremental(config, reparse=False):
        captured["reparse"] = reparse
        return 5

    def fake_fill_missing_costs(config, region=None):
        return 0

    monkeypatch.setattr("claude_meter.cli.parse_incremental", fake_parse_incremental)
    monkeypatch.setattr("claude_meter.cli.fill_missing_costs", fake_fill_missing_costs)
    runner = CliRunner()
    result = runner.invoke(main, ["collect", "--reparse"])
    assert result.exit_code == 0
    assert captured["reparse"] is True
    assert "Inserted 5 new records." in result.output


def test_ui_initializes_db_before_launch(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`claude-meter ui` を init/collect より先に実行しても、DBスキーマが事前に初期化されていること。"""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, check=True):
        captured["cmd"] = cmd

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("claude_meter.cli.subprocess.run", fake_run)
    runner = CliRunner()
    result = runner.invoke(main, ["ui"])
    assert result.exit_code == 0
    assert "cmd" in captured
    db_path = temp_home / ".claude-meter" / "data.db"
    assert db_path.exists()
    from claude_meter.db import get_connection

    with get_connection(db_path) as conn:
        # must not raise sqlite3.OperationalError: no such table: requests
        conn.execute("SELECT COUNT(*) FROM requests").fetchone()


def test_watch_command_with_poll_interval(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test watch command with custom poll interval."""
    captured: dict[str, tuple] = {}

    def fake_watch(config, poll_interval=5.0):
        captured["watch_call"] = (config, poll_interval)

    monkeypatch.setattr("claude_meter.cli.watch", fake_watch)
    runner = CliRunner()
    result = runner.invoke(main, ["watch", "--poll", "2.5"])
    assert result.exit_code == 0
    assert "Watching ClaudeCode logs for changes (poll=2.5s)..." in result.output
    assert "watch_call" in captured
    _, poll_val = captured["watch_call"]
    assert poll_val == 2.5


def test_ui_with_watch_flag(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test `claude-meter ui --watch` starts watcher thread before streamlit."""
    captured: dict[str, tuple] = {}

    def fake_watch(config, poll_interval=5.0):
        captured["watch_call"] = (config, poll_interval)

    def fake_run(cmd, check=True):
        captured["run_called"] = True

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("claude_meter.cli.watch", fake_watch)
    monkeypatch.setattr("claude_meter.cli.subprocess.run", fake_run)
    runner = CliRunner()
    result = runner.invoke(main, ["ui", "--watch"])
    assert result.exit_code == 0
    assert "Watching ClaudeCode logs in background (poll=5.0s)..." in result.output
    assert "watch_call" in captured
    _, poll_val = captured["watch_call"]
    assert poll_val == 5.0
    assert "run_called" in captured


def test_ui_with_watch_and_custom_poll(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test `claude-meter ui --watch --poll N` passes custom poll interval to watcher."""
    captured: dict[str, tuple] = {}

    def fake_watch(config, poll_interval=5.0):
        captured["watch_call"] = (config, poll_interval)

    def fake_run(cmd, check=True):
        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("claude_meter.cli.watch", fake_watch)
    monkeypatch.setattr("claude_meter.cli.subprocess.run", fake_run)
    runner = CliRunner()
    result = runner.invoke(main, ["ui", "--watch", "--poll", "3.5"])
    assert result.exit_code == 0
    assert "Watching ClaudeCode logs in background (poll=3.5s)..." in result.output
    assert "watch_call" in captured
    _, poll_val = captured["watch_call"]
    assert poll_val == 3.5


def test_ui_without_watch_flag(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test `claude-meter ui` without --watch does not call watch()."""
    captured: dict[str, bool] = {}

    def fake_watch(config, poll_interval=5.0):
        captured["watch_called"] = True

    def fake_run(cmd, check=True):
        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("claude_meter.cli.watch", fake_watch)
    monkeypatch.setattr("claude_meter.cli.subprocess.run", fake_run)
    runner = CliRunner()
    result = runner.invoke(main, ["ui"])
    assert result.exit_code == 0
    assert "watch_called" not in captured


def test_start_first_launch_initializes_and_collects(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start` on first launch saves config, collects logs, and launches Streamlit."""
    captured: dict[str, object] = {}

    def fake_parse_incremental(config, reparse=False):
        captured["parse_called"] = True
        return 7

    def fake_fill_missing_costs(config, region=None):
        captured["fill_called"] = True
        return 0

    def fake_watch(config, poll_interval=5.0):
        captured["watch_call"] = (config, poll_interval)

    def fake_run(cmd, check=True):
        captured["cmd"] = cmd

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("claude_meter.cli.parse_incremental", fake_parse_incremental)
    monkeypatch.setattr("claude_meter.cli.fill_missing_costs", fake_fill_missing_costs)
    monkeypatch.setattr("claude_meter.cli.watch", fake_watch)
    monkeypatch.setattr("claude_meter.cli.subprocess.run", fake_run)

    config_path = temp_home / ".claude-meter" / "config.yaml"
    assert not config_path.exists()

    runner = CliRunner()
    result = runner.invoke(main, ["start"])
    assert result.exit_code == 0
    assert config_path.exists()
    assert captured.get("parse_called") is True
    assert captured.get("fill_called") is True
    assert "watch_call" in captured
    assert "cmd" in captured


def test_start_second_launch_skips_collect(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start` with an existing config skips collection but still launches UI + watch."""
    captured: dict[str, object] = {}

    def fake_parse_incremental(config, reparse=False):
        captured["parse_called"] = True
        return 0

    def fake_fill_missing_costs(config, region=None):
        captured["fill_called"] = True
        return 0

    def fake_watch(config, poll_interval=5.0):
        captured["watch_call"] = (config, poll_interval)

    def fake_run(cmd, check=True):
        captured["cmd"] = cmd

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("claude_meter.cli.parse_incremental", fake_parse_incremental)
    monkeypatch.setattr("claude_meter.cli.fill_missing_costs", fake_fill_missing_costs)
    monkeypatch.setattr("claude_meter.cli.watch", fake_watch)
    monkeypatch.setattr("claude_meter.cli.subprocess.run", fake_run)

    config_dir = temp_home / ".claude-meter"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("ui:\n  port: 8501\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["start"])
    assert result.exit_code == 0
    assert "parse_called" not in captured
    assert "fill_called" not in captured
    assert "watch_call" in captured
    assert "cmd" in captured


def test_start_launches_streamlit_with_new_flags(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start` passes the new Streamlit hardening flags to subprocess.run."""
    captured: dict[str, list[str]] = {}

    def fake_watch(config, poll_interval=5.0):
        pass

    def fake_run(cmd, check=True):
        captured["cmd"] = cmd

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("claude_meter.cli.watch", fake_watch)
    monkeypatch.setattr("claude_meter.cli.subprocess.run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, ["start"])
    assert result.exit_code == 0
    cmd = captured["cmd"]
    assert cmd[cmd.index("--server.showEmailPrompt") + 1] == "false"
    assert cmd[cmd.index("--client.toolbarMode") + 1] == "viewer"


def test_ui_launches_streamlit_with_new_flags(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ui` passes the new Streamlit hardening flags to subprocess.run."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, check=True):
        captured["cmd"] = cmd

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("claude_meter.cli.subprocess.run", fake_run)
    runner = CliRunner()
    result = runner.invoke(main, ["ui"])
    assert result.exit_code == 0
    cmd = captured["cmd"]
    assert cmd[cmd.index("--server.showEmailPrompt") + 1] == "false"
    assert cmd[cmd.index("--client.toolbarMode") + 1] == "viewer"
    assert cmd[cmd.index("--browser.gatherUsageStats") + 1] == "false"
