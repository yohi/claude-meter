from contextlib import closing
from datetime import date, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from claude_meter.config import Config
from claude_meter.db import get_connection, init_db
from claude_meter.report import ComponentRow, ModelRow
from claude_meter.ui import overview
from claude_meter.ui.overview import (
    _daily_cost,
    _reconciliation_breakdown,
    _reconciliation_days,
    _reconciliation_report_kwargs,
    _summary_for_period,
    _top_costly_prompts,
    _tz_offset_modifiers,
)


def test_summary_for_period_aggregates(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    init_db(db_path)
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "INSERT INTO requests (timestamp, session_id, request_id, model, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-07-15T00:00:00+00:00", "s", "r", "m", 100, 50, 0.123),
        )
        conn.commit()
        summary = _summary_for_period(conn, "2026-07-01", "2026-07-31")
        assert summary["total_cost"] == 0.123
        assert summary["total_input_tokens"] == 100
        assert summary["total_output_tokens"] == 50


def test_tz_offset_modifiers_for_fixed_offsets() -> None:
    assert _tz_offset_modifiers(timezone.utc) == []
    assert _tz_offset_modifiers(timezone(timedelta(hours=9))) == ["+9 hours"]
    assert _tz_offset_modifiers(timezone(timedelta(hours=-5))) == ["-5 hours"]
    assert _tz_offset_modifiers(timezone(timedelta(hours=5, minutes=30))) == [
        "+5 hours",
        "+30 minutes",
    ]


def test_utc_date_boundary_converts_local_midnight() -> None:
    local_tz = timezone(timedelta(hours=9))

    assert overview._utc_date_boundary(date(2026, 7, 12), local_tz) == "2026-07-11T15:00:00+00:00"


def test_daily_cost_buckets_by_local_day(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    init_db(db_path)
    with closing(get_connection(db_path)) as conn:
        conn.executemany(
            "INSERT INTO requests (timestamp, session_id, request_id, model, cost_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("2026-07-10T23:30:00+00:00", "s", "r1", "m", 1.0),  # +9h -> 07-11
                ("2026-07-10T06:00:00+00:00", "s", "r2", "m", 2.0),  # +9h -> 07-10
                ("2026-07-11T00:30:00+00:00", "s", "r3", "m", 4.0),  # +9h -> 07-11
            ],
        )
        conn.commit()
        modifiers = _tz_offset_modifiers(timezone(timedelta(hours=9)))
        local = _daily_cost(conn, "2026-07-01", "2026-07-31", modifiers)
        assert {row["date"]: row["cost"] for _, row in local.iterrows()} == {
            "2026-07-10": 2.0,
            "2026-07-11": 5.0,
        }
        utc = _daily_cost(conn, "2026-07-01", "2026-07-31", [])
        assert {row["date"]: row["cost"] for _, row in utc.iterrows()} == {
            "2026-07-10": 3.0,
            "2026-07-11": 4.0,
        }


def test_top_costly_prompts_aggregates_duplicate_prompts(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    init_db(db_path)
    with closing(get_connection(db_path)) as conn:
        conn.executemany(
            "INSERT INTO requests "
            "(timestamp, session_id, request_id, project, model, cost_usd, prompt_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-07-10T06:12:41.572000+00:00", "s", "r1", "claude-meter", "haiku", 0.05, "同じプロンプト"),
                ("2026-07-10T06:12:41.646000+00:00", "s", "r2", "claude-meter", "haiku", 0.05, "同じプロンプト"),
                ("2026-07-10T06:12:41.700000+00:00", "s", "r3", "claude-meter", "haiku", 0.05, "同じプロンプト"),
                ("2026-07-10T05:00:00.000000+00:00", "s", "r4", "claude-meter", "opus", 1.0, "別のプロンプト"),
            ],
        )
        conn.commit()
        top = _top_costly_prompts(conn, "2026-07-01", "2026-07-31", show_prompts=True)
        # Three identical prompts collapse into one aggregated row, ordered by total_cost DESC.
        assert list(top["prompt_text"]) == ["別のプロンプト", "同じプロンプト"]
        aggregated = top[top["prompt_text"] == "同じプロンプト"].iloc[0]
        assert aggregated["occurrences"] == 3
        assert aggregated["total_cost"] == pytest.approx(0.15)


def test_top_costly_prompts_excludes_null_and_empty_prompts(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    init_db(db_path)
    with closing(get_connection(db_path)) as conn:
        conn.executemany(
            "INSERT INTO requests "
            "(timestamp, session_id, request_id, model, cost_usd, prompt_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("2026-07-10T01:00:00+00:00", "s", "r1", "m", 0.2, "実プロンプト"),
                ("2026-07-10T02:00:00+00:00", "s", "r2", "m", 0.9, None),
                ("2026-07-10T03:00:00+00:00", "s", "r3", "m", 0.8, ""),
            ],
        )
        conn.commit()
        top = _top_costly_prompts(conn, "2026-07-01", "2026-07-31", show_prompts=True)
        assert list(top["prompt_text"]) == ["実プロンプト"]


def test_top_costly_prompts_without_text_is_not_aggregated(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    init_db(db_path)
    with closing(get_connection(db_path)) as conn:
        conn.executemany(
            "INSERT INTO requests "
            "(timestamp, session_id, request_id, model, cost_usd, prompt_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("2026-07-10T06:00:00+00:00", "s", "r1", "m", 0.05, "同じプロンプト"),
                ("2026-07-10T06:00:01+00:00", "s", "r2", "m", 0.05, "同じプロンプト"),
            ],
        )
        conn.commit()
        top = _top_costly_prompts(conn, "2026-07-01", "2026-07-31", show_prompts=False)
        assert list(top.columns) == ["timestamp", "project", "model", "cost_usd"]
        assert len(top) == 2


def test_reconciliation_days_maps_labels() -> None:
    assert _reconciliation_days("All time") is None
    assert _reconciliation_days("Last 7 days") == 7
    assert _reconciliation_days("Last 30 days") == 30
    assert _reconciliation_days("Last 90 days") == 90
    assert _reconciliation_days("Custom") == -1

def test_reconciliation_breakdown_flattens_components() -> None:
    models = [
        ModelRow(
            model="claude-sonnet",
            region="us-east-1",
            requests=3,
            priced=True,
            estimated_cost=0.5,
            stored_cost_usd=0.4,
            components=[
                ComponentRow("input", 1000, 0.003, 0.003),
                ComponentRow("output", 500, 0.015, 0.0075),
            ],
        ),
        ModelRow(
            model="unknown-model",
            region="us-west-2",
            requests=1,
            priced=False,
            estimated_cost=None,
            stored_cost_usd=None,
            components=[ComponentRow("input", 200, None, None)],
        ),
    ]
    df = _reconciliation_breakdown(models)
    assert list(df.columns) == [
        "model",
        "region",
        "token_type",
        "tokens",
        "unit_price_per_1k",
        "estimated_cost",
    ]
    assert len(df) == 3
    first = df.iloc[0]
    assert first["model"] == "claude-sonnet"
    assert first["region"] == "us-east-1"
    assert first["token_type"] == "input"
    assert first["tokens"] == 1000
    assert first["unit_price_per_1k"] == pytest.approx(0.003)
    assert first["estimated_cost"] == pytest.approx(0.003)
    unpriced = df[df["model"] == "unknown-model"].iloc[0]
    assert unpriced["token_type"] == "input"
    assert unpriced["tokens"] == 200
    assert pd.isna(unpriced["unit_price_per_1k"])
    assert pd.isna(unpriced["estimated_cost"])


def test_reconciliation_breakdown_empty_has_columns() -> None:
    df = _reconciliation_breakdown([])
    assert df.empty
    assert list(df.columns) == [
        "model",
        "region",
        "token_type",
        "tokens",
        "unit_price_per_1k",
        "estimated_cost",
    ]


def test_reconciliation_report_kwargs_translates_custom_days_to_none() -> None:
    """Custom reconciliation (days == -1) must pass days=None to build_report."""
    config = Config()
    kwargs = _reconciliation_report_kwargs(
        config,
        recon_days=-1,
        recon_start=date(2026, 7, 12),
        recon_end=date(2026, 7, 12),
        tz_modifiers=["+9 hours"],
        actual_total_cost=None,
    )
    assert kwargs == {
        "config": config,
        "days": None,
        "start": date(2026, 7, 12),
        "end": date(2026, 7, 12),
        "tz_modifiers": ["+9 hours"],
        "actual_total_cost": None,
    }


def test_reconciliation_report_kwargs_preserves_non_custom_days() -> None:
    config = Config()
    kwargs = _reconciliation_report_kwargs(
        config,
        recon_days=7,
        recon_start=None,
        recon_end=None,
        tz_modifiers=["+9 hours"],
        actual_total_cost=None,
    )
    assert kwargs["days"] == 7
    assert kwargs["tz_modifiers"] is None
