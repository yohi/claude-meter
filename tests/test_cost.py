from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_meter.cost import calculate_cost
from claude_meter.models import PricingRecord, UsageRecord


def test_calculate_cost_known_model() -> None:
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-sonnet-4-5-20260701",
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=2000,
        cache_read_input_tokens=100,
        source_file=Path("x"),
    )
    pricing = {
        ("anthropic.claude-sonnet-4-5-20260701-v1:0", "us-east-1"): PricingRecord(
            model="anthropic.claude-sonnet-4-5-20260701-v1:0",
            region="us-east-1",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
            cache_creation_price_per_1k=0.00375,
            cache_read_price_per_1k=0.0003,
        )
    }
    cost = calculate_cost(record, pricing, "us-east-1")
    # (1000*0.003 + 500*0.015 + 2000*0.00375 + 100*0.0003) / 1000
    assert cost == pytest.approx(0.003 + 0.0075 + 0.0075 + 0.00003)


def test_calculate_cost_unknown_model_returns_none() -> None:
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="unknown",
        input_tokens=1,
        source_file=Path("x"),
    )
    assert calculate_cost(record, {}, "us-east-1") is None


def test_fill_missing_costs_handles_null_token_columns(tmp_path: Path) -> None:
    """Test that fill_missing_costs handles NULL token columns gracefully."""
    from claude_meter.config import Config
    from claude_meter.cost import fill_missing_costs
    from claude_meter.db import get_connection, init_db
    from claude_meter.pricing import _save_cached_pricing, load_fallback_pricing

    # Setup config with temp DB
    config = Config(storage={"db_path": str(tmp_path / "data.db")})
    init_db(config.storage.db_path)

    # Seed pricing offline (no network)
    _save_cached_pricing(config, load_fallback_pricing())

    # Insert a request row WITHOUT cache-token columns (they will be NULL)
    with get_connection(config.storage.db_path) as conn:
        conn.execute(
            "INSERT INTO requests (timestamp, session_id, request_id, model, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-07-08T10:00:00Z", "s", "r", "claude-sonnet-4-5-20260701", 1000, 500),
        )
        conn.commit()

    # Call fill_missing_costs — must NOT raise
    updated = fill_missing_costs(config)
    assert updated == 1

    # Verify the row was updated with correct cost and region
    with get_connection(config.storage.db_path) as conn:
        cursor = conn.execute("SELECT cost_usd, region FROM requests WHERE id = 1")
        row = cursor.fetchone()
        assert row is not None
        # Cost = (1000*0.003 + 500*0.015) / 1000 = 0.0105 (cache tokens treated as 0)
        assert row["cost_usd"] == pytest.approx(0.0105)
        assert row["region"] == "us-east-1"


def test_fill_missing_costs_does_not_null_existing_cost_when_region_missing(
    tmp_path: Path,
) -> None:
    """region が NULL で cost が算出不能な行を処理しても、既存の cost_usd を
    NULL で上書きしてはならない（region のみを埋める）。"""
    from claude_meter.config import Config
    from claude_meter.cost import fill_missing_costs
    from claude_meter.db import get_connection, init_db
    from claude_meter.pricing import _save_cached_pricing, load_fallback_pricing

    config = Config(storage={"db_path": str(tmp_path / "data.db")})
    init_db(config.storage.db_path)
    _save_cached_pricing(config, load_fallback_pricing())

    # region が NULL、かつ未知モデルのため cost が再計算不能な行を挿入。
    # cost_usd には既存の（何らかの経路で設定済みの）値を持たせておく。
    with get_connection(config.storage.db_path) as conn:
        conn.execute(
            "INSERT INTO requests (timestamp, session_id, request_id, model, "
            "input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-07-08T10:00:00Z", "s", "r", "unknown-model", 1000, 500, 0.0105),
        )
        conn.commit()

    updated = fill_missing_costs(config)
    # cost は再計算できない（未知モデル）ため updated には数えない。
    assert updated == 0

    with get_connection(config.storage.db_path) as conn:
        cursor = conn.execute("SELECT cost_usd, region FROM requests WHERE id = 1")
        row = cursor.fetchone()
        assert row is not None
        # region は埋まるが、既存の cost_usd は消えない。
        assert row["region"] == "us-east-1"
        assert row["cost_usd"] == pytest.approx(0.0105)
