"""Project breakdown page."""

from contextlib import closing
import sqlite3

import altair as alt
import pandas as pd
import streamlit as st

from claude_meter.config import load_config
from claude_meter.db import get_connection


def _project_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT project,
                  COUNT(*) AS requests,
                  COALESCE(SUM(cost_usd), 0) AS total_cost,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens
           FROM requests
           GROUP BY project
           ORDER BY total_cost DESC"""
    ).fetchall()
    return pd.DataFrame(
        rows,
        columns=["project", "requests", "total_cost", "input_tokens", "output_tokens"],
    )


def render() -> None:
    config = load_config()
    st.title("Project Breakdown")
    with closing(get_connection(config.storage.db_path)) as conn:
        summary = _project_summary(conn)
        st.dataframe(summary, use_container_width=True)
        if not summary.empty:
            st.altair_chart(
                alt.Chart(summary)
                .mark_bar()
                .encode(x=alt.X("project:N", sort="-y"), y="total_cost:Q")
                .properties(title="Cost by Project"),
                use_container_width=True,
            )
