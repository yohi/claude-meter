"""Configuration management and OS-specific path resolution."""

import os
import zoneinfo
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class _ClaudeConfig(BaseModel):
    projects_dir: Path | None = Field(default=None)
    transcripts_dir: Path | None = Field(default=None)
    region: str = Field(default="us-east-1")


class _StorageConfig(BaseModel):
    db_path: Path = Field(default_factory=lambda: _dot_claude_meter() / "data.db")


class _PricingConfig(BaseModel):
    primary_source: Literal["models_dev", "aws_bedrock_json"] = Field(default="models_dev")
    fallback_source: Literal["models_dev", "aws_bedrock_json"] = Field(default="aws_bedrock_json")
    cache_ttl_hours: int = Field(default=24)


class _PrivacyConfig(BaseModel):
    store_prompts: bool = Field(default=True)
    max_prompt_length: int = Field(default=10000)
    max_response_length: int = Field(default=10000)
    show_prompts_in_ui: bool = Field(default=True)


class _UiConfig(BaseModel):
    port: int = Field(default=8501)
    host: str = Field(default="127.0.0.1")
    timezone: str | None = Field(default=None)


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_METER_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    claude: _ClaudeConfig = Field(default_factory=_ClaudeConfig)
    storage: _StorageConfig = Field(default_factory=_StorageConfig)
    pricing: _PricingConfig = Field(default_factory=_PricingConfig)
    privacy: _PrivacyConfig = Field(default_factory=_PrivacyConfig)
    ui: _UiConfig = Field(default_factory=_UiConfig)


def _dot_claude_meter() -> Path:
    return Path.home() / ".claude-meter"


def resolve_config_path() -> Path:
    return _dot_claude_meter() / "config.yaml"


def default_claude_dir() -> Path:
    r"""Return the OS-specific Claude data directory.

    Windows: %LOCALAPPDATA%\Claude
    macOS/Linux: ~/.claude
    """
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "Claude"
    return Path.home() / ".claude"


def detect_system_timezone() -> str | None:
    """Return the system's local IANA timezone name, or ``None`` if unknown.

    Uses only the standard library, checking in order:

    1. The ``TZ`` environment variable, when set.
    2. On POSIX systems, the symlink target of ``/etc/localtime``: the path
       segment after ``zoneinfo/`` (e.g. ``/usr/share/zoneinfo/Asia/Tokyo``
       yields ``Asia/Tokyo``).

    Returns ``None`` when no IANA name can be determined; callers then fall
    back to a fixed-offset local time via :func:`resolve_tzinfo`.
    """
    tz_env = os.environ.get("TZ")
    if tz_env:
        return tz_env
    if os.name != "nt":
        localtime = Path("/etc/localtime")
        if localtime.is_symlink():
            _, _, iana_name = localtime.readlink().as_posix().partition("zoneinfo/")
            if iana_name:
                return iana_name
    return None


def resolve_tzinfo(name: str | None) -> tzinfo:
    """Resolve a timezone *name* to a concrete :class:`~datetime.tzinfo`.

    ``name`` takes precedence; when it is ``None`` the auto-detected system
    timezone (:func:`detect_system_timezone`) is used. Resolution is attempted
    via :class:`zoneinfo.ZoneInfo`; on failure (unknown name or invalid value)
    it falls back to the process's local timezone (a fixed offset) and finally
    to UTC.
    """
    candidate = name or detect_system_timezone()
    if candidate is not None:
        try:
            return zoneinfo.ZoneInfo(candidate)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            pass
    local_tzinfo = datetime.now().astimezone().tzinfo
    return local_tzinfo if local_tzinfo is not None else timezone.utc


def load_config(path: Path | None = None) -> Config:
    config_path = path or resolve_config_path()
    if config_path.exists():
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in config file {config_path}: {e}") from e
        return Config.model_validate(raw or {})
    return Config()


def save_config(config: Config, path: Path | None = None) -> None:
    config_path = path or resolve_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
