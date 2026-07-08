from pathlib import Path

import pytest

from claude_meter.config import Config
from claude_meter.models import PricingRecord
from claude_meter.pricing import load_fallback_pricing, update_pricing


def test_load_fallback_pricing_has_records() -> None:
    records = load_fallback_pricing()
    assert records
    assert all(r.region for r in records)


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


def test_fetch_aws_bedrock_json_classifies_cache_before_input_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AWS usagetype values for cache tokens (e.g. 'CacheWriteInputTokenCount',
    'CacheReadInputTokenCount') contain the substring 'input', so cache pricing
    must be classified before the generic input/output substring checks."""
    from claude_meter import pricing

    def _sku(usagetype: str) -> dict:
        return {
            "attributes": {
                "provider": "Anthropic",
                "modelId": "anthropic.claude-3-haiku-20240307-v1:0",
                "regionCode": "us-east-1",
                "usagetype": usagetype,
                "inferenceType": "OnDemand",
            }
        }

    def _terms(price: str) -> dict:
        return {"TERM1": {"priceDimensions": {"DIM1": {"pricePerUnit": {"USD": price}}}}}

    sample = {
        "products": {
            "SKU_INPUT": _sku("USE1-InputTokenCount"),
            "SKU_OUTPUT": _sku("USE1-OutputTokenCount"),
            "SKU_CACHE_WRITE": _sku("USE1-CacheWriteInputTokenCount"),
            "SKU_CACHE_READ": _sku("USE1-CacheReadInputTokenCount"),
        },
        "terms": {
            "OnDemand": {
                "SKU_INPUT": _terms("0.00025"),
                "SKU_OUTPUT": _terms("0.00125"),
                "SKU_CACHE_WRITE": _terms("0.0003"),
                "SKU_CACHE_READ": _terms("0.00003"),
            }
        },
    }

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return sample

    monkeypatch.setattr(pricing.requests, "get", lambda *a, **k: _Resp())
    records = pricing.fetch_aws_bedrock_json()
    assert len(records) == 1
    record = records[0]
    assert record.model == "anthropic.claude-3-haiku-20240307-v1:0"
    assert record.region == "us-east-1"
    assert record.input_price_per_1k == pytest.approx(0.00025)
    assert record.output_price_per_1k == pytest.approx(0.00125)
    assert record.cache_creation_price_per_1k == pytest.approx(0.0003)
    assert record.cache_read_price_per_1k == pytest.approx(0.00003)


def test_update_pricing_skips_source_without_arn_style_keys(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fetcher returning non-empty but non-ARN-style keys (e.g. human-readable
    AWS model names) must be rejected so update_pricing falls through to the
    next configured source instead of caching unusable pricing."""
    from claude_meter import pricing
    from claude_meter.db import init_db

    config = Config()
    config.pricing.primary_source = "aws_bedrock_json"
    config.pricing.fallback_source = "models_dev"
    init_db(config.storage.db_path)

    unusable = [PricingRecord(model="Claude 2.1", region="us-east-1", input_price_per_1k=1.0)]
    usable = [PricingRecord(model="anthropic.claude-3-haiku-20240307-v1:0", region="us-east-1", input_price_per_1k=2.0)]
    monkeypatch.setattr(pricing, "fetch_aws_bedrock_json", lambda: unusable)
    monkeypatch.setattr(pricing, "fetch_models_dev", lambda: usable)
    monkeypatch.setitem(pricing._FETCHERS_BY_SOURCE, "aws_bedrock_json", lambda: unusable)
    monkeypatch.setitem(pricing._FETCHERS_BY_SOURCE, "models_dev", lambda: usable)

    result = pricing.update_pricing(config)
    assert result == usable
