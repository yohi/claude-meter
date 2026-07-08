import json
from pathlib import Path

from claude_meter.collector import collect_files, derive_project, parse_incremental
from claude_meter.config import load_config
from claude_meter.db import init_db


def test_collect_files_finds_jsonl(temp_home: Path, sample_project_jsonl: Path) -> None:
    config = load_config()
    files = collect_files(config)
    assert sample_project_jsonl in files


def test_parse_incremental_inserts_record(temp_home: Path, sample_project_jsonl: Path) -> None:
    config = load_config()
    init_db(config.storage.db_path)
    inserted = parse_incremental(config)
    assert inserted == 1
    # second run is idempotent
    assert parse_incremental(config) == 0


def test_derive_project_from_git_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    git_dir = project_dir / ".git"
    git_dir.mkdir()
    config_file = git_dir / "config"
    config_file.write_text("""[remote "origin"]
        url = git@github.com:example/my-project.git
    """, encoding="utf-8")
    project, repo = derive_project(str(project_dir))
    assert project == "my-project"
    assert repo == "example/my-project"


def test_response_time_computed_from_transcript(temp_home: Path, sample_project_jsonl: Path, sample_transcript_jsonl: Path) -> None:
    from claude_meter.collector import _load_transcripts, _compute_response_time, _parse_iso_ts
    config = load_config()
    transcripts = _load_transcripts(config)
    key = ("sess-001", "req-001")
    assert key in transcripts
    prompt_text, response_text, _ = transcripts[key]
    assert prompt_text == "hello"
    assert response_text == "world"
    duration = _compute_response_time("sess-001", "req-001", _parse_iso_ts("2026-07-08T10:00:00.000Z"), transcripts)
    assert duration == 2000


def test_collect_survives_list_content_and_missing_timestamp(temp_home: Path) -> None:
    from contextlib import closing
    from claude_meter.db import get_connection

    config = load_config()
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    rec_a = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:00Z",
        "cwd": "/x",
        "sessionId": "s1",
        "requestId": "req-A",
        "message": {"model": "claude-sonnet-4-5-20260701", "usage": {"input_tokens": 10}},
    }
    rec_b = {
        "type": "assistant",
        "cwd": "/x",
        "sessionId": "s1",
        "requestId": "req-B",
        "message": {"model": "m", "usage": {}},
    }  # missing timestamp -> must be skipped, not crash
    (projects / "s1.jsonl").write_text(
        json.dumps(rec_a) + "\n" + json.dumps(rec_b) + "\n", encoding="utf-8"
    )
    transcripts = temp_home / ".claude" / "transcripts"
    transcripts.mkdir(parents=True)
    user_rec = {
        "type": "user",
        "timestamp": "2026-07-08T09:59:59Z",
        "sessionId": "s1",
        "requestId": "req-A",
        "message": {
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
            ]
        },
    }
    asst_rec = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:00Z",
        "sessionId": "s1",
        "requestId": "req-A",
        "message": {"content": [{"type": "text", "text": "hi there"}]},
    }
    (transcripts / "s1.jsonl").write_text(
        json.dumps(user_rec) + "\n" + json.dumps(asst_rec) + "\n", encoding="utf-8"
    )

    inserted = parse_incremental(config)  # must NOT raise
    assert inserted == 1  # rec_a inserted; rec_b (no timestamp) skipped
    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT prompt_text, response_text FROM requests WHERE request_id = 'req-A'"
        ).fetchone()
        assert row["prompt_text"] == "hello world"
        assert row["response_text"] == "hi there"


def test_response_time_computed_when_request_id_missing(temp_home: Path) -> None:
    """requestId が欠損しているレコードでもトランスクリプト照合が成功すること。"""
    from contextlib import closing
    from claude_meter.db import get_connection

    config = load_config()
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    rec_a = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:00Z",
        "cwd": "/x",
        "sessionId": "s1",
        # requestId is intentionally absent
        "message": {"model": "m", "usage": {"input_tokens": 5}},
    }
    (projects / "s1.jsonl").write_text(json.dumps(rec_a) + "\n", encoding="utf-8")

    transcripts = temp_home / ".claude" / "transcripts"
    transcripts.mkdir(parents=True)
    user_rec = {
        "type": "user",
        "timestamp": "2026-07-08T09:59:59Z",
        "sessionId": "s1",
        # requestId is also absent in the transcript source
        "message": {"content": "hello"},
    }
    asst_rec = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:00Z",
        "sessionId": "s1",
        "message": {"content": "world"},
    }
    (transcripts / "s1.jsonl").write_text(
        json.dumps(user_rec) + "\n" + json.dumps(asst_rec) + "\n", encoding="utf-8"
    )

    inserted = parse_incremental(config)
    assert inserted == 1
    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT prompt_text, response_text, response_time_ms FROM requests WHERE session_id = 's1'"
        ).fetchone()
        assert row["prompt_text"] == "hello"
        assert row["response_text"] == "world"
        assert row["response_time_ms"] == 1000
