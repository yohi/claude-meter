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
    assert db_path.exists()


def test_config_shows_path(temp_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["config"])
    assert result.exit_code == 0
    assert ".claude-meter/config.yaml" in result.output


def test_pricing_update_force_monkeypatched(temp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
