"""Session explorer page."""

import math
import numbers
import sqlite3
from contextlib import closing
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from claude_meter.config import load_config
from claude_meter.db import get_connection
from claude_meter.model_normalizer import display_model_name


def _list_sessions(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT session_id,
                  GROUP_CONCAT(DISTINCT project) AS project,
                  GROUP_CONCAT(DISTINCT git_repository) AS git_repository,
                  COUNT(*) AS requests, COALESCE(SUM(cost_usd), 0) AS total_cost,
                  MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen
           FROM requests
           GROUP BY session_id
           ORDER BY last_seen DESC"""
    ).fetchall()
    return pd.DataFrame(
        rows,
        columns=[
            "session_id",
            "project",
            "git_repository",
            "requests",
            "total_cost",
            "first_seen",
            "last_seen",
        ],
    )


# SQL queries for _session_requests
_SESSION_REQUESTS_WITHOUT_TEXT = """SELECT timestamp, request_id, project, model,
           input_tokens, output_tokens,
           cache_creation_input_tokens, cache_read_input_tokens, cost_usd,
           response_time_ms
           FROM requests
           WHERE session_id = ?
           ORDER BY timestamp"""

_SESSION_REQUESTS_WITH_TEXT = """SELECT timestamp, request_id, project, model,
           input_tokens, output_tokens,
           cache_creation_input_tokens, cache_read_input_tokens, cost_usd,
           response_time_ms, prompt_text, response_text
           FROM requests
           WHERE session_id = ?
           ORDER BY timestamp"""


def _session_requests(
    conn: sqlite3.Connection, session_id: str, show_prompts: bool
) -> pd.DataFrame:
    col_names = [
        "timestamp",
        "request_id",
        "project",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cost_usd",
        "response_time_ms",
    ]
    if show_prompts:
        col_names += ["prompt_text", "response_text"]
        query = _SESSION_REQUESTS_WITH_TEXT
    else:
        query = _SESSION_REQUESTS_WITHOUT_TEXT
    rows = conn.execute(query, (session_id,)).fetchall()
    return pd.DataFrame(rows, columns=col_names)


def _coerce_float(value: object) -> float | None:
    """Coerce a possibly-missing numeric cell to ``float``.

    Returns ``None`` for missing or non-numeric values. pandas stores SQLite
    ``NULL`` numerics as ``NaN`` and may yield numpy scalar types, so this
    accepts any ``object`` and treats ``None``/``NaN``/non-numbers as missing.
    """
    if not isinstance(value, numbers.Real):
        return None
    number = float(value)
    if math.isnan(number):
        return None
    return number


def _format_cost(cost_usd: object) -> str:
    """Format a cost as ``$X.XXXX``; ``$ -`` when missing (``None``/``NaN``)."""
    number = _coerce_float(cost_usd)
    if number is None:
        return "$ -"
    return f"${number:.4f}"


def _format_duration(response_time_ms: object) -> str:
    """Format a duration as ``{ms} ms``; ``-`` when missing (``None``/``NaN``)."""
    number = _coerce_float(response_time_ms)
    if number is None:
        return "-"
    return f"{int(number)} ms"


def _format_tokens(value: object) -> str:
    """Format a token count as a grouped integer; ``-`` when missing (``None``/``NaN``)."""
    number = _coerce_float(value)
    if number is None:
        return "-"
    return f"{int(number):,}"


def _text_or_empty(value: object) -> str:
    """Return a cell's text, or ``""`` for missing (``None``/``NaN``) values."""
    if value is None:
        return ""
    if isinstance(value, numbers.Real) and math.isnan(float(value)):
        return ""
    return str(value)


def _format_project_label(project: object, git_repository: object) -> str:
    """Format a session's project as "name" or "name (git_repository)".

    Returns "-" when project is missing (None/NaN/empty string). When
    git_repository is present (non-missing, non-empty), it is appended in
    parentheses. Handles GROUP_CONCAT's comma-joined multi-value edge case
    transparently (just displays whatever string SQLite returned).
    """
    label = _text_or_empty(project) or "-"
    repo = _text_or_empty(git_repository)
    if repo:
        label += f" ({repo})"
    return label


@dataclass(frozen=True, slots=True)
class _RequestCardDisplay:
    cost: str
    duration: str
    input_tokens: str
    output_tokens: str


def _card_label(row: pd.Series, display: _RequestCardDisplay) -> str:
    """Build the compact one-line summary shown on a collapsed request card."""
    model = display_model_name(str(row["model"]))
    return " · ".join(
        [
            str(row["timestamp"]),
            f"**{model}**",
            f"{display.input_tokens} イン / {display.output_tokens} アウト",
            display.cost,
            display.duration,
        ]
    )


def _render_request_card(row: pd.Series, *, show_prompts: bool, expanded: bool) -> None:
    """Render one request as a collapsible card with metrics and text panes."""
    display = _RequestCardDisplay(
        cost=_format_cost(row["cost_usd"]),
        duration=_format_duration(row["response_time_ms"]),
        input_tokens=_format_tokens(row["input_tokens"]),
        output_tokens=_format_tokens(row["output_tokens"]),
    )
    with st.expander(_card_label(row, display), expanded=expanded):
        cost_col, duration_col, input_col, output_col = st.columns(4)
        cost_col.metric("料金", display.cost)
        duration_col.metric("期間", display.duration)
        input_col.metric("入力", display.input_tokens)
        output_col.metric("出力", display.output_tokens)
        if show_prompts:
            request_col, response_col = st.columns(2)
            request_col.caption("リクエスト")
            request_col.code(_text_or_empty(row["prompt_text"]), language="text")
            response_col.caption("レスポンス")
            response_col.code(_text_or_empty(row["response_text"]), language="text")
        project = _text_or_empty(row["project"]) or "-"
        request_id = _text_or_empty(row["request_id"])
        st.caption(f"project: {project} · request_id: {request_id}")


def _filter_requests(requests_df: pd.DataFrame, show_prompts: bool, search: str) -> pd.DataFrame:
    if show_prompts and search:
        prompt_hits = requests_df["prompt_text"].str.contains(
            search, na=False, case=False, regex=False
        )
        response_hits = requests_df["response_text"].str.contains(
            search, na=False, case=False, regex=False
        )
        return requests_df[prompt_hits | response_hits]
    return requests_df


def _render_request_cards(requests_df: pd.DataFrame, show_prompts: bool, search: str) -> None:
    """Render the per-session request list as collapsible cards, filtered by search."""
    requests_df = _filter_requests(requests_df, show_prompts, search)
    if requests_df.empty:
        st.info("リクエストが見つかりません。")
        return
    for position, (_index, row) in enumerate(requests_df.iterrows()):
        _render_request_card(row, show_prompts=show_prompts, expanded=position == 0)


def render() -> None:
    config = load_config()
    st.title("Session Explorer")
    with closing(get_connection(config.storage.db_path)) as conn:
        sessions = _list_sessions(conn)
        if sessions.empty:
            st.info("セッションが見つかりません。")
            return
        raw_projects = [_text_or_empty(value) for value in sessions["project"]]
        project_options = sorted({label for label in raw_projects if label})
        project_filter = st.selectbox("Project", ["All", *project_options])
        if project_filter != "All":
            matches = sessions["project"].apply(_text_or_empty) == project_filter
            sessions = sessions[matches].copy()
            if sessions.empty:
                st.info("フィルタ条件に一致するセッションが見つかりません。")
                return
        sessions["project"] = sessions.apply(
            lambda row: _format_project_label(row["project"], row["git_repository"]),
            axis=1,
        )
        sessions = sessions.drop(columns=["git_repository"])
        st.dataframe(sessions, use_container_width=True)
        selected = st.selectbox("Session", sessions["session_id"].tolist())
        if not selected:
            return
        show_prompts = config.privacy.show_prompts_in_ui
        requests_df = _session_requests(conn, selected, show_prompts)
        search = st.text_input("Search prompts/responses") if show_prompts else ""
        _render_request_cards(requests_df, show_prompts, search)
