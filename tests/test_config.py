import zoneinfo
from datetime import tzinfo
from pathlib import Path

import pytest

from claude_meter.config import (
    default_claude_dir,
    detect_system_timezone,
    load_config,
    resolve_config_path,
    resolve_tzinfo,
)


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


def test_detect_system_timezone_prefers_tz_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert detect_system_timezone() == "Asia/Tokyo"


def test_resolve_tzinfo_uses_explicit_name() -> None:
    assert resolve_tzinfo("Asia/Tokyo") == zoneinfo.ZoneInfo("Asia/Tokyo")


def test_resolve_tzinfo_none_falls_back_to_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    assert resolve_tzinfo(None) == zoneinfo.ZoneInfo("America/New_York")


def test_resolve_tzinfo_invalid_name_returns_tzinfo() -> None:
    resolved = resolve_tzinfo("Definitely/NotAZone")
    assert isinstance(resolved, tzinfo)
