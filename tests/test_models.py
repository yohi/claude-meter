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
