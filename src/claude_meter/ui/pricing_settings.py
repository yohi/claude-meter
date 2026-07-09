"""Pricing settings page."""

from contextlib import closing
import sqlite3

import pandas as pd
import streamlit as st

from claude_meter.config import load_config
from claude_meter.db import get_connection
from claude_meter.models import PricingRecord
from claude_meter.pricing import load_fallback_pricing, save_pricing_overrides, update_pricing


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
        with st.spinner("Refreshing pricing..."):
            try:
                update_pricing(config, force=True)
            except Exception as exc:
                st.error(f"Failed to refresh pricing: {exc}")
            else:
                st.success("Pricing refreshed.")

    with closing(get_connection(config.storage.db_path)) as conn:
        pricing = _list_pricing(conn)
    st.subheader("Current Pricing")
    st.dataframe(pricing, use_container_width=True)

    st.subheader("Fallback Price Overrides")
    fallback = pd.DataFrame(
        [record.model_dump(mode="json") for record in load_fallback_pricing(config)]
    )
    editable = st.data_editor(
        fallback,
        disabled=["model", "region", "source", "updated_at"],
        hide_index=True,
        key="fallback_price_overrides",
        num_rows="dynamic",
        use_container_width=True,
    )
    if st.button("Save fallback price overrides"):
        overrides = [
            PricingRecord.model_validate(record).model_copy(
                update={"source": "local_override"}
            )
            for record in editable.to_dict("records")
        ]
        save_pricing_overrides(config, overrides)
        st.success("Fallback price overrides saved.")
