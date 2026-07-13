"""Tests for the reconciliation report (claude_meter.report)."""

import json
from pathlib import Path

import pytest

from claude_meter.config import Config
from claude_meter.db import get_connection, init_db
from claude_meter.models import PricingRecord
from claude_meter.pricing import _save_cached_pricing
from claude_meter.report import build_report, to_csv, to_json, to_markdown

_SONNET_ARN = "anthropic.claude-sonnet-4-5-20260701-v1:0"
_SONNET_MODEL = "claude-sonnet-4-5-20260701"


def _sonnet_pricing() -> list[PricingRecord]:
    return [
        PricingRecord(
            model=_SONNET_ARN,
            region="us-east-1",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
            cache_creation_price_per_1k=0.00375,
            cache_read_price_per_1k=0.0003,
        )
    ]


def _insert(conn: object, columns: str, values: tuple[object, ...]) -> None:
    placeholders = ", ".join("?" for _ in values)
    conn.execute(  # type: ignore[attr-defined]
        f"INSERT INTO requests ({columns}) VALUES ({placeholders})", values
    )


def test_build_report_basic_coverage_and_components(temp_home: Path) -> None:
    config = Config()
    init_db(config.storage.db_path)
    _save_cached_pricing(config, _sonnet_pricing())
    with get_connection(config.storage.db_path) as conn:
        _insert(
            conn,
            "timestamp, session_id, request_id, model, input_tokens, output_tokens, "
            "cache_creation_input_tokens, cache_creation_5m_tokens, "
            "cache_creation_1h_tokens, cache_read_input_tokens",
            ("2026-07-08T10:00:00Z", "s", "r", _SONNET_MODEL, 1000, 500, 3000, 2000, 1000, 100),
        )
        conn.commit()

    report = build_report(config)

    assert report.total_requests == 1
    assert report.priced_requests == 1
    assert report.unpriced_requests == 0
    assert report.unpriced_models == []
    assert report.cache_1h_present is True
    assert report.inference_endpoint == "global"
    assert report.endpoint_factor == pytest.approx(1.0)

    assert len(report.models) == 1
    model_row = report.models[0]
    assert model_row.model == _SONNET_MODEL
    assert model_row.priced is True
    comp = {c.token_type: c for c in model_row.components}
    assert comp["input"].tokens == 1000
    assert comp["input"].cost == pytest.approx(1000 * 0.003 / 1000)
    assert comp["output"].cost == pytest.approx(500 * 0.015 / 1000)
    assert comp["cache_write_5m"].tokens == 2000
    assert comp["cache_write_5m"].cost == pytest.approx(2000 * 0.00375 / 1000)
    assert comp["cache_write_1h"].tokens == 1000
    # 1-hour cache write derived at 2x input: 1000 * (0.003 * 2) / 1000 = 0.006
    assert comp["cache_write_1h"].cost == pytest.approx(1000 * 0.003 * 2 / 1000)
    assert comp["cache_read"].cost == pytest.approx(100 * 0.0003 / 1000)
    expected = 0.003 + 0.0075 + 0.0075 + 0.006 + 0.00003
    assert model_row.estimated_cost == pytest.approx(expected)
    assert report.estimated_total_cost == pytest.approx(expected)


def test_build_report_applies_regional_endpoint_factor(temp_home: Path) -> None:
    config = Config(claude={"inference_endpoint": "regional"})
    init_db(config.storage.db_path)
    _save_cached_pricing(config, _sonnet_pricing())
    with get_connection(config.storage.db_path) as conn:
        _insert(
            conn,
            "timestamp, session_id, request_id, model, input_tokens, output_tokens",
            ("2026-07-08T10:00:00Z", "s", "r", _SONNET_MODEL, 1000, 500),
        )
        conn.commit()

    report = build_report(config)

    assert report.endpoint_factor == pytest.approx(1.10)
    comp = {c.token_type: c for c in report.models[0].components}
    assert comp["input"].cost == pytest.approx(0.003 * 1.10)
    assert comp["output"].cost == pytest.approx(0.0075 * 1.10)


def test_build_report_flags_unpriced_model(temp_home: Path) -> None:
    config = Config()
    init_db(config.storage.db_path)
    # A valid ARN record keeps the cache trusted; the inserted model is unknown.
    _save_cached_pricing(config, _sonnet_pricing())
    with get_connection(config.storage.db_path) as conn:
        _insert(
            conn,
            "timestamp, session_id, request_id, model, input_tokens",
            ("2026-07-08T10:00:00Z", "s", "r", "totally-unknown-xyz", 1000),
        )
        conn.commit()

    report = build_report(config)

    assert "totally-unknown-xyz" in report.unpriced_models
    assert report.unpriced_requests == 1
    assert report.priced_requests == 0
    assert report.models[0].estimated_cost is None


def test_build_report_computes_delta_against_actual(temp_home: Path) -> None:
    config = Config()
    init_db(config.storage.db_path)
    _save_cached_pricing(config, _sonnet_pricing())
    with get_connection(config.storage.db_path) as conn:
        _insert(
            conn,
            "timestamp, session_id, request_id, model, input_tokens, output_tokens",
            ("2026-07-08T10:00:00Z", "s", "r", _SONNET_MODEL, 1000, 500),
        )
        conn.commit()

    report = build_report(config, actual_total_cost=1.0)

    est = report.estimated_total_cost
    assert est > 0
    assert report.actual_total_cost == pytest.approx(1.0)
    assert report.delta_abs == pytest.approx(1.0 - est)
    assert report.delta_pct == pytest.approx((1.0 - est) / est * 100)


def test_report_formatters_produce_expected_shapes(temp_home: Path) -> None:
    config = Config()
    init_db(config.storage.db_path)
    _save_cached_pricing(config, _sonnet_pricing())
    with get_connection(config.storage.db_path) as conn:
        _insert(
            conn,
            "timestamp, session_id, request_id, model, input_tokens, output_tokens, "
            "cache_creation_input_tokens, cache_creation_1h_tokens",
            ("2026-07-08T10:00:00Z", "s", "r", _SONNET_MODEL, 1000, 500, 3000, 1000),
        )
        conn.commit()

    report = build_report(config)

    csv_out = to_csv(report)
    assert "model,region,token_type,tokens,unit_price_per_1k,estimated_cost" in csv_out
    assert "cache_write_1h" in csv_out

    md_out = to_markdown(report)
    assert "# claude-meter Reconciliation Report" in md_out
    assert "Cost by model x token type" in md_out

    parsed = json.loads(to_json(report))
    assert parsed["total_requests"] == report.total_requests
    assert isinstance(parsed["models"], list)
    assert parsed["models"][0]["model"] == _SONNET_MODEL


def test_build_report_legacy_cache_without_breakdown_uses_5m_rate(temp_home: Path) -> None:
    config = Config()
    init_db(config.storage.db_path)
    _save_cached_pricing(config, _sonnet_pricing())
    with get_connection(config.storage.db_path) as conn:
        # Legacy row: aggregate cache-creation only, no 5m/1h breakdown columns.
        _insert(
            conn,
            "timestamp, session_id, request_id, model, cache_creation_input_tokens",
            ("2026-07-08T10:00:00Z", "s", "r", _SONNET_MODEL, 2000),
        )
        conn.commit()

    report = build_report(config)

    comp = {c.token_type: c for c in report.models[0].components}
    assert comp["cache_write_5m"].tokens == 2000
    assert comp["cache_write_1h"].tokens == 0
    assert comp["cache_write_5m"].cost == pytest.approx(2000 * 0.00375 / 1000)
    assert comp["cache_write_1h"].cost == pytest.approx(0.0)
