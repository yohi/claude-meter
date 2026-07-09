from pathlib import Path

from claude_meter.config import default_claude_dir, load_config, resolve_config_path


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
