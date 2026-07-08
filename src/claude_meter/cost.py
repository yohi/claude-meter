"""Cost calculation from usage records and cached pricing."""

from datetime import datetime, timezone
from pathlib import Path

from claude_meter.config import Config
from claude_meter.db import get_connection
from claude_meter.model_normalizer import model_to_arn_keys, normalize_model_name
from claude_meter.models import PricingRecord, UsageRecord
from claude_meter.pricing import update_pricing


def calculate_cost(
    record: UsageRecord,
    pricing: dict[tuple[str, str], PricingRecord],
    region: str,
) -> float | None:
    normalized = normalize_model_name(record.model)
    if normalized is None:
        return None
    keys = model_to_arn_keys(normalized)
    price: PricingRecord | None = None
    for key in keys:
        price = pricing.get((key, region))
        if price is not None:
            break
    if price is None:
        return None
    input_cost = (record.input_tokens * (price.input_price_per_1k or 0)) / 1000
    output_cost = (record.output_tokens * (price.output_price_per_1k or 0)) / 1000
    cache_creation_cost = (
        record.cache_creation_input_tokens * (price.cache_creation_price_per_1k or 0)
    ) / 1000
    cache_read_cost = (
        record.cache_read_input_tokens * (price.cache_read_price_per_1k or 0)
    ) / 1000
    return input_cost + output_cost + cache_creation_cost + cache_read_cost


def _load_pricing_map(config: Config) -> dict[tuple[str, str], PricingRecord]:
    records = update_pricing(config)
    return {(r.model, r.region): r for r in records}


def fill_missing_costs(config: Config, region: str | None = None) -> int:
    target_region = region or config.claude.region
    pricing = _load_pricing_map(config)
    updated = 0
    with get_connection(config.storage.db_path) as conn:
        cursor = conn.execute(
            "SELECT id, model, region, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens "
            "FROM requests WHERE cost_usd IS NULL OR region IS NULL"
        )
        rows = cursor.fetchall()
        for row in rows:
            row_region = row["region"] or target_region
            record = UsageRecord(
                timestamp=datetime.now(timezone.utc),
                session_id="",
                request_id="",  # placeholder: calculate_cost() は id 系フィールドを未使用
                model=row["model"],
                input_tokens=row["input_tokens"] or 0,
                output_tokens=row["output_tokens"] or 0,
                cache_creation_input_tokens=row["cache_creation_input_tokens"] or 0,
                cache_read_input_tokens=row["cache_read_input_tokens"] or 0,
                source_file=Path("."),
            )
            cost = calculate_cost(record, pricing, row_region)
            if row["region"] is None:
                # region 未設定時は region を必ず埋める。cost が算出できれば併せて埋める。
                conn.execute(
                    "UPDATE requests SET cost_usd = ?, region = ? WHERE id = ?",
                    (cost, row_region, row["id"]),
                )
            elif cost is not None:
                # region は既にあるため cost のみ更新。cost が None なら書き込む意味がないのでスキップ。
                conn.execute(
                    "UPDATE requests SET cost_usd = ? WHERE id = ?",
                    (cost, row["id"]),
                )
            if cost is not None:
                updated += 1
        conn.commit()
    return updated
