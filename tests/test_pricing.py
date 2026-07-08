from pathlib import Path

import pytest

from claude_meter.config import Config
from claude_meter.models import PricingRecord
from claude_meter.pricing import load_fallback_pricing, update_pricing


def test_load_fallback_pricing_has_records() -> None:
    records = load_fallback_pricing()
    assert records
    assert all(r.region == "us-east-1" for r in records)


def test_update_pricing_uses_cache_when_fresh(temp_home: Path) -> None:
    from claude_meter.db import init_db

    config = Config()
    init_db(config.storage.db_path)
    records = [PricingRecord(model="m", region="us-east-1", input_price_per_1k=1.0)]
    from claude_meter.pricing import _save_cached_pricing

    _save_cached_pricing(config, records)
    result = update_pricing(config)
    assert len(result) == 1
    assert result[0].input_price_per_1k == 1.0


def test_fetch_models_dev_parses_and_converts(monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_meter import pricing

    sample = {
        "amazon-bedrock": {
            "models": {
                "anthropic.claude-haiku-4-5-20251001-v1:0": {
                    "cost": {"input": 1, "output": 5, "cache_read": 0.1, "cache_write": 1.25}
                },
                "eu.anthropic.claude-sonnet-4-5-20250929-v1:0": {
                    "cost": {"input": 3.3, "output": 16.5, "cache_read": 0.33, "cache_write": 4.125}
                },
                "no-cost-model": {"name": "x"},
            }
        }
    }

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return sample

    monkeypatch.setattr(pricing.requests, "get", lambda *a, **k: _Resp())
    records = pricing.fetch_models_dev()
    by_model = {r.model: r for r in records}
    haiku = by_model["anthropic.claude-haiku-4-5-20251001-v1:0"]
    assert haiku.region == "us-east-1"
    # models.dev cost is USD per 1M tokens -> per 1k is /1000
    assert haiku.input_price_per_1k == pytest.approx(0.001)
    assert haiku.output_price_per_1k == pytest.approx(0.005)
    assert haiku.cache_read_price_per_1k == pytest.approx(0.0001)
    # cache_write maps to cache_creation
    assert haiku.cache_creation_price_per_1k == pytest.approx(0.00125)
    assert haiku.source == "models_dev"
    # inference-profile prefix maps to its region
    assert by_model["eu.anthropic.claude-sonnet-4-5-20250929-v1:0"].region == "eu-west-1"
    # a model without a cost object is skipped
    assert "no-cost-model" not in by_model
