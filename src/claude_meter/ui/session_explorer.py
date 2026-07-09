"""Session explorer page."""

from contextlib import closing
import sqlite3

import pandas as pd
import streamlit as st

from claude_meter.config import load_config
from claude_meter.db import get_connection


def _list_sessions(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT session_id, COUNT(*) AS requests, SUM(cost_usd) AS total_cost,
                  MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen
           FROM requests
           GROUP BY session_id
           ORDER BY last_seen DESC"""
    ).fetchall()
    return pd.DataFrame(
        rows,
        columns=["session_id", "requests", "total_cost", "first_seen", "last_seen"],
    )


def _session_requests(conn: sqlite3.Connection, session_id: str, show_prompts: bool) -> pd.DataFrame:
    col_names = [
        "timestamp", "request_id", "project", "model",
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens", "cost_usd",
    ]
    if show_prompts:
        col_names += ["prompt_text", "response_text"]
    columns = ", ".join(col_names)
    rows = conn.execute(
        f"""SELECT {columns}
           FROM requests
           WHERE session_id = ?
           ORDER BY timestamp""",
        (session_id,),
    ).fetchall()
    return pd.DataFrame(rows, columns=col_names)


def render() -> None:
    config = load_config()
    st.title("Session Explorer")
    with closing(get_connection(config.storage.db_path)) as conn:
        sessions = _list_sessions(conn)
        st.dataframe(sessions, use_container_width=True)
        if sessions.empty:
            st.info("セッションが見つかりません。")
        else:
            selected = st.selectbox("Session", sessions["session_id"].tolist())
            if selected:
                requests_df = _session_requests(conn, selected, config.privacy.show_prompts_in_ui)
                st.dataframe(requests_df, use_container_width=True)
                search = st.text_input("Search prompts/responses")
                if search and config.privacy.show_prompts_in_ui:
                    mask = (
                        requests_df["prompt_text"].str.contains(search, na=False, case=False)
                        | requests_df["response_text"].str.contains(search, na=False, case=False)
                    )
                    st.dataframe(requests_df[mask], use_container_width=True)
