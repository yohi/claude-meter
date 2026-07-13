import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_meter.cost import build_canonical_pricing_index, calculate_cost
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


def test_calculate_cost_returns_none_when_used_price_component_missing() -> None:
    """使用されているトークンの価格が None (価格ソースから取得できなかった) の場合、
    0円として計上するのではなく None を返し、過小な cost_usd が確定しないこと。"""
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
            cache_creation_price_per_1k=None,  # 価格ソースで欠落
            cache_read_price_per_1k=0.0003,
        )
    }
    cost = calculate_cost(record, pricing, "us-east-1")
    assert cost is None


def test_calculate_cost_ignores_missing_price_for_zero_token_component() -> None:
    """使用されていない(トークン数 0)コンポーネントの価格が None であっても、
    全体の計算には影響しないこと。"""
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-sonnet-4-5-20260701",
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        source_file=Path("x"),
    )
    pricing = {
        ("anthropic.claude-sonnet-4-5-20260701-v1:0", "us-east-1"): PricingRecord(
            model="anthropic.claude-sonnet-4-5-20260701-v1:0",
            region="us-east-1",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
            cache_creation_price_per_1k=None,
            cache_read_price_per_1k=None,
        )
    }
    cost = calculate_cost(record, pricing, "us-east-1")
    assert cost == pytest.approx(0.003 + 0.0075)


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
    NULL で上書きしてはならない(region のみを埋める)。"""
    from claude_meter.config import Config
    from claude_meter.cost import fill_missing_costs
    from claude_meter.db import get_connection, init_db
    from claude_meter.pricing import _save_cached_pricing, load_fallback_pricing

    config = Config(storage={"db_path": str(tmp_path / "data.db")})
    init_db(config.storage.db_path)
    _save_cached_pricing(config, load_fallback_pricing())

    # region が NULL、かつ未知モデルのため cost が再計算不能な行を挿入。
    # cost_usd には既存の(何らかの経路で設定済みの)値を持たせておく。
    with get_connection(config.storage.db_path) as conn:
        conn.execute(
            "INSERT INTO requests (timestamp, session_id, request_id, model, "
            "input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-07-08T10:00:00Z", "s", "r", "unknown-model", 1000, 500, 0.0105),
        )
        conn.commit()

    updated = fill_missing_costs(config)
    # cost は再計算できない(未知モデル)ため updated には数えない。
    assert updated == 0

    with get_connection(config.storage.db_path) as conn:
        cursor = conn.execute("SELECT cost_usd, region FROM requests WHERE id = 1")
        row = cursor.fetchone()
        assert row is not None
        # region は埋まるが、既存の cost_usd は消えない。
        assert row["region"] == "us-east-1"
        assert row["cost_usd"] == pytest.approx(0.0105)


def test_fill_missing_costs_updates_cost_when_region_already_set(
    tmp_path: Path,
) -> None:
    """region が既に設定済みで cost_usd のみ NULL の行は、cost のみ更新され
    updated に数えられる(elif cost is not None ブランチ)。"""
    from claude_meter.config import Config
    from claude_meter.cost import fill_missing_costs
    from claude_meter.db import get_connection, init_db
    from claude_meter.pricing import _save_cached_pricing, load_fallback_pricing

    config = Config(storage={"db_path": str(tmp_path / "data.db")})
    init_db(config.storage.db_path)
    _save_cached_pricing(config, load_fallback_pricing())

    with get_connection(config.storage.db_path) as conn:
        conn.execute(
            "INSERT INTO requests (timestamp, session_id, request_id, model, "
            "region, input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-07-08T10:00:00Z",
                "s",
                "r",
                "claude-sonnet-4-5-20260701",
                "us-east-1",
                1000,
                500,
            ),
        )
        conn.commit()

    updated = fill_missing_costs(config)
    assert updated == 1

    with get_connection(config.storage.db_path) as conn:
        cursor = conn.execute("SELECT cost_usd, region FROM requests WHERE id = 1")
        row = cursor.fetchone()
        assert row is not None
        # Cost = (1000*0.003 + 500*0.015) / 1000 = 0.0105
        assert row["cost_usd"] == pytest.approx(0.0105)
        # region は既存の値のまま変わらない。
        assert row["region"] == "us-east-1"


def test_fill_missing_costs_preserves_existing_cost_when_recalculable(
    tmp_path: Path,
) -> None:
    """region が NULL ですでに cost_usd が設定済みの行は、既知モデルで価格が再計算
    可能であっても、既存の cost_usd を上書きしてはならない(region のみを埋める)。"""
    from claude_meter.config import Config
    from claude_meter.cost import fill_missing_costs
    from claude_meter.db import get_connection, init_db
    from claude_meter.pricing import _save_cached_pricing, load_fallback_pricing

    config = Config(storage={"db_path": str(tmp_path / "data.db")})
    init_db(config.storage.db_path)
    _save_cached_pricing(config, load_fallback_pricing())

    # region が NULL だが、既存の cost_usd がある行を挿入(既知モデルのため再計算は可能)。
    with get_connection(config.storage.db_path) as conn:
        conn.execute(
            "INSERT INTO requests (timestamp, session_id, request_id, model, "
            "input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-07-08T10:00:00Z",
                "s",
                "r",
                "claude-sonnet-4-5-20260701",
                1000,
                500,
                999.99,
            ),
        )
        conn.commit()

    updated = fill_missing_costs(config)
    # 既存の cost_usd があるため updated には数えない。
    assert updated == 0

    with get_connection(config.storage.db_path) as conn:
        cursor = conn.execute("SELECT cost_usd, region FROM requests WHERE id = 1")
        row = cursor.fetchone()
        assert row is not None
        # region は埋まるが、既存の cost_usd(再計算値 0.0105 ではなく 999.99)は保持される。
        assert row["region"] == "us-east-1"
        assert row["cost_usd"] == pytest.approx(999.99)


def test_fill_missing_costs_skips_row_when_region_set_and_no_pricing(
    tmp_path: Path,
) -> None:
    """region が既に設定済みで cost_usd が NULL だが、価格情報が無く cost が
    再計算できない行は更新もカウントもされない(書き込む意味がないためスキップ)。"""
    from claude_meter.config import Config
    from claude_meter.cost import fill_missing_costs
    from claude_meter.db import get_connection, init_db
    from claude_meter.pricing import _save_cached_pricing, load_fallback_pricing

    config = Config(storage={"db_path": str(tmp_path / "data.db")})
    init_db(config.storage.db_path)
    _save_cached_pricing(config, load_fallback_pricing())

    with get_connection(config.storage.db_path) as conn:
        conn.execute(
            "INSERT INTO requests (timestamp, session_id, request_id, model, "
            "region, input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-07-08T10:00:00Z",
                "s",
                "r",
                "unknown-model",
                "us-east-1",
                1000,
                500,
            ),
        )
        conn.commit()

    updated = fill_missing_costs(config)
    assert updated == 0

    with get_connection(config.storage.db_path) as conn:
        cursor = conn.execute("SELECT cost_usd, region FROM requests WHERE id = 1")
        row = cursor.fetchone()
        assert row is not None
        assert row["cost_usd"] is None
        assert row["region"] == "us-east-1"


def test_calculate_cost_matches_models_dev_region_prefixed_pricing() -> None:
    """models.dev 由来の region 接頭辞付きキー(eu.anthropic...)でも、非us region で
    キャノニカル照合により単価を引き当てられること(乖離1の回帰)。"""
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-haiku-4-5-20251001",
        input_tokens=1000,
        output_tokens=500,
        source_file=Path("x"),
    )
    pricing = {
        ("eu.anthropic.claude-haiku-4-5-20251001-v1:0", "eu-west-1"): PricingRecord(
            model="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            region="eu-west-1",
            input_price_per_1k=0.0008,
            output_price_per_1k=0.004,
        )
    }
    cost = calculate_cost(record, pricing, "eu-west-1")
    assert cost == pytest.approx(1000 * 0.0008 / 1000 + 500 * 0.004 / 1000)


def test_calculate_cost_prices_current_model_absent_from_whitelist() -> None:
    """fallback JSON 未登録の現行モデル(claude-sonnet-4-5-20250929)でも、models.dev 由来
    の ARN 単価にキャノニカル照合できること(乖離2の回帰)。"""
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-sonnet-4-5-20250929",
        input_tokens=1000,
        output_tokens=500,
        source_file=Path("x"),
    )
    pricing = {
        ("anthropic.claude-sonnet-4-5-20250929-v1:0", "us-east-1"): PricingRecord(
            model="anthropic.claude-sonnet-4-5-20250929-v1:0",
            region="us-east-1",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
        )
    }
    cost = calculate_cost(record, pricing, "us-east-1")
    assert cost == pytest.approx(1000 * 0.003 / 1000 + 500 * 0.015 / 1000)


def test_build_canonical_pricing_index_deterministic_on_collision() -> None:
    """2つの異なる raw model id が同一の (canonical_key, region) に還元される場合、
    dict/API のイテレーション順に依存せず、辞書順で最初の raw model id が決定的に勝つこと。"""
    pricing = {
        (
            "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us-east-1",
        ): PricingRecord(
            model="global.anthropic.claude-haiku-4-5-20251001-v1:0",
            region="us-east-1",
            input_price_per_1k=0.001,
            output_price_per_1k=0.005,
        ),
        (
            "anthropic.claude-haiku-4-5-20251001-v1:0",
            "us-east-1",
        ): PricingRecord(
            model="anthropic.claude-haiku-4-5-20251001-v1:0",
            region="us-east-1",
            input_price_per_1k=0.0008,
            output_price_per_1k=0.004,
        ),
    }
    index = build_canonical_pricing_index(pricing)
    # 衝突する2エントリは単一のキャノニカルキーに畳み込まれる。
    assert len(index) == 1
    # 辞書順で最初の raw model id(anthropic... < global...)が決定的に勝つ。
    assert (
        index[("claude-haiku-4-5-20251001", "us-east-1")].model
        == "anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def test_calculate_cost_accepts_precomputed_canonical_index() -> None:
    """事前計算した canonical index を明示的に渡しても、キャノニカル fallback により
    単価を引き当てられること。"""
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-haiku-4-5-20251001",
        input_tokens=1000,
        output_tokens=500,
        source_file=Path("x"),
    )
    pricing = {
        ("eu.anthropic.claude-haiku-4-5-20251001-v1:0", "eu-west-1"): PricingRecord(
            model="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            region="eu-west-1",
            input_price_per_1k=0.0008,
            output_price_per_1k=0.004,
        )
    }
    canonical_index = build_canonical_pricing_index(pricing)
    cost = calculate_cost(record, pricing, "eu-west-1", canonical_index)
    assert cost == pytest.approx(1000 * 0.0008 / 1000 + 500 * 0.004 / 1000)


def test_calculate_cost_logs_warning_for_unpriced_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """claude- 接頭辞にマッチするが価格が見つからないモデルは警告ログを1回だけ出し、
    2回目以降は(プロセス内 dedup により)重複ログしないこと。"""
    from claude_meter import cost as cost_module

    model = "claude-totally-made-up-typo"
    # 他テストの影響を排除するため、対象モデルの dedup 記録をクリアしておく。
    cost_module._logged_unpriced_models.discard(model)

    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model=model,
        input_tokens=1,
        source_file=Path("x"),
    )

    with caplog.at_level(logging.WARNING, logger="claude_meter.cost"):
        first = calculate_cost(record, {}, "us-east-1")
    assert first is None
    warnings = [r for r in caplog.records if model in r.getMessage()]
    assert len(warnings) == 1

    # 同一モデルの2回目呼び出しでは dedup により警告が出ないこと。
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="claude_meter.cost"):
        second = calculate_cost(record, {}, "us-east-1")
    assert second is None
    warnings_again = [r for r in caplog.records if model in r.getMessage()]
    assert len(warnings_again) == 0



def test_calculate_cost_applies_regional_endpoint_factor() -> None:
    """The regional (geographic) endpoint carries a 10% premium over global; the
    factor multiplies the whole cost uniformly."""
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-sonnet-4-5-20260701",
        input_tokens=1000,
        output_tokens=500,
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
    global_cost = calculate_cost(record, pricing, "us-east-1", endpoint_factor=1.0)
    regional_cost = calculate_cost(record, pricing, "us-east-1", endpoint_factor=1.10)
    assert global_cost is not None
    assert regional_cost is not None
    assert regional_cost == pytest.approx(global_cost * 1.10)


def test_calculate_cost_prices_1h_cache_write_at_double_input() -> None:
    """1-hour cache writes are billed at ~2x the base input rate (not published by
    models.dev, derived from input_price_per_1k); 5-minute writes use the models.dev
    cache_creation (cache_write) price."""
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-sonnet-4-5-20260701",
        cache_creation_input_tokens=3000,
        cache_creation_5m_tokens=2000,
        cache_creation_1h_tokens=1000,
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
    # 5m: 2000 * 0.00375 / 1000 = 0.0075 ; 1h: 1000 * (0.003*2) / 1000 = 0.006
    assert cost == pytest.approx(0.0075 + 0.006)


def test_calculate_cost_legacy_cache_creation_without_breakdown_uses_5m_rate() -> None:
    """Records ingested before the 5m/1h breakdown was captured have both split
    columns at 0; the full aggregate is priced at the 5-minute rate (unchanged)."""
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-sonnet-4-5-20260701",
        cache_creation_input_tokens=2000,
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
    # aggregate 2000 priced entirely at the 5-minute rate: 2000 * 0.00375 / 1000
    assert cost == pytest.approx(0.0075)


def test_fill_missing_costs_applies_regional_endpoint_factor(tmp_path: Path) -> None:
    """When claude.inference_endpoint is 'regional', stored costs carry the 10%
    premium over the global endpoint price."""
    from claude_meter.config import Config
    from claude_meter.cost import fill_missing_costs
    from claude_meter.db import get_connection, init_db
    from claude_meter.pricing import _save_cached_pricing, load_fallback_pricing

    config = Config(
        storage={"db_path": str(tmp_path / "data.db")},
        claude={"inference_endpoint": "regional"},
    )
    init_db(config.storage.db_path)
    _save_cached_pricing(config, load_fallback_pricing())
    with get_connection(config.storage.db_path) as conn:
        conn.execute(
            "INSERT INTO requests (timestamp, session_id, request_id, model, "
            "input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-07-08T10:00:00Z", "s", "r", "claude-sonnet-4-5-20260701", 1000, 500),
        )
        conn.commit()

    fill_missing_costs(config)

    with get_connection(config.storage.db_path) as conn:
        row = conn.execute("SELECT cost_usd FROM requests WHERE id = 1").fetchone()
    assert row is not None
    # Global cost = (1000*0.003 + 500*0.015)/1000 = 0.0105; regional = x 1.10
    assert row["cost_usd"] == pytest.approx(0.0105 * 1.10)


def test_calculate_cost_no_invariant_warning_when_5m_matches_derived(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """派生した 5m トークン数と保存済み cache_creation_5m_tokens が一致する場合、
    5m/1h 不変条件の警告は出ないこと(レガシー全0行・モダン一致行の両方)。"""
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

    # レガシー行: 5m/1h 列がともに 0(集計のみ)。1h == 0 のため警告対象外。
    legacy = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-sonnet-4-5-20260701",
        cache_creation_input_tokens=2000,
        source_file=Path("x"),
    )
    # モダン行: 5m + 1h == 集計、かつ保存 5m が派生値(2000)に一致。
    modern = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-sonnet-4-5-20260701",
        cache_creation_input_tokens=3000,
        cache_creation_5m_tokens=2000,
        cache_creation_1h_tokens=1000,
        source_file=Path("x"),
    )

    with caplog.at_level(logging.WARNING, logger="claude_meter.cost"):
        assert calculate_cost(legacy, pricing, "us-east-1") is not None
        assert calculate_cost(modern, pricing, "us-east-1") is not None
    invariant_warnings = [
        r for r in caplog.records if "5m/1h split invariant" in r.getMessage()
    ]
    assert invariant_warnings == []


def test_calculate_cost_warns_on_invariant_break_without_changing_cost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """非レガシー行(1h > 0)で保存 cache_creation_5m_tokens が派生値と食い違う場合、
    警告を出しつつも、返す cost は派生値(total - 1h)ベースで不変であること
    (食い違った保存値はコスト計算に一切影響しない)。"""
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
    # 集計 3000、1h 1000 → 派生 5m = 2000。保存 5m は 999 で不整合(かつ 1h > 0)。
    record = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        request_id="r",
        model="claude-sonnet-4-5-20260701",
        cache_creation_input_tokens=3000,
        cache_creation_5m_tokens=999,
        cache_creation_1h_tokens=1000,
        source_file=Path("x"),
    )

    with caplog.at_level(logging.WARNING, logger="claude_meter.cost"):
        cost = calculate_cost(record, pricing, "us-east-1")

    # 不変条件違反の警告が1回出ていること。
    invariant_warnings = [
        r for r in caplog.records if "5m/1h split invariant" in r.getMessage()
    ]
    assert len(invariant_warnings) == 1

    # cost は派生値(total - 1h = 2000)ベースで、保存 5m=999 の影響を受けない。
    derived_5m = 3000 - 1000
    expected = (derived_5m * 0.00375 + 1000 * (0.003 * 2)) / 1000
    assert cost == pytest.approx(expected)
    # 保存 5m=999 を使っていた場合の値とは異なることを明示(食い違い値は無影響)。
    wrong_if_stored_used = (999 * 0.00375 + 1000 * (0.003 * 2)) / 1000
    assert expected != pytest.approx(wrong_if_stored_used)