"""Pricing settings page."""

import sqlite3

import pandas as pd
import streamlit as st

from claude_meter.config import load_config
from claude_meter.db import get_connection
from claude_meter.pricing import update_pricing


def _list_pricing(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT model, region, input_price_per_1k, output_price_per_1k,
                  cache_creation_price_per_1k, cache_read_price_per_1k,
                  source, updated_at
           FROM pricing
           ORDER BY model, region"""
    ).fetchall()
    return pd.DataFrame(
        rows,
        columns=[
            "model",
            "region",
            "input_price_per_1k",
            "output_price_per_1k",
            "cache_creation_price_per_1k",
            "cache_read_price_per_1k",
            "source",
            "updated_at",
        ],
    )


def render() -> None:
    config = load_config()
    st.title("Pricing Settings")

    st.subheader("Sources")
    st.write(f"Primary source: `{config.pricing.primary_source}`")
    st.write(f"Fallback source: `{config.pricing.fallback_source}`")
    st.write(f"Cache TTL (hours): {config.pricing.cache_ttl_hours}")

    if st.button("Refresh pricing"):
        update_pricing(config, force=True)
        st.success("Pricing refreshed.")

    with get_connection(config.storage.db_path) as conn:
        pricing = _list_pricing(conn)
    st.subheader("Current Pricing")
    st.dataframe(pricing, use_container_width=True)
