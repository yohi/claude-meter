import json
from contextlib import closing
from datetime import datetime
from pathlib import Path

from claude_meter.collector import collect_files, derive_project, parse_incremental
from claude_meter.config import load_config
from claude_meter.db import get_connection, init_db


def test_collect_files_finds_jsonl(temp_home: Path, sample_project_jsonl: Path) -> None:
    config = load_config()
    files = collect_files(config)
    assert sample_project_jsonl in files


def test_parse_incremental_inserts_record(temp_home: Path, sample_project_jsonl: Path) -> None:
    config = load_config()
    init_db(config.storage.db_path)
    inserted = parse_incremental(config)
    # Only the assistant record becomes a billing row; the user record does not.
    assert inserted == 1
    # second run is idempotent
    assert parse_incremental(config) == 0


def test_parse_incremental_stores_configured_region(
    temp_home: Path, sample_project_jsonl: Path
) -> None:
    config = load_config()
    config.claude.region = "eu-west-1"
    init_db(config.storage.db_path)

    parse_incremental(config)

    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute("SELECT region FROM requests").fetchone()
    assert row is not None
    assert row["region"] == "eu-west-1"


def test_derive_project_from_git_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    git_dir = project_dir / ".git"
    git_dir.mkdir()
    config_file = git_dir / "config"
    config_file.write_text(
        """[remote "origin"]
        url = git@github.com:example/my-project.git
    """,
        encoding="utf-8",
    )
    project, repo = derive_project(str(project_dir))
    assert project == "my-project"
    assert repo == "example/my-project"


def test_prompt_and_response_extracted_from_pair(
    temp_home: Path, sample_project_jsonl: Path
) -> None:
    """user->assistant ペアから prompt_text/response_text/response_time_ms が抽出されること。

    prompt_text は人間発話の user レコードの content 文字列、response_text は
    assistant レコード自身の content のうち type=="text" ブロックのみ(thinking は除外)。
    """
    config = load_config()
    init_db(config.storage.db_path)
    parse_incremental(config)
    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT request_id, prompt_text, response_text, response_time_ms "
            "FROM requests WHERE session_id = 'sess-001'"
        ).fetchone()
    assert row is not None
    # request_id defaults to the assistant record's uuid.
    assert row["request_id"] == "a1"
    assert row["prompt_text"] == "hello"
    # thinking block excluded; only the text block survives.
    assert row["response_text"] == "world"
    # 10:00:00 - 09:59:58 == 2 seconds.
    assert row["response_time_ms"] == 2000


def test_compute_response_time_pure_function() -> None:
    from claude_meter.collector import _compute_response_time, _parse_iso_ts

    duration = _compute_response_time(
        _parse_iso_ts("2026-07-08T09:59:58Z"), _parse_iso_ts("2026-07-08T10:00:00Z")
    )
    assert duration == 2000
    # Non-monotonic timestamps must clamp to 0, never go negative.
    clamped = _compute_response_time(
        _parse_iso_ts("2026-07-08T10:00:05Z"), _parse_iso_ts("2026-07-08T10:00:00Z")
    )
    assert clamped == 0


def test_response_text_extracts_only_text_blocks_and_missing_timestamp_skipped(
    temp_home: Path,
) -> None:
    """assistant.content がブロック配列でも text ブロックのみ連結され、timestamp 欠損行は
    例外にせずスキップされること。"""
    config = load_config()
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    user_rec = {
        "type": "user",
        "uuid": "u1",
        "parentUuid": None,
        "timestamp": "2026-07-08T09:59:59Z",
        "cwd": "/x",
        "sessionId": "s1",
        "message": {"role": "user", "content": "ask something"},
    }
    asst_ok = {
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": "u1",
        "timestamp": "2026-07-08T10:00:00Z",
        "cwd": "/x",
        "sessionId": "s1",
        "message": {
            "model": "claude-sonnet-4-5-20260701",
            "content": [
                {"type": "text", "text": "hi "},
                {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
                {"type": "text", "text": "there"},
            ],
            "usage": {"input_tokens": 10},
        },
    }
    asst_no_ts = {
        "type": "assistant",
        "uuid": "a2",
        "parentUuid": "a1",
        "cwd": "/x",
        "sessionId": "s1",
        "message": {"model": "m", "usage": {}},
    }  # missing timestamp -> must be skipped, not crash
    (projects / "s1.jsonl").write_text(
        json.dumps(user_rec) + "\n" + json.dumps(asst_ok) + "\n" + json.dumps(asst_no_ts) + "\n",
        encoding="utf-8",
    )

    inserted = parse_incremental(config)  # must NOT raise
    assert inserted == 1  # asst_ok inserted; asst_no_ts (no timestamp) skipped
    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT prompt_text, response_text FROM requests WHERE request_id = 'a1'"
        ).fetchone()
        assert row["prompt_text"] == "ask something"
        assert row["response_text"] == "hi there"


def test_long_parent_chain_resolves_human_prompt(temp_home: Path) -> None:
    """tool_use/tool_result を挟んだ 20 ホップ超の親鎖でも、parentUuid を遡って
    人間発話まで到達し prompt_text を解決できること(20 のような小さいホップ上限では失敗する)。"""
    config = load_config()
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)

    records: list[dict[str, object]] = [
        {
            "type": "user",
            "uuid": "u0",
            "parentUuid": None,
            "timestamp": "2026-07-08T10:00:00Z",
            "cwd": "/x",
            "sessionId": "s-chain",
            "message": {"role": "user", "content": "the real question"},
        }
    ]
    prev = "u0"
    n_iterations = 25
    for i in range(1, n_iterations + 1):
        a_uuid = f"a{i}"
        records.append(
            {
                "type": "assistant",
                "uuid": a_uuid,
                "parentUuid": prev,
                "timestamp": f"2026-07-08T10:{i:02d}:00Z",
                "cwd": "/x",
                "sessionId": "s-chain",
                "message": {
                    "model": "m",
                    "content": [{"type": "tool_use", "id": f"t{i}", "name": "bash", "input": {}}],
                    "usage": {"input_tokens": 1},
                },
            }
        )
        tr_uuid = f"tr{i}"
        records.append(
            {
                "type": "user",
                "uuid": tr_uuid,
                "parentUuid": a_uuid,
                "timestamp": f"2026-07-08T10:{i:02d}:05Z",
                "cwd": "/x",
                "sessionId": "s-chain",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}],
                },
            }
        )
        prev = tr_uuid
    records.append(
        {
            "type": "assistant",
            "uuid": "a-final",
            "parentUuid": prev,
            "timestamp": "2026-07-08T11:00:00Z",
            "cwd": "/x",
            "sessionId": "s-chain",
            "message": {
                "model": "m",
                "content": [{"type": "text", "text": "final answer"}],
                "usage": {"input_tokens": 2},
            },
        }
    )
    (projects / "s-chain.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )

    parse_incremental(config)
    with closing(get_connection(config.storage.db_path)) as conn:
        # The final assistant is > 20 hops from the human utterance.
        final = conn.execute(
            "SELECT prompt_text, response_text FROM requests WHERE request_id = 'a-final'"
        ).fetchone()
        # A mid-chain tool_use assistant also resolves to the same human prompt.
        mid = conn.execute(
            "SELECT prompt_text FROM requests WHERE request_id = 'a20'"
        ).fetchone()
    assert final["prompt_text"] == "the real question"
    assert final["response_text"] == "final answer"
    assert mid["prompt_text"] == "the real question"


def test_batch_boundary_prompt_resolved_via_sync_state(temp_home: Path) -> None:
    """1回目の collect で user 行のみ、2回目で対応する assistant 行を取り込むシナリオでも、
    sync_state に永続化した直近人間発話コンテキスト経由で prompt_text が解決されること。"""
    config = load_config()
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    log = projects / "s-batch.jsonl"

    user_rec = {
        "type": "user",
        "uuid": "u1",
        "parentUuid": None,
        "timestamp": "2026-07-08T09:59:58Z",
        "cwd": "/x",
        "sessionId": "s-batch",
        "message": {"role": "user", "content": "batched prompt"},
    }
    log.write_text(json.dumps(user_rec) + "\n", encoding="utf-8")

    # Batch 1: only the human user record is present -> no billing rows yet.
    assert parse_incremental(config) == 0

    asst_rec = {
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": "u1",
        "timestamp": "2026-07-08T10:00:00Z",
        "cwd": "/x",
        "sessionId": "s-batch",
        "message": {
            "model": "m",
            "content": [{"type": "text", "text": "batched response"}],
            "usage": {"input_tokens": 5},
        },
    }
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asst_rec) + "\n")

    # Batch 2: the assistant's parent (u1) is NOT in this batch's in-memory
    # window; resolution must fall back to the persisted sync_state context.
    assert parse_incremental(config) == 1

    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT prompt_text, response_text, response_time_ms "
            "FROM requests WHERE request_id = 'a1'"
        ).fetchone()
    assert row["prompt_text"] == "batched prompt"
    assert row["response_text"] == "batched response"
    assert row["response_time_ms"] == 2000


def test_store_prompts_false_computes_response_time_only(temp_home: Path) -> None:
    """privacy.store_prompts=false のとき prompt_text/response_text は None だが、
    response_time_ms はタイムスタンプのみで算出できるため常に記録されること(D6)。"""
    config = load_config()
    config.privacy.store_prompts = False
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    user_rec = {
        "type": "user",
        "uuid": "u1",
        "parentUuid": None,
        "timestamp": "2026-07-08T09:59:58Z",
        "cwd": "/x",
        "sessionId": "s1",
        "message": {"role": "user", "content": "secret prompt"},
    }
    asst_rec = {
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": "u1",
        "timestamp": "2026-07-08T10:00:00Z",
        "cwd": "/x",
        "sessionId": "s1",
        "message": {
            "model": "m",
            "content": [{"type": "text", "text": "secret response"}],
            "usage": {"input_tokens": 10},
        },
    }
    (projects / "s1.jsonl").write_text(
        json.dumps(user_rec) + "\n" + json.dumps(asst_rec) + "\n", encoding="utf-8"
    )

    inserted = parse_incremental(config)
    assert inserted == 1
    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT prompt_text, response_text, response_time_ms "
            "FROM requests WHERE session_id = 's1'"
        ).fetchone()
        assert row["prompt_text"] is None
        assert row["response_text"] is None
        # response_time_ms is decoupled from store_prompts and still computed.
        assert row["response_time_ms"] == 2000


def test_missing_uuid_uses_synthetic_id(temp_home: Path) -> None:
    """uuid 欠損時、DB の request_id が missing-{ファイル名}-{行番号} 形式になること。"""
    config = load_config()
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    user_rec = {
        "type": "user",
        "uuid": "u1",
        "parentUuid": None,
        "timestamp": "2026-07-08T09:59:59Z",
        "cwd": "/x",
        "sessionId": "s-syn",
        "message": {"role": "user", "content": "hi"},
    }
    asst_no_uuid = {
        "type": "assistant",
        # uuid intentionally absent -> synthesized as missing-log.jsonl-2
        "parentUuid": "u1",
        "timestamp": "2026-07-08T10:00:00Z",
        "cwd": "/x",
        "sessionId": "s-syn",
        "message": {"model": "m", "usage": {"input_tokens": 2}},
    }
    # line 1 = user, line 2 = assistant (line_no is 1-based)
    (projects / "log.jsonl").write_text(
        json.dumps(user_rec) + "\n" + json.dumps(asst_no_uuid) + "\n",
        encoding="utf-8",
    )

    inserted = parse_incremental(config)
    assert inserted == 1
    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT request_id FROM requests WHERE session_id = 's-syn'"
        ).fetchone()
        assert row["request_id"] == "missing-log.jsonl-2"


def test_reparse_truncates_and_rebuilds(temp_home: Path, sample_project_jsonl: Path) -> None:
    """--reparse 相当(reparse=True)で requests/sync_state を truncate してから
    行0から全再取込され、増分状態に関係なく再構築されること(D7)。"""
    config = load_config()
    init_db(config.storage.db_path)

    assert parse_incremental(config) == 1
    # Incremental run is a no-op once sync_state is caught up.
    assert parse_incremental(config) == 0

    # reparse must wipe sync_state + requests and re-ingest from line 0.
    assert parse_incremental(config, reparse=True) == 1

    with closing(get_connection(config.storage.db_path)) as conn:
        rows = conn.execute("SELECT request_id, prompt_text FROM requests").fetchall()
    assert len(rows) == 1
    assert rows[0]["request_id"] == "a1"
    assert rows[0]["prompt_text"] == "hello"


def test_parse_incremental_survives_unreadable_file(temp_home: Path) -> None:
    """処理対象の .jsonl が open 失敗しても例外を出さず他ファイルは挿入され続けること。"""
    config = load_config()
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    good = {
        "type": "assistant",
        "uuid": "u-good",
        "parentUuid": None,
        "timestamp": "2026-07-08T10:00:00Z",
        "cwd": "/x",
        "sessionId": "s-good",
        "message": {"model": "m", "usage": {"input_tokens": 7}},
    }
    (projects / "good.jsonl").write_text(json.dumps(good) + "\n", encoding="utf-8")
    # bad.jsonl is created as a directory so open("r") raises OSError (IsADirectoryError).
    # collect_files' rglob('*.jsonl') still lists it; parse_incremental must skip it.
    (projects / "bad.jsonl").mkdir()

    inserted = parse_incremental(config)  # must NOT raise
    assert inserted == 1  # only good.jsonl inserted; bad.jsonl skipped
    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute("SELECT request_id FROM requests WHERE session_id = 's-good'").fetchone()
        assert row is not None
        assert row["request_id"] == "u-good"
        # the skipped file must not leave a partial sync_state entry
        bad_state = conn.execute(
            "SELECT 1 FROM sync_state WHERE file_path = ?",
            (str(projects / "bad.jsonl"),),
        ).fetchone()
        assert bad_state is None


def test_collect_survives_null_or_non_string_model(temp_home: Path) -> None:
    """message.model が null / 数値 / message 自体が非dict でも normalize_model_name()
    の AttributeError で例外終了せず、'unknown' として取り込みを継続すること。"""
    config = load_config()
    init_db(config.storage.db_path)
    projects = temp_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    rec_null_model = {
        "type": "assistant",
        "uuid": "u-null",
        "parentUuid": None,
        "timestamp": "2026-07-08T10:00:00Z",
        "cwd": "/x",
        "sessionId": "s-null",
        "message": {"model": None, "usage": {"input_tokens": 1}},
    }
    rec_numeric_model = {
        "type": "assistant",
        "uuid": "u-numeric",
        "parentUuid": "u-null",
        "timestamp": "2026-07-08T10:00:01Z",
        "cwd": "/x",
        "sessionId": "s-null",
        "message": {"model": 123, "usage": {"input_tokens": 2}},
    }
    rec_null_message = {
        "type": "assistant",
        "uuid": "u-nullmsg",
        "parentUuid": "u-numeric",
        "timestamp": "2026-07-08T10:00:02Z",
        "cwd": "/x",
        "sessionId": "s-null",
        "message": None,
    }
    (projects / "s-null.jsonl").write_text(
        json.dumps(rec_null_model)
        + "\n"
        + json.dumps(rec_numeric_model)
        + "\n"
        + json.dumps(rec_null_message)
        + "\n",
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
        assert rows["u-null"] == "unknown"
        assert rows["u-numeric"] == "unknown"
        assert rows["u-nullmsg"] == "unknown"
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
    from claude_meter.collector import _insert_usage
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


def test_parse_incremental_reprocessed_existing_row_not_counted_as_insert(
    temp_home: Path,
    sample_project_jsonl: Path,
) -> None:
    config = load_config()
    init_db(config.storage.db_path)
    assert parse_incremental(config) == 1

    with closing(get_connection(config.storage.db_path)) as conn:
        conn.execute("DELETE FROM sync_state")
        conn.commit()

    assert parse_incremental(config) == 0


def test_is_human_user_empty_content_list_is_not_human() -> None:
    """content が空リスト [] の user レコードは人間発話として誤判定されず False を返すこと。

    空 content では types も [] となり tool_result 判定に到達しないため、以前は誤って True を
    返していた。その回帰を直接検証する。"""
    from claude_meter.collector import _is_human_user

    empty_list = {"type": "user", "message": {"role": "user", "content": []}}
    assert _is_human_user(empty_list) is False

    # 回帰防止: tool_result のみの配列は従来どおり False。
    tool_result_block = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
    tool_only = {"type": "user", "message": {"content": [tool_result_block]}}
    assert _is_human_user(tool_only) is False

    # 回帰防止: テキストブロック配列・素の文字列は人間発話として True。
    text_block = {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}
    assert _is_human_user(text_block) is True
    plain_string = {"type": "user", "message": {"content": "hi"}}
    assert _is_human_user(plain_string) is True


def test_extract_text_blocks_handles_null_and_non_string_text() -> None:
    """text ブロックの ``text`` が null や非文字列でも TypeError にならず空文字列扱いされること。

    ``block.get("text", "")`` はキーが存在し値が None の場合はデフォルト値が使われず None を
    そのまま返すため、以前は ``"".join`` に None が渡り TypeError となっていた。その回帰を検証する。
    """
    from claude_meter.collector import _extract_text_blocks

    # text が null のブロックは空文字列として扱われ、他の正常なブロックと連結できる。
    null_text = [{"type": "text", "text": "hi "}, {"type": "text", "text": None}, {"type": "text", "text": "there"}]
    assert _extract_text_blocks(null_text) == "hi there"

    # text が非文字列(数値など)のブロックも空文字列として扱われる。
    non_string_text = [{"type": "text", "text": 123}]
    assert _extract_text_blocks(non_string_text) == ""

    # text キー自体が欠落している場合は従来どおり空文字列。
    missing_text = [{"type": "text"}]
    assert _extract_text_blocks(missing_text) == ""

    # 通常の文字列 text ブロックは変更なく連結される。
    normal_text = [{"type": "text", "text": "ok"}]
    assert _extract_text_blocks(normal_text) == "ok"



def test_parse_incremental_captures_extended_usage_fields(temp_home: Path) -> None:
    """cache_creation 5m/1h split, service_tier, speed, server_tool_use, and
    inference_geo are captured from the assistant usage block."""
    projects_dir = temp_home / ".claude" / "projects" / "demo"
    projects_dir.mkdir(parents=True)
    session_id = "sess-ext"
    user_record = {
        "type": "user",
        "uuid": "u1",
        "parentUuid": None,
        "timestamp": "2026-07-08T09:59:58.000Z",
        "cwd": "/home/user/demo",
        "sessionId": session_id,
        "message": {"role": "user", "content": "hi"},
    }
    assistant_record = {
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": "u1",
        "timestamp": "2026-07-08T10:00:00.000Z",
        "cwd": "/home/user/demo",
        "sessionId": session_id,
        "message": {
            "model": "claude-opus-4-8",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 731,
                "cache_creation_input_tokens": 21288,
                "cache_read_input_tokens": 25329,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 288,
                    "ephemeral_5m_input_tokens": 21000,
                },
                "server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 1},
                "service_tier": "standard",
                "speed": "standard",
                "inference_geo": "",
            },
        },
    }
    path = projects_dir / f"{session_id}.jsonl"
    path.write_text(
        json.dumps(user_record) + "\n" + json.dumps(assistant_record) + "\n",
        encoding="utf-8",
    )

    config = load_config()
    init_db(config.storage.db_path)
    parse_incremental(config)

    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT cache_creation_5m_tokens, cache_creation_1h_tokens, "
            "web_search_requests, web_fetch_requests, service_tier, speed, "
            "inference_geo FROM requests WHERE request_id = 'a1'"
        ).fetchone()
    assert row is not None
    assert row["cache_creation_5m_tokens"] == 21000
    assert row["cache_creation_1h_tokens"] == 288
    assert row["web_search_requests"] == 3
    assert row["web_fetch_requests"] == 1
    assert row["service_tier"] == "standard"
    assert row["speed"] == "standard"
    # Empty inference_geo is normalized to NULL.
    assert row["inference_geo"] is None


def test_parse_incremental_defaults_extended_fields_when_absent(
    temp_home: Path, sample_project_jsonl: Path
) -> None:
    """The conftest sample has no cache_creation breakdown / service_tier / speed /
    server_tool_use, so token/count fields default to 0 and text fields to NULL."""
    config = load_config()
    init_db(config.storage.db_path)
    parse_incremental(config)

    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT cache_creation_5m_tokens, cache_creation_1h_tokens, "
            "web_search_requests, web_fetch_requests, service_tier, speed, "
            "inference_geo FROM requests WHERE request_id = 'a1'"
        ).fetchone()
    assert row is not None
    assert row["cache_creation_5m_tokens"] == 0
    assert row["cache_creation_1h_tokens"] == 0
    assert row["web_search_requests"] == 0
    assert row["web_fetch_requests"] == 0
    assert row["service_tier"] is None
    assert row["speed"] is None
    assert row["inference_geo"] is None


def test_parallel_tool_use_split_dedups_usage_by_message_id(temp_home: Path) -> None:
    """Claude Code splits one API response containing multiple parallel tool_use
    blocks into several 'assistant' JSONL lines that all share the same real
    Anthropic response id (message.id) and an identical (duplicated) usage block.
    Only the first such line may keep the token usage; later lines with the same
    message.id must be zeroed out so the underlying single Bedrock invocation is
    not billed multiple times."""
    projects_dir = temp_home / ".claude" / "projects" / "demo"
    projects_dir.mkdir(parents=True)
    session_id = "sess-split"
    usage_block = {
        "input_tokens": 2,
        "output_tokens": 1113,
        "cache_creation_input_tokens": 3449,
        "cache_read_input_tokens": 76079,
    }
    records = [
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": None,
            "timestamp": "2026-07-09T02:15:05.881Z",
            "cwd": "/x",
            "sessionId": session_id,
            "message": {
                "id": "msg_bdrk_dup1",
                "model": "claude-sonnet-5",
                "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}],
                "usage": usage_block,
            },
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "parentUuid": "a1",
            "timestamp": "2026-07-09T02:15:06.884Z",
            "cwd": "/x",
            "sessionId": session_id,
            "message": {
                "id": "msg_bdrk_dup1",
                "model": "claude-sonnet-5",
                "content": [{"type": "tool_use", "id": "t2", "name": "bash", "input": {}}],
                "usage": usage_block,
            },
        },
    ]
    (projects_dir / "s-split.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )

    config = load_config()
    init_db(config.storage.db_path)
    inserted = parse_incremental(config)
    assert inserted == 2  # both transcript lines are kept as distinct rows

    with closing(get_connection(config.storage.db_path)) as conn:
        rows = conn.execute(
            "SELECT request_id, message_id, input_tokens, output_tokens, "
            "cache_creation_input_tokens, cache_read_input_tokens "
            "FROM requests WHERE session_id = ? ORDER BY request_id",
            (session_id,),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["message_id"] == "msg_bdrk_dup1"
    assert rows[1]["message_id"] == "msg_bdrk_dup1"
    # Exactly one of the two rows keeps the real usage; the other is zeroed.
    totals = {
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "cache_creation_input_tokens": sum(r["cache_creation_input_tokens"] for r in rows),
        "cache_read_input_tokens": sum(r["cache_read_input_tokens"] for r in rows),
    }
    assert totals == usage_block
    # The first-seen row (a1) is the one that keeps the usage.
    primary = next(r for r in rows if r["request_id"] == "a1")
    duplicate = next(r for r in rows if r["request_id"] == "a2")
    assert primary["input_tokens"] == 2
    assert duplicate["input_tokens"] == 0
    assert duplicate["output_tokens"] == 0
    assert duplicate["cache_creation_input_tokens"] == 0
    assert duplicate["cache_read_input_tokens"] == 0


def test_missing_message_id_keeps_legacy_no_dedup_behavior(
    temp_home: Path, sample_project_jsonl: Path
) -> None:
    """Records without message.id (older ClaudeCode versions / the shared test
    fixture) must not be deduplicated: message_id is NULL and full usage is kept,
    preserving pre-fix behavior exactly."""
    config = load_config()
    init_db(config.storage.db_path)
    parse_incremental(config)

    with closing(get_connection(config.storage.db_path)) as conn:
        row = conn.execute(
            "SELECT message_id, input_tokens FROM requests WHERE request_id = 'a1'"
        ).fetchone()
    assert row is not None
    assert row["message_id"] is None
    assert row["input_tokens"] == 100


def test_message_id_dedup_is_idempotent_across_reparse(temp_home: Path) -> None:
    """Re-running parse_incremental (and a full --reparse) must not flip which row
    is primary, and must not zero-out an already-primary row on re-insert."""
    projects_dir = temp_home / ".claude" / "projects" / "demo"
    projects_dir.mkdir(parents=True)
    session_id = "sess-idem"
    usage_block = {"input_tokens": 5, "output_tokens": 7}
    records = [
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": None,
            "timestamp": "2026-07-09T02:15:05.881Z",
            "cwd": "/x",
            "sessionId": session_id,
            "message": {
                "id": "msg_bdrk_idem",
                "model": "m",
                "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}],
                "usage": usage_block,
            },
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "parentUuid": "a1",
            "timestamp": "2026-07-09T02:15:06.884Z",
            "cwd": "/x",
            "sessionId": session_id,
            "message": {
                "id": "msg_bdrk_idem",
                "model": "m",
                "content": [{"type": "tool_use", "id": "t2", "name": "bash", "input": {}}],
                "usage": usage_block,
            },
        },
    ]
    (projects_dir / "s-idem.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )

    config = load_config()
    init_db(config.storage.db_path)
    assert parse_incremental(config) == 2
    assert parse_incremental(config, reparse=True) == 2

    with closing(get_connection(config.storage.db_path)) as conn:
        rows = conn.execute(
            "SELECT request_id, input_tokens FROM requests WHERE session_id = ? "
            "ORDER BY request_id",
            (session_id,),
        ).fetchall()
    assert {r["request_id"]: r["input_tokens"] for r in rows} == {"a1": 5, "a2": 0}