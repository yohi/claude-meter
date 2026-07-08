"""Integration smoke tests for claude-meter."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_meter.cli import main


def test_full_flow(
    temp_home: Path, sample_project_jsonl: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test full flow: init -> collect -> cost backfill (with pricing mocked)."""
    # Monkeypatch pricing update to avoid network calls
    monkeypatch.setattr("claude_meter.cost.update_pricing", lambda config, force=False: [])

    runner = CliRunner()

    # Step 1: init
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0

    # Step 2: collect (should insert 1 record from sample_project_jsonl)
    result = runner.invoke(main, ["collect"])
    assert result.exit_code == 0
    assert "Inserted 1 new records." in result.output

    # Step 3: verify database exists
    db_path = temp_home / ".claude-meter" / "data.db"
    assert db_path.exists()


def test_ui_entrypoint_importable() -> None:
    """Test that UI module and app entrypoint are importable."""
    from claude_meter.ui import app

    assert hasattr(app, "main")
