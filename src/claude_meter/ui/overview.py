"""Overview dashboard page."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from claude_meter.db import get_connection
from claude_meter.config import load_config
from claude_meter.model_normalizer import display_model_name


def _summary_for_period(conn: sqlite3.Connection, start: str, end: str) -> dict[str, Any]:
    row = conn.execute(
        """SELECT
            COALESCE(SUM(cost_usd), 0) AS total_cost,
            COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
            COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
            COALESCE(SUM(cache_creation_input_tokens), 0) AS total_cache_creation_input_tokens,
            COALESCE(SUM(cache_read_input_tokens), 0) AS total_cache_read_input_tokens,
            AVG(response_time_ms) AS avg_response_time_ms,
            COUNT(*) AS request_count
        FROM requests
        WHERE timestamp >= ? AND timestamp < ?""",
        (start, end),
    ).fetchone()
    return dict(row)


def _daily_cost(conn: sqlite3.Connection, start: str, end: str) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT date(timestamp) AS date, SUM(cost_usd) AS cost
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
           GROUP BY date(timestamp)
           ORDER BY date""",
        (start, end),
    ).fetchall()
    return pd.DataFrame(rows, columns=["date", "cost"])


def _project_cost(conn: sqlite3.Connection, start: str, end: str) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT project, SUM(cost_usd) AS cost
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
           GROUP BY project
           ORDER BY cost DESC""",
        (start, end),
    ).fetchall()
    return pd.DataFrame(rows, columns=["project", "cost"])


def _model_tokens(conn: sqlite3.Connection, start: str, end: str) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT model,
                  SUM(
                      COALESCE(input_tokens, 0)
                      + COALESCE(output_tokens, 0)
                      + COALESCE(cache_creation_input_tokens, 0)
                      + COALESCE(cache_read_input_tokens, 0)
                  ) AS tokens
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
           GROUP BY model""",
        (start, end),
    ).fetchall()
    return pd.DataFrame(rows, columns=["model", "tokens"])


def _daily_avg_response_time(conn: sqlite3.Connection, start: str, end: str) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT date(timestamp) AS date, AVG(response_time_ms) AS avg_response_time_ms
           FROM requests
           WHERE timestamp >= ? AND timestamp < ? AND response_time_ms IS NOT NULL
           GROUP BY date(timestamp)
           ORDER BY date""",
        (start, end),
    ).fetchall()
    return pd.DataFrame(rows, columns=["date", "avg_response_time_ms"])


# SQL queries for _top_costly_prompts
_TOP_COSTLY_PROMPTS_WITHOUT_TEXT = """SELECT timestamp, project, model, cost_usd
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
           ORDER BY cost_usd DESC NULLS LAST
           LIMIT ?"""

_TOP_COSTLY_PROMPTS_WITH_TEXT = """SELECT timestamp, project, model, cost_usd, prompt_text
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
           ORDER BY cost_usd DESC NULLS LAST
           LIMIT ?"""


def _top_costly_prompts(
    conn: sqlite3.Connection, start: str, end: str, show_prompts: bool, limit: int = 10
) -> pd.DataFrame:
    column_names = ["timestamp", "project", "model", "cost_usd"]
    if show_prompts:
        column_names.append("prompt_text")
        query = _TOP_COSTLY_PROMPTS_WITH_TEXT
    else:
        query = _TOP_COSTLY_PROMPTS_WITHOUT_TEXT
    rows = conn.execute(query, (start, end, limit)).fetchall()
    return pd.DataFrame(rows, columns=column_names)


def render() -> None:
    config = load_config()
    st.title("claude-meter Overview")
    period = st.selectbox("Period", ["Today", "Last 7 days", "Last 30 days", "Custom"])
    today = datetime.now(timezone.utc).date()
    if period == "Today":
        start = today.isoformat()
        end = (today + timedelta(days=1)).isoformat()
    elif period == "Last 7 days":
        start = (today - timedelta(days=6)).isoformat()
        end = (today + timedelta(days=1)).isoformat()
    elif period == "Last 30 days":
        start = (today - timedelta(days=29)).isoformat()
        end = (today + timedelta(days=1)).isoformat()
    else:
        col1, col2 = st.columns(2)
        start = str(col1.date_input("Start", today - timedelta(days=6)))
        end = str(col2.date_input("End", today) + timedelta(days=1))

    with closing(get_connection(config.storage.db_path)) as conn:
        summary = _summary_for_period(conn, start, end)
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Cost", f"${summary['total_cost']:.4f}")
        col2.metric("Input Tokens", f"{summary['total_input_tokens']:,}")
        col3.metric("Output Tokens", f"{summary['total_output_tokens']:,}")

        daily = _daily_cost(conn, start, end)
        if not daily.empty:
            st.altair_chart(
                alt.Chart(daily)
                .mark_line(point=True)
                .encode(x="date:T", y="cost:Q")
                .properties(title="Daily Cost"),
                use_container_width=True,
            )

        proj = _project_cost(conn, start, end)
        if not proj.empty:
            st.altair_chart(
                alt.Chart(proj)
                .mark_bar()
                .encode(x=alt.X("project:N", sort="-y"), y="cost:Q")
                .properties(title="Cost by Project"),
                use_container_width=True,
            )

        models = _model_tokens(conn, start, end)
        if not models.empty:
            models["model"] = models["model"].apply(lambda model: display_model_name(str(model)))
            models = models.groupby("model", as_index=False).agg(tokens=("tokens", "sum"))
            st.altair_chart(
                alt.Chart(models)
                .mark_arc()
                .encode(theta="tokens:Q", color="model:N")
                .properties(title="Token Distribution by Model"),
                use_container_width=True,
            )

        response_times = _daily_avg_response_time(conn, start, end)
        if not response_times.empty:
            st.altair_chart(
                alt.Chart(response_times)
                .mark_line(point=True)
                .encode(x="date:T", y="avg_response_time_ms:Q")
                .properties(title="Average Response Time"),
                use_container_width=True,
            )

        top = _top_costly_prompts(conn, start, end, config.privacy.show_prompts_in_ui)
        if not top.empty:
            st.subheader("Top Costly Prompts")
            st.dataframe(top, use_container_width=True)
