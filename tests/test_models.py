from pathlib import Path

from claude_meter.models import PricingRecord, UsageRecord


def test_usage_record_defaults() -> None:
    record = UsageRecord(
        timestamp="2026-07-08T10:00:00Z",
        session_id="s-1",
        request_id="r-1",
        model="claude-sonnet-4-5-20260701",
        source_file=Path("/tmp/x.jsonl"),
    )
    assert record.input_tokens == 0
    assert record.cache_read_input_tokens == 0


def test_pricing_record_validates() -> None:
    record = PricingRecord(
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        region="us-east-1",
        input_price_per_1k=0.003,
        output_price_per_1k=0.015,
    )
    assert record.cache_creation_price_per_1k is None



def test_usage_record_extended_usage_fields_default() -> None:
    record = UsageRecord(
        timestamp="2026-07-08T10:00:00Z",
        session_id="s-1",
        request_id="r-1",
        model="claude-opus-4-8",
        source_file=Path("/tmp/x.jsonl"),
    )
    assert record.cache_creation_5m_tokens == 0
    assert record.cache_creation_1h_tokens == 0
    assert record.web_search_requests == 0
    assert record.web_fetch_requests == 0
    assert record.service_tier is None
    assert record.speed is None
    assert record.inference_geo is None


def test_usage_record_accepts_extended_usage_fields() -> None:
    record = UsageRecord(
        timestamp="2026-07-08T10:00:00Z",
        session_id="s-1",
        request_id="r-1",
        model="claude-opus-4-8",
        source_file=Path("/tmp/x.jsonl"),
        cache_creation_5m_tokens=21000,
        cache_creation_1h_tokens=288,
        web_search_requests=3,
        web_fetch_requests=1,
        service_tier="standard",
        speed="fast",
        inference_geo="us",
    )
    assert record.cache_creation_5m_tokens == 21000
    assert record.cache_creation_1h_tokens == 288
    assert record.web_search_requests == 3
    assert record.web_fetch_requests == 1
    assert record.service_tier == "standard"
    assert record.speed == "fast"
    assert record.inference_geo == "us"