"""Shared pytest fixtures."""

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a temporary home directory and set HOME/USERPROFILE."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        # Windows LOCALAPPDATA is derived from USERPROFILE by default.
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        yield tmp_path


@pytest.fixture
def sample_project_jsonl(temp_home: Path) -> Path:
    """Create a fake ClaudeCode projects directory with one assistant record."""
    projects_dir = temp_home / ".claude" / "projects" / "demo"
    projects_dir.mkdir(parents=True)
    session_id = "sess-001"
    record = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:00.000Z",
        "cwd": "/home/user/demo",
        "sessionId": session_id,
        "requestId": "req-001",
        "message": {
            "model": "claude-sonnet-4-5-20260701",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 10,
            },
        },
    }
    path = projects_dir / f"{session_id}.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def sample_transcript_jsonl(temp_home: Path, sample_project_jsonl: Path) -> Path:
    """Create a matching transcript file for the session."""
    session_id = "sess-001"
    transcripts_dir = temp_home / ".claude" / "transcripts"
    transcripts_dir.mkdir(parents=True)
    user_record = {
        "type": "user",
        "timestamp": "2026-07-08T09:59:58.000Z",
        "sessionId": session_id,
        "requestId": "req-001",
        "message": {"content": "hello"},
    }
    assistant_record = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:00.000Z",
        "sessionId": session_id,
        "requestId": "req-001",
        "message": {"content": "world"},
    }
    path = transcripts_dir / f"{session_id}.jsonl"
    path.write_text(
        json.dumps(user_record) + "\n" + json.dumps(assistant_record) + "\n",
        encoding="utf-8",
    )
    return path
