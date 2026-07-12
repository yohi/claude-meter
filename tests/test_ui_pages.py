from contextlib import closing
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from claude_meter.db import get_connection, init_db
from claude_meter.ui import session_explorer
from claude_meter.ui.session_explorer import (
    _coerce_float,
    _format_cost,
    _format_duration,
    _format_project_label,
    _format_tokens,
    _list_sessions,
    _text_or_empty,
)


def test_list_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    init_db(db_path)
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "INSERT INTO requests (timestamp, session_id, request_id, model) VALUES (?, ?, ?, ?)",
            ("2026-07-08T10:00:00Z", "sess-1", "r-1", "m"),
        )
        conn.commit()
        sessions = _list_sessions(conn)
        assert len(sessions) == 1
        assert sessions.iloc[0]["session_id"] == "sess-1"


def test_format_cost_valid() -> None:
    assert _format_cost(1.2345) == "$1.2345"
    assert _format_cost(0) == "$0.0000"


def test_format_cost_missing() -> None:
    assert _format_cost(None) == "$ -"
    assert _format_cost(float("nan")) == "$ -"


def test_format_duration_valid() -> None:
    assert _format_duration(1234) == "1234 ms"
    assert _format_duration(1234.0) == "1234 ms"


def test_format_duration_missing() -> None:
    assert _format_duration(None) == "-"
    assert _format_duration(float("nan")) == "-"


def test_format_tokens_valid() -> None:
    assert _format_tokens(1000) == "1,000"
    assert _format_tokens(0) == "0"


def test_format_tokens_missing() -> None:
    assert _format_tokens(None) == "-"
    assert _format_tokens(float("nan")) == "-"


def test_text_or_empty() -> None:
    assert _text_or_empty("hello") == "hello"
    assert _text_or_empty(None) == ""
    assert _text_or_empty(float("nan")) == ""


def test_coerce_float() -> None:
    assert _coerce_float(1.5) == pytest.approx(1.5)
    assert _coerce_float(2) == pytest.approx(2.0)
    assert _coerce_float(np.int64(2)) == pytest.approx(2.0)
    assert _coerce_float(None) is None
    assert _coerce_float(float("nan")) is None
    assert _coerce_float("x") is None


def test_filter_requests_matches_prompt_or_response_as_literal_text() -> None:
    requests = pd.DataFrame(
        {
            "prompt_text": ["Find [this]", None, "unrelated"],
            "response_text": [None, "FOUND [THIS] here", "unrelated"],
        }
    )

    filtered = session_explorer._filter_requests(requests, show_prompts=True, search="[this]")

    assert filtered.index.tolist() == [0, 1]


def test_card_label_uses_preformatted_display_values() -> None:
    row = pd.Series({"timestamp": "2026-07-12T00:00:00+00:00", "model": "claude-haiku"})
    display = session_explorer._RequestCardDisplay(
        cost="$0.1234", duration="250 ms", input_tokens="1,000", output_tokens="500"
    )

    label = session_explorer._card_label(row, display)

    assert (
        label
        == "2026-07-12T00:00:00+00:00 · **claude-haiku** · 1,000 イン / 500 アウト · $0.1234 · 250 ms"
    )


def test_format_project_label() -> None:
    assert _format_project_label("myproj", None) == "myproj"
    assert (
        _format_project_label("myproj", "github.com/user/myproj")
        == "myproj (github.com/user/myproj)"
    )
    assert _format_project_label(None, None) == "-"
    assert _format_project_label(float("nan"), None) == "-"
    assert _format_project_label("", None) == "-"
    assert _format_project_label("myproj", "") == "myproj"
    assert _format_project_label("myproj", float("nan")) == "myproj"
    assert _format_project_label("a,b", "r1,r2") == "a,b (r1,r2)"


def test_list_sessions_project_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    init_db(db_path)
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "INSERT INTO requests "
            "(timestamp, session_id, request_id, model, project, git_repository) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-07-08T10:00:00Z", "sess-a", "r-1", "m", "projA", "github.com/user/projA"),
        )
        conn.execute(
            "INSERT INTO requests "
            "(timestamp, session_id, request_id, model, project, git_repository) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-07-08T10:01:00Z", "sess-a", "r-2", "m", "projA", "github.com/user/projA"),
        )
        conn.execute(
            "INSERT INTO requests (timestamp, session_id, request_id, model, project) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-07-08T11:00:00Z", "sess-b", "r-3", "m", "projB"),
        )
        conn.commit()
        sessions = _list_sessions(conn)
    assert set(sessions["session_id"]) == {"sess-a", "sess-b"}
    by_id = sessions.set_index("session_id")
    # GROUP_CONCAT(DISTINCT ...) collapses the duplicate projA rows to one value.
    assert by_id.loc["sess-a", "project"] == "projA"
    assert by_id.loc["sess-a", "git_repository"] == "github.com/user/projA"
    assert by_id.loc["sess-b", "project"] == "projB"
    # git_repository is NULL for sess-b -> GROUP_CONCAT returns NULL -> missing in DataFrame.
    assert pd.isna(by_id.loc["sess-b", "git_repository"])
