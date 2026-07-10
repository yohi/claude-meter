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
    """Create a fake ClaudeCode projects file with one human user + assistant turn.

    Mirrors the real ~/.claude/projects/<project>/<session>.jsonl format:
    records are linked via uuid/parentUuid, prompt text lives in the human
    ``user`` record's ``message.content`` string, and response text lives in
    the ``assistant`` record's ``message.content`` text blocks.
    """
    projects_dir = temp_home / ".claude" / "projects" / "demo"
    projects_dir.mkdir(parents=True)
    session_id = "sess-001"
    user_record = {
        "type": "user",
        "uuid": "u1",
        "parentUuid": None,
        "timestamp": "2026-07-08T09:59:58.000Z",
        "cwd": "/home/user/demo",
        "sessionId": session_id,
        "message": {"role": "user", "content": "hello"},
    }
    assistant_record = {
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": "u1",
        "timestamp": "2026-07-08T10:00:00.000Z",
        "cwd": "/home/user/demo",
        "sessionId": session_id,
        "message": {
            "model": "claude-sonnet-4-5-20260701",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "let me think"},
                {"type": "text", "text": "world"},
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 10,
            },
        },
    }
    path = projects_dir / f"{session_id}.jsonl"
    path.write_text(
        json.dumps(user_record) + "\n" + json.dumps(assistant_record) + "\n",
        encoding="utf-8",
    )
    return path
