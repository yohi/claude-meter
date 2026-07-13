"""Cost calculation from usage records and cached pricing."""

import logging
from datetime import datetime, timezone
from pathlib import Path

from claude_meter.config import Config
from claude_meter.db import get_connection
from claude_meter.model_normalizer import (
    canonical_model_key,
    model_to_arn_keys,
    normalize_model_name,
)
from claude_meter.models import PricingRecord, UsageRecord
from claude_meter.pricing import update_pricing

logger = logging.getLogger(__name__)

# Dedup so a given unpriced model logs only once per process (fill_missing_costs
# re-scans unresolved rows on every call, e.g. from watcher.py's periodic loop;
# without this a permanently-unpriced model would spam the log forever).
_logged_unpriced_models: set[str] = set()


# Regional (geographic/CRIS) inference endpoints carry a ~10% premium over the
# global endpoint on Bedrock, applied to every token type. The matched pricing is
# the global/base rate, so a regional deployment multiplies the whole cost.
_ENDPOINT_FACTORS: dict[str, float] = {"global": 1.0, "regional": 1.10}


# 1-hour cache writes are billed at ~2x the base input rate on Bedrock/Anthropic.
# The 1h rate is not published by models.dev, so it is derived from input pricing.
CACHE_1H_INPUT_MULTIPLIER = 2.0


def endpoint_cost_factor(endpoint: str) -> float:
    """Return the cost multiplier for an inference endpoint type (default global)."""
    return _ENDPOINT_FACTORS.get(endpoint, 1.0)


def price_for_model(
    model: str,
    region: str,
    pricing: dict[tuple[str, str], PricingRecord],
    canonical_index: dict[tuple[str, str], PricingRecord] | None = None,
) -> PricingRecord | None:
    """Resolve the PricingRecord for a model+region.

    Tries the normalized ARN-style keys first, then a prefix/version-stripped
    canonical fallback (matching how models.dev region-prefixed ids reduce to a
    bare ARN key). Returns None when the model is unknown or absent from pricing.
    """
    normalized = normalize_model_name(model)
    if normalized is None:
        return None
    for key in model_to_arn_keys(normalized):
        price = pricing.get((key, region))
        if price is not None:
            return price
    index = (
        canonical_index
        if canonical_index is not None
        else build_canonical_pricing_index(pricing)
    )
    return index.get((canonical_model_key(model), region))


def calculate_cost(
    record: UsageRecord,
    pricing: dict[tuple[str, str], PricingRecord],
    region: str,
    canonical_index: dict[tuple[str, str], PricingRecord] | None = None,
    endpoint_factor: float = 1.0,
) -> float | None:
    price = price_for_model(record.model, region, pricing, canonical_index)
    if price is None:
        if record.model not in _logged_unpriced_models:
            _logged_unpriced_models.add(record.model)
            logger.warning(
                "No pricing found for model=%r region=%r (normalized=%r); cost "
                "will be recorded as NULL. This can mean a typo/unknown model "
                "name, or a legitimate model missing from the pricing cache.",
                record.model,
                region,
                normalize_model_name(record.model),
            )
        return None

    def _component(tokens: int, price_per_1k: float | None) -> float | None:
        # 使用トークンが 0 のコンポーネントは価格未知でも影響しないので 0 を返す。
        # トークンがあるのに価格が None の場合は、0円として過小計上しないよう None を返す。
        if tokens <= 0:
            return 0.0
        if price_per_1k is None:
            return None
        return (tokens * price_per_1k) / 1000

    input_cost = _component(record.input_tokens, price.input_price_per_1k)
    output_cost = _component(record.output_tokens, price.output_price_per_1k)
    # Cache-write cost splits by TTL. models.dev's cache_creation (cache_write)
    # price is the 5-minute rate (1.25x base input); the 1-hour rate (2x base input)
    # is not published, so it is derived from input_price_per_1k. Records ingested
    # before the 5m/1h breakdown was captured have both split columns at 0, so the
    # whole aggregate falls into the 5-minute bucket (unchanged legacy pricing).
    cache_1h_tokens = record.cache_creation_1h_tokens
    cache_5m_tokens = max(0, record.cache_creation_input_tokens - cache_1h_tokens)
    cache_1h_price = (
        None if price.input_price_per_1k is None else price.input_price_per_1k * CACHE_1H_INPUT_MULTIPLIER
    )
    cache_5m_cost = _component(cache_5m_tokens, price.cache_creation_price_per_1k)
    cache_1h_cost = _component(cache_1h_tokens, cache_1h_price)
    cache_read_cost = _component(record.cache_read_input_tokens, price.cache_read_price_per_1k)
    if (
        input_cost is None
        or output_cost is None
        or cache_5m_cost is None
        or cache_1h_cost is None
        or cache_read_cost is None
    ):
        return None
    subtotal = input_cost + output_cost + cache_5m_cost + cache_1h_cost + cache_read_cost
    return subtotal * endpoint_factor


def build_canonical_pricing_index(
    pricing: dict[tuple[str, str], PricingRecord],
) -> dict[tuple[str, str], PricingRecord]:
    """Build a (canonical_key, region) -> PricingRecord index once, so
    calculate_cost()'s canonical fallback can do an O(1) lookup instead of
    scanning the whole pricing dict (and recomputing canonical_model_key() for
    every entry) on every call.

    Multiple raw pricing keys can reduce to the same (canonical_key, region) -
    e.g. models.dev returns both a bare "anthropic.claude-...-v1:0" id and a
    "global.anthropic.claude-...-v1:0" id, and both map to region "us-east-1"
    (see _MODELS_DEV_REGION_BY_PREFIX in pricing.py). When that happens we deterministically
    keep the lexicographically-first raw model id (independent of dict/API
    iteration order) and log a warning so the ambiguity is visible instead of
    silently depending on iteration order.
    """
    index: dict[tuple[str, str], PricingRecord] = {}
    for (p_model, p_region), p_price in sorted(pricing.items(), key=lambda kv: kv[0][0]):
        canon_key = (canonical_model_key(p_model), p_region)
        existing = index.get(canon_key)
        if existing is not None and existing.model != p_model:
            logger.warning(
                "Ambiguous canonical pricing match for canonical_key=%r region=%r: "
                "keeping price from model=%r over model=%r (deterministic: "
                "lexicographically-first wins)",
                canon_key[0],
                p_region,
                existing.model,
                p_model,
            )
            continue
        index[canon_key] = p_price
    return index


def _load_pricing_map(config: Config) -> dict[tuple[str, str], PricingRecord]:
    records = update_pricing(config)
    return {(r.model, r.region): r for r in records}


def fill_missing_costs(config: Config, region: str | None = None) -> int:
    target_region = region or config.claude.region
    pricing = _load_pricing_map(config)
    canonical_index = build_canonical_pricing_index(pricing)
    endpoint_factor = _ENDPOINT_FACTORS.get(config.claude.inference_endpoint, 1.0)
    updated = 0
    with get_connection(config.storage.db_path) as conn:
        cursor = conn.execute(
            "SELECT id, model, region, cost_usd, input_tokens, output_tokens, "
            "cache_creation_input_tokens, cache_creation_5m_tokens, "
            "cache_creation_1h_tokens, cache_read_input_tokens "
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
                cache_creation_5m_tokens=row["cache_creation_5m_tokens"] or 0,
                cache_creation_1h_tokens=row["cache_creation_1h_tokens"] or 0,
                cache_read_input_tokens=row["cache_read_input_tokens"] or 0,
                source_file=Path("."),
            )
            cost = calculate_cost(record, pricing, row_region, canonical_index, endpoint_factor)
            cost_missing = row["cost_usd"] is None
            if row["region"] is None:
                if cost_missing and cost is not None:
                    # region 未設定時は region を必ず埋める。cost が未設定かつ算出できれば併せて埋める。
                    conn.execute(
                        "UPDATE requests SET cost_usd = ?, region = ? WHERE id = ?",
                        (cost, row_region, row["id"]),
                    )
                else:
                    # cost が既存、または算出できない場合は region のみ埋める。既存の
                    # cost_usd を上書きしないようにする。
                    conn.execute(
                        "UPDATE requests SET region = ? WHERE id = ?",
                        (row_region, row["id"]),
                    )
            elif cost_missing and cost is not None:
                # region は既にあるため cost のみ更新。cost が既存、または None なら
                # 書き込む意味がないのでスキップ。
                conn.execute(
                    "UPDATE requests SET cost_usd = ? WHERE id = ?",
                    (cost, row["id"]),
                )
            if cost_missing and cost is not None:
                updated += 1
        conn.commit()
    return updated
