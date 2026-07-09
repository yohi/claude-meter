"""Model breakdown page."""

from contextlib import closing
import sqlite3

import altair as alt
import pandas as pd
import streamlit as st

from claude_meter.config import load_config
from claude_meter.db import get_connection
from claude_meter.model_normalizer import display_model_name

def _model_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT model,
                  COUNT(*) AS requests,
                  SUM(cost_usd) AS total_cost,
                  SUM(
                      COALESCE(input_tokens, 0)
                      + COALESCE(output_tokens, 0)
                      + COALESCE(cache_creation_input_tokens, 0)
                      + COALESCE(cache_read_input_tokens, 0)
                  ) AS total_tokens
           FROM requests
           GROUP BY model
           ORDER BY total_cost DESC"""
    ).fetchall()
    return pd.DataFrame(
        rows,
        columns=["model", "requests", "total_cost", "total_tokens"],
    )


def render() -> None:
    config = load_config()
    st.title("Model Breakdown")
    with closing(get_connection(config.storage.db_path)) as conn:
        summary = _model_summary(conn)
        if not summary.empty:
            summary["model"] = summary["model"].apply(lambda model: display_model_name(str(model)))
            summary = (
                summary.groupby("model", as_index=False)
                .agg(
                    requests=("requests", "sum"),
                    total_cost=("total_cost", "sum"),
                    total_tokens=("total_tokens", "sum"),
                )
                .sort_values("total_cost", ascending=False, na_position="last")
                .reset_index(drop=True)
            )
        st.dataframe(summary, use_container_width=True)
        if not summary.empty:
            st.altair_chart(
                alt.Chart(summary)
                .mark_bar()
                .encode(x=alt.X("model:N", sort="-y"), y="total_cost:Q")
                .properties(title="Cost by Model"),
                use_container_width=True,
            )
