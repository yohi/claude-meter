import json
from datetime import datetime
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


def test_missing_request_id_uses_synthetic_id(temp_home: Path) -> None:
    """requestId 欠損時、DB の request_id が missing-{ファイル名}-{行番号} 形式になること。"""
    from contextlib import closing
    from claude_meter.db import get_connection

    config = load_config()
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    rec_with_id = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:00Z",
        "cwd": "/x",
        "sessionId": "s-syn",
        "requestId": "req-present",
        "message": {"model": "m", "usage": {"input_tokens": 1}},
    }
    rec_missing = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:01Z",
        "cwd": "/x",
        "sessionId": "s-syn",
        # requestId is intentionally absent -> synthesized as missing-log.jsonl-2
        "message": {"model": "m", "usage": {"input_tokens": 2}},
    }
    # line 1 = rec_with_id, line 2 = rec_missing (line_no is 1-based)
    (projects / "log.jsonl").write_text(
        json.dumps(rec_with_id) + "\n" + json.dumps(rec_missing) + "\n",
        encoding="utf-8",
    )

    inserted = parse_incremental(config)
    assert inserted == 2
    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT request_id FROM requests WHERE session_id = 's-syn' AND input_tokens = 2"
        ).fetchone()
        assert row["request_id"] == "missing-log.jsonl-2"


def test_parse_incremental_survives_unreadable_file(temp_home: Path) -> None:
    """処理対象の .jsonl が open 失敗しても例外を出さず他ファイルは挿入され続けること。"""
    from contextlib import closing
    from claude_meter.db import get_connection

    config = load_config()
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    good = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:00Z",
        "cwd": "/x",
        "sessionId": "s-good",
        "requestId": "req-good",
        "message": {"model": "m", "usage": {"input_tokens": 7}},
    }
    (projects / "good.jsonl").write_text(json.dumps(good) + "\n", encoding="utf-8")
    # bad.jsonl is created as a directory so open("r") raises OSError (IsADirectoryError).
    # collect_files' rglob('*.jsonl') still lists it; parse_incremental must skip it.
    (projects / "bad.jsonl").mkdir()

    inserted = parse_incremental(config)  # must NOT raise
    assert inserted == 1  # only good.jsonl inserted; bad.jsonl skipped
    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT request_id FROM requests WHERE session_id = 's-good'"
        ).fetchone()
        assert row is not None
        assert row["request_id"] == "req-good"
        # the skipped file must not leave a partial sync_state entry
        bad_state = conn.execute(
            "SELECT 1 FROM sync_state WHERE file_path = ?",
            (str(projects / "bad.jsonl"),),
        ).fetchone()
        assert bad_state is None


def test_collect_survives_null_or_non_string_model(temp_home: Path) -> None:
    """message.model が null / 数値 / message 自体が非dict でも normalize_model_name()
    の AttributeError で例外終了せず、'unknown' として取り込みを継続すること。"""
    from contextlib import closing
    from claude_meter.db import get_connection

    config = load_config()
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    rec_null_model = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:00Z",
        "cwd": "/x",
        "sessionId": "s-null",
        "requestId": "req-null",
        "message": {"model": None, "usage": {"input_tokens": 1}},
    }
    rec_numeric_model = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:01Z",
        "cwd": "/x",
        "sessionId": "s-null",
        "requestId": "req-numeric",
        "message": {"model": 123, "usage": {"input_tokens": 2}},
    }
    rec_null_message = {
        "type": "assistant",
        "timestamp": "2026-07-08T10:00:02Z",
        "cwd": "/x",
        "sessionId": "s-null",
        "requestId": "req-nullmsg",
        "message": None,
    }
    (projects / "s-null.jsonl").write_text(
        json.dumps(rec_null_model) + "\n"
        + json.dumps(rec_numeric_model) + "\n"
        + json.dumps(rec_null_message) + "\n",
        encoding="utf-8",
    )

    inserted = parse_incremental(config)  # must NOT raise
    assert inserted == 3
    with closing(get_connection(config.storage.db_path)) as conn:
        rows = {
            row["request_id"]: row["model"]
            for row in conn.execute(
                "SELECT request_id, model FROM requests WHERE session_id = 's-null'"
            ).fetchall()
        }
        assert rows["req-null"] == "unknown"
        assert rows["req-numeric"] == "unknown"
        assert rows["req-nullmsg"] == "unknown"
        # sync_state must have advanced past all 3 lines, not stalled on the crash.
        state = conn.execute(
            "SELECT last_line FROM sync_state WHERE file_path = ?",
            (str(projects / "s-null.jsonl"),),
        ).fetchone()
        assert state["last_line"] == 3


def test_insert_usage_conflict_updates_stale_attributes(temp_home: Path) -> None:
    """同じ (session_id, request_id) を再取り込みしたとき、token 以外の属性
    (timestamp/project/git_repository/model) も新値に更新され、トークン数の変化によって
    古くなった region/cost_usd は再計算のため NULL にリセットされること。"""
    from contextlib import closing
    from claude_meter.collector import _insert_usage
    from claude_meter.db import get_connection
    from claude_meter.models import UsageRecord

    config = load_config()
    init_db(config.storage.db_path)

    with closing(get_connection(config.storage.db_path)) as conn:
        first = UsageRecord(
            timestamp=datetime.fromisoformat("2026-07-08T10:00:00+00:00"),
            session_id="s-conflict",
            request_id="req-conflict",
            project="old-project",
            git_repository="old/repo",
            model="old-model",
            region="us-east-1",
            input_tokens=10,
            output_tokens=5,
            source_file=Path("old.jsonl"),
        )
        first.cost_usd = 0.001
        _insert_usage(conn, first)
        conn.commit()

        second = UsageRecord(
            timestamp=datetime.fromisoformat("2026-07-08T11:00:00+00:00"),
            session_id="s-conflict",
            request_id="req-conflict",
            project="new-project",
            git_repository="new/repo",
            model="new-model",
            input_tokens=100,
            output_tokens=50,
            source_file=Path("new.jsonl"),
        )
        _insert_usage(conn, second)
        conn.commit()

        row = conn.execute(
            "SELECT timestamp, project, git_repository, model, region, cost_usd, input_tokens "
            "FROM requests WHERE session_id = 's-conflict' AND request_id = 'req-conflict'"
        ).fetchone()
        assert row["project"] == "new-project"
        assert row["git_repository"] == "new/repo"
        assert row["model"] == "new-model"
        assert row["input_tokens"] == 100
        assert row["timestamp"].startswith("2026-07-08T11:00:00")
        # トークン数が変わったので cost_usd/region は再計算が必要
        # -> NULL にリセットされ、fill_missing_costs の次回実行で正しく再計算される。
        assert row["cost_usd"] is None
        assert row["region"] is None
