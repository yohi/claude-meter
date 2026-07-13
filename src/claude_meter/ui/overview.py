"""Overview dashboard page."""

from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone, tzinfo
import sqlite3
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from claude_meter.db import get_connection
from claude_meter.config import Config, load_config, resolve_tzinfo
from claude_meter.model_normalizer import display_model_name
from claude_meter.report import (
    ModelRow,
    ReconciliationReport,
    build_report,
    to_csv,
    to_json,
    to_markdown,
)


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


def _tz_offset_modifiers(tz: tzinfo) -> list[str]:
    """Return SQLite ``date()`` modifiers shifting a UTC timestamp into ``tz``.

    The timezone's *current* UTC offset is used (as of now); historical DST
    transitions are intentionally ignored. Stored timestamps stay in UTC, so
    this only affects display/aggregation-time day bucketing. A per-row exact
    conversion would require correlated subqueries and is out of scope.

    Examples: UTC+9 -> ``["+9 hours"]``, UTC-5 -> ``["-5 hours"]``,
    UTC+5:30 -> ``["+5 hours", "+30 minutes"]``, UTC -> ``[]``.
    """
    offset = datetime.now(timezone.utc).astimezone(tz).utcoffset()
    if offset is None:
        return []
    total_minutes = round(offset.total_seconds() / 60)
    if total_minutes == 0:
        return []
    sign = "+" if total_minutes > 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    modifiers = [f"{sign}{hours} hours"]
    if minutes:
        modifiers.append(f"{sign}{minutes} minutes")
    return modifiers


def _utc_date_boundary(day: date, tz: tzinfo) -> str:
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc).isoformat()


def _local_date_expr(tz_modifiers: list[str]) -> str:
    """Build a ``date(timestamp, ...)`` SQL expression for local-day bucketing.

    ``tz_modifiers`` come from :func:`_tz_offset_modifiers` (internally generated
    fixed-offset strings such as ``"+9 hours"``, never user input), so they are
    safe to inline as SQL literals. With no modifiers this is ``date(timestamp)``.
    """
    return "date(timestamp" + "".join(f", '{modifier}'" for modifier in tz_modifiers) + ")"


def _daily_cost(
    conn: sqlite3.Connection, start: str, end: str, tz_modifiers: list[str]
) -> pd.DataFrame:
    date_expr = _local_date_expr(tz_modifiers)
    rows = conn.execute(
        f"""SELECT {date_expr} AS date, COALESCE(SUM(cost_usd), 0.0) AS cost
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
           GROUP BY {date_expr}
           ORDER BY date""",
        (start, end),
    ).fetchall()
    return pd.DataFrame(rows, columns=["date", "cost"])


def _project_cost(conn: sqlite3.Connection, start: str, end: str) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT COALESCE(project, '-') AS project_name, COALESCE(SUM(cost_usd), 0.0) AS cost
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
           GROUP BY COALESCE(project, '-')
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


def _daily_avg_response_time(
    conn: sqlite3.Connection, start: str, end: str, tz_modifiers: list[str]
) -> pd.DataFrame:
    date_expr = _local_date_expr(tz_modifiers)
    rows = conn.execute(
        f"""SELECT {date_expr} AS date, COALESCE(AVG(response_time_ms), 0.0) AS avg_response_time_ms
           FROM requests
           WHERE timestamp >= ? AND timestamp < ? AND response_time_ms IS NOT NULL
           GROUP BY {date_expr}
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

# When prompts are shown, collapse identical prompt_text into a single row:
# total_cost = SUM(cost_usd), occurrences = COUNT(*), latest_timestamp =
# MAX(timestamp). ``project``/``model`` are bare columns, so SQLite resolves
# them from the row holding the single MAX() aggregate (the latest execution).
# Rows with NULL/empty prompt_text are excluded so they never merge into one
# group.
_TOP_COSTLY_PROMPTS_AGGREGATED = """SELECT
               MAX(timestamp) AS latest_timestamp,
               project,
               model,
               SUM(cost_usd) AS total_cost,
               COUNT(*) AS occurrences,
               prompt_text
           FROM requests
           WHERE timestamp >= ? AND timestamp < ?
             AND prompt_text IS NOT NULL AND prompt_text != ''
           GROUP BY prompt_text
           ORDER BY total_cost DESC NULLS LAST
           LIMIT ?"""


def _top_costly_prompts(
    conn: sqlite3.Connection, start: str, end: str, show_prompts: bool, limit: int = 10
) -> pd.DataFrame:
    if show_prompts:
        column_names = [
            "latest_timestamp",
            "project",
            "model",
            "total_cost",
            "occurrences",
            "prompt_text",
        ]
        query = _TOP_COSTLY_PROMPTS_AGGREGATED
    else:
        column_names = ["timestamp", "project", "model", "cost_usd"]
        query = _TOP_COSTLY_PROMPTS_WITHOUT_TEXT
    rows = conn.execute(query, (start, end, limit)).fetchall()
    return pd.DataFrame(rows, columns=column_names)


_LABEL_LAST_7_DAYS = "Last 7 days"
_LABEL_LAST_30_DAYS = "Last 30 days"

_RECONCILIATION_PERIOD_DAYS: dict[str, int | None] = {
    "All time": None,
    _LABEL_LAST_7_DAYS: 7,
    _LABEL_LAST_30_DAYS: 30,
    "Last 90 days": 90,
}


def _reconciliation_days(label: str) -> int | None:
    """Map a reconciliation period label to build_report's ``days`` argument.

    Kept independent from the top-of-page ``Period`` control: build_report only
    supports a last-N-days (or all-time) window, not an arbitrary start/end range.
    """
    return _RECONCILIATION_PERIOD_DAYS[label]


def _reconciliation_breakdown(models: list[ModelRow]) -> pd.DataFrame:
    """Flatten reconciliation model rows into a per-(model, token-type) table.

    Columns mirror claude_meter.report.to_csv so the on-screen table matches the
    CSV download.
    """
    rows: list[dict[str, Any]] = [
        {
            "model": model_row.model,
            "region": model_row.region,
            "token_type": component.token_type,
            "tokens": component.tokens,
            "unit_price_per_1k": component.unit_price_per_1k,
            "estimated_cost": component.cost,
        }
        for model_row in models
        for component in model_row.components
    ]
    return pd.DataFrame(
        rows,
        columns=[
            "model",
            "region",
            "token_type",
            "tokens",
            "unit_price_per_1k",
            "estimated_cost",
        ],
    )


@st.cache_data(ttl=30, show_spinner=False)
def _cached_build_report(
    config: Config, days: int | None, actual_total_cost: float | None
) -> ReconciliationReport:
    return build_report(config, days=days, actual_total_cost=actual_total_cost)


def render() -> None:
    config = load_config()
    st.title("claude-meter Overview")
    period = st.selectbox("Period", ["Today", _LABEL_LAST_7_DAYS, _LABEL_LAST_30_DAYS, "Custom"])
    resolved_tz = resolve_tzinfo(config.ui.timezone)
    tz_modifiers = _tz_offset_modifiers(resolved_tz)
    today = datetime.now(resolved_tz).date()
    if period == "Today":
        start_day = today
        end_day = today + timedelta(days=1)
    elif period == _LABEL_LAST_7_DAYS:
        start_day = today - timedelta(days=6)
        end_day = today + timedelta(days=1)
    elif period == _LABEL_LAST_30_DAYS:
        start_day = today - timedelta(days=29)
        end_day = today + timedelta(days=1)
    else:
        col1, col2 = st.columns(2)
        start_day = col1.date_input("Start", today - timedelta(days=6))
        end_day = col2.date_input("End", today) + timedelta(days=1)
    start = _utc_date_boundary(start_day, resolved_tz)
    end = _utc_date_boundary(end_day, resolved_tz)

    with closing(get_connection(config.storage.db_path)) as conn:
        summary = _summary_for_period(conn, start, end)
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Cost", f"${summary['total_cost']:.4f}")
        col2.metric("Input Tokens", f"{summary['total_input_tokens']:,}")
        col3.metric("Output Tokens", f"{summary['total_output_tokens']:,}")

        daily = _daily_cost(conn, start, end, tz_modifiers)
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

        response_times = _daily_avg_response_time(conn, start, end, tz_modifiers)
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
            st.dataframe(
                top,
                use_container_width=True,
                column_config={
                    "prompt_text": st.column_config.TextColumn("prompt_text", width="large"),
                },
            )

    st.subheader("Reconciliation")
    recon_label = st.selectbox(
        "Reconciliation period",
        list(_RECONCILIATION_PERIOD_DAYS.keys()),
        key="reconciliation_period",
    )
    recon_days = _reconciliation_days(recon_label)
    compare_actual = st.checkbox("実際のBedrock請求額を入力して比較する")
    actual_total_cost: float | None = None
    if compare_actual:
        actual_total_cost = st.number_input(
            "Actual Bedrock total (USD)",
            min_value=0.0,
            value=None,
            step=0.01,
            format="%.4f",
            placeholder="例: 12.3456",
        )
    try:
        report = _cached_build_report(
            config, days=recon_days, actual_total_cost=actual_total_cost
        )
    except Exception as exc:
        st.error(f"Failed to build reconciliation report: {exc}")
        st.stop()

    est_col, stored_col = st.columns(2)
    est_col.metric("Estimated Total Cost", f"${report.estimated_total_cost:.4f}")
    stored_col.metric("Stored Total Cost (DB)", f"${report.stored_total_cost:.4f}")
    if report.actual_total_cost is not None:
        actual_col, delta_abs_col, delta_pct_col = st.columns(3)
        actual_col.metric("Actual Bedrock", f"${report.actual_total_cost:.4f}")
        delta_abs = 0.0 if report.delta_abs is None else report.delta_abs
        delta_abs_col.metric("Delta ($)", f"${delta_abs:.4f}")
        delta_pct = "n/a" if report.delta_pct is None else f"{report.delta_pct:.2f}%"
        delta_pct_col.metric("Delta (%)", delta_pct)

    st.write(
        f"Requests: {report.total_requests} "
        f"(priced {report.priced_requests}, unpriced {report.unpriced_requests})"
    )

    if report.unpriced_models:
        st.warning(f"Unpriced models: {', '.join(report.unpriced_models)}")
    if len(report.distinct_regions) > 1:
        st.warning(
            f"Multiple regions found in data: {', '.join(report.distinct_regions)}"
        )

    st.caption("Divergence flags")
    flags_df = pd.DataFrame(
        {
            "flag": [
                "1-hour cache writes present",
                "Non-standard service-tier requests",
                "Non-standard speed (fast) requests",
                "Server web_search requests",
                "Server web_fetch requests",
            ],
            "value": [
                str(report.cache_1h_present),
                str(report.non_standard_tier_requests),
                str(report.non_standard_speed_requests),
                str(report.web_search_requests),
                str(report.web_fetch_requests),
            ],
        }
    )
    st.dataframe(flags_df, use_container_width=True)

    st.caption("Cost by model x token type")
    st.dataframe(_reconciliation_breakdown(report.models), use_container_width=True)

    csv_col, md_col, json_col = st.columns(3)
    csv_col.download_button(
        "Download CSV",
        data=to_csv(report),
        file_name="reconciliation_report.csv",
        mime="text/csv",
    )
    md_col.download_button(
        "Download Markdown",
        data=to_markdown(report),
        file_name="reconciliation_report.md",
        mime="text/markdown",
    )
    json_col.download_button(
        "Download JSON",
        data=to_json(report),
        file_name="reconciliation_report.json",
        mime="application/json",
    )
