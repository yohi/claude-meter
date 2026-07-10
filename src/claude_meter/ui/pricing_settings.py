"""Pricing settings page."""

from contextlib import closing
import sqlite3
import math

import pandas as pd
import streamlit as st

from claude_meter.config import load_config
from claude_meter.db import get_connection
from claude_meter.models import PricingRecord
from claude_meter.pricing import load_fallback_pricing, save_pricing_overrides, update_pricing

_PRICE_FIELDS = (
    "input_price_per_1k",
    "output_price_per_1k",
    "cache_creation_price_per_1k",
    "cache_read_price_per_1k",
)


def _is_missing_price(value: float | None) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _price_fields_differ(a: PricingRecord, b: PricingRecord) -> bool:
    for field in _PRICE_FIELDS:
        av = getattr(a, field)
        bv = getattr(b, field)
        if _is_missing_price(av) and _is_missing_price(bv):
            continue
        if _is_missing_price(av) or _is_missing_price(bv) or av != bv:
            return True
    return False


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
    fallback = pd.DataFrame([record.model_dump(mode="json") for record in load_fallback_pricing()])
    editable = st.data_editor(
        fallback,
        disabled=["model", "region", "source", "updated_at"],
        hide_index=True,
        key="fallback_price_overrides",
        num_rows="fixed",
        use_container_width=True,
    )
    if st.button("Save fallback price overrides"):
        baseline = {(record.model, record.region): record for record in load_fallback_pricing()}
        overrides: list[PricingRecord] = []
        for record in editable.to_dict("records"):
            validated = PricingRecord.model_validate(record)
            if not validated.model or not validated.region:
                continue
            base = baseline.get((validated.model, validated.region))
            if base is None or _price_fields_differ(base, validated):
                overrides.append(validated.model_copy(update={"source": "local_override"}))
        save_pricing_overrides(config, overrides)
        st.success("Fallback price overrides saved.")
