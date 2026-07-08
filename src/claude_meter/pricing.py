"""Fetch and cache Bedrock pricing from models.dev / AWS / built-in fallback."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
import yaml

from claude_meter.config import Config
from claude_meter.db import get_connection
from claude_meter.models import PricingRecord

# AWS publishes the Bedrock price list as a large JSON at .../current/index.json
# (the bare .../current/ path is a 404). It uses human-readable model names and is
# ~15 MB, so it is only a secondary source here.
AWS_BEDROCK_PRICING_URL = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/index.json"
)
# models.dev exposes a single JSON with ARN-style Bedrock model ids and per-1M-token
# prices; this is the reliable source for matching ClaudeCode model names to prices.
MODELS_DEV_URL = "https://models.dev/api.json"

# models.dev inference-profile prefix -> canonical Bedrock region for the pricing table.
_MODELS_DEV_REGION_BY_PREFIX = {
    "us": "us-east-1",
    "eu": "eu-west-1",
    "au": "ap-southeast-2",
    "jp": "ap-northeast-1",
    "global": "us-east-1",
}


def _cache_path(config: Config) -> Path:
    return Path(config.storage.db_path).parent / "pricing.json"


def _cache_meta_path(config: Config) -> Path:
    return Path(config.storage.db_path).parent / "pricing-meta.yaml"


def _per_1k(value: Any) -> float | None:
    """Convert a models.dev per-million-token price to a per-1k-token price."""
    if value is None:
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def load_fallback_pricing() -> list[PricingRecord]:
    path = Path(__file__).with_name("pricing_fallback.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    records: list[PricingRecord] = []
    now = datetime.now(timezone.utc)
    for model, info in data.get("models", {}).items():
        for arn in info.get("arn_keys", [model]):
            for region, prices in info.get("prices", {}).items():
                records.append(
                    PricingRecord(
                        model=arn,
                        region=region,
                        input_price_per_1k=prices.get("input_price_per_1k"),
                        output_price_per_1k=prices.get("output_price_per_1k"),
                        cache_creation_price_per_1k=prices.get("cache_creation_price_per_1k"),
                        cache_read_price_per_1k=prices.get("cache_read_price_per_1k"),
                        source="built-in",
                        updated_at=now,
                    )
                )
    return records


def _load_cached_pricing(config: Config) -> list[PricingRecord] | None:
    cache = _cache_path(config)
    meta = _cache_meta_path(config)
    if not cache.exists() or not meta.exists():
        return None
    try:
        meta_data = yaml.safe_load(meta.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(meta_data["updated_at"])
    except Exception:
        return None
    ttl = timedelta(hours=config.pricing.cache_ttl_hours)
    if datetime.now(timezone.utc) - updated > ttl:
        return None
    data = json.loads(cache.read_text(encoding="utf-8"))
    return [PricingRecord.model_validate(r) for r in data]


def _save_cached_pricing(config: Config, records: list[PricingRecord]) -> None:
    cache = _cache_path(config)
    meta = _cache_meta_path(config)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps([r.model_dump(mode="json") for r in records], indent=2),
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc).isoformat()
    meta.write_text(yaml.safe_dump({"updated_at": now}), encoding="utf-8")


def upsert_pricing_table(config: Config, records: list[PricingRecord]) -> None:
    """Persist pricing records to the SQLite pricing table."""
    with get_connection(config.storage.db_path) as conn:
        conn.executemany(
            """INSERT INTO pricing (
                model, region, input_price_per_1k, output_price_per_1k,
                cache_creation_price_per_1k, cache_read_price_per_1k,
                source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model, region) DO UPDATE SET
                input_price_per_1k=excluded.input_price_per_1k,
                output_price_per_1k=excluded.output_price_per_1k,
                cache_creation_price_per_1k=excluded.cache_creation_price_per_1k,
                cache_read_price_per_1k=excluded.cache_read_price_per_1k,
                source=excluded.source,
                updated_at=excluded.updated_at""",
            [
                (
                    r.model,
                    r.region,
                    r.input_price_per_1k,
                    r.output_price_per_1k,
                    r.cache_creation_price_per_1k,
                    r.cache_read_price_per_1k,
                    r.source,
                    r.updated_at.isoformat() if r.updated_at else None,
                )
                for r in records
            ],
        )
        conn.commit()


def _extract_aws_sku_price(terms: dict[str, Any], sku: str) -> float | None:
    sku_terms = terms.get(sku, {})
    for term in sku_terms.values():
        for dim in term.get("priceDimensions", {}).values():
            price = dim.get("pricePerUnit", {}).get("USD")
            if price is not None:
                try:
                    return float(Decimal(str(price)))
                except Exception:
                    return None
    return None


def fetch_aws_bedrock_json() -> list[PricingRecord]:
    """Parse the AWS Bedrock price list. AWS prices are already per-1k tokens.

    Note: AWS attributes carry human-readable model names (e.g. "Claude 2.1") rather
    than ARN model ids, so these records rarely match ClaudeCode's model ids; this is
    a best-effort secondary source.
    """
    try:
        resp = requests.get(AWS_BEDROCK_PRICING_URL, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    products = data.get("products", {})
    terms = data.get("terms", {}).get("OnDemand", {})
    now = datetime.now(timezone.utc)
    grouped: dict[tuple[str, str], dict[str, float | None]] = {}
    for sku, prod in products.items():
        attrs = prod.get("attributes", {})
        if str(attrs.get("provider", "")).lower() != "anthropic":
            continue
        model = attrs.get("modelId") or attrs.get("model")
        if not model:
            continue
        region = attrs.get("regionCode", "us-east-1")
        usage_type = str(attrs.get("usagetype", "")).lower()
        inference_type = str(attrs.get("inferenceType", "")).lower()
        price = _extract_aws_sku_price(terms, sku)
        if price is None:
            continue
        entry = grouped.setdefault(
            (model, region),
            {
                "input_price_per_1k": None,
                "output_price_per_1k": None,
                "cache_creation_price_per_1k": None,
                "cache_read_price_per_1k": None,
            },
        )
        if "input" in inference_type or "input" in usage_type:
            entry["input_price_per_1k"] = price
        elif "output" in inference_type or "output" in usage_type:
            entry["output_price_per_1k"] = price
        elif "cache" in usage_type and ("write" in usage_type or "creation" in usage_type):
            entry["cache_creation_price_per_1k"] = price
        elif "cache" in usage_type and "read" in usage_type:
            entry["cache_read_price_per_1k"] = price

    records: list[PricingRecord] = []
    for (model, region), prices in grouped.items():
        records.append(
            PricingRecord(
                model=model,
                region=region,
                source="aws_bedrock_json",
                input_price_per_1k=prices["input_price_per_1k"],
                output_price_per_1k=prices["output_price_per_1k"],
                cache_creation_price_per_1k=prices["cache_creation_price_per_1k"],
                cache_read_price_per_1k=prices["cache_read_price_per_1k"],
                updated_at=now,
            )
        )
    return records


def fetch_models_dev() -> list[PricingRecord]:
    """Parse models.dev/api.json for Bedrock pricing (per-1M-token -> per-1k)."""
    try:
        resp = requests.get(MODELS_DEV_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    provider = data.get("amazon-bedrock", {})
    models = provider.get("models", {}) if isinstance(provider, dict) else {}
    records: list[PricingRecord] = []
    now = datetime.now(timezone.utc)
    for model_id, info in models.items():
        if not isinstance(info, dict):
            continue
        cost = info.get("cost")
        if not isinstance(cost, dict):
            continue
        prefix = model_id.split(".", 1)[0] if "." in model_id else ""
        region = _MODELS_DEV_REGION_BY_PREFIX.get(prefix, "us-east-1")
        records.append(
            PricingRecord(
                model=model_id,
                region=region,
                input_price_per_1k=_per_1k(cost.get("input")),
                output_price_per_1k=_per_1k(cost.get("output")),
                cache_creation_price_per_1k=_per_1k(cost.get("cache_write")),
                cache_read_price_per_1k=_per_1k(cost.get("cache_read")),
                source="models_dev",
                updated_at=now,
            )
        )
    return records


def update_pricing(config: Config, force: bool = False) -> list[PricingRecord]:
    if not force:
        cached = _load_cached_pricing(config)
        if cached is not None:
            upsert_pricing_table(config, cached)
            return cached
    # models.dev is the reliable ARN-keyed source; AWS is a large human-named
    # secondary; the bundled JSON is the final offline fallback.
    for fetcher in (fetch_models_dev, fetch_aws_bedrock_json):
        records = fetcher()
        if records:
            _save_cached_pricing(config, records)
            upsert_pricing_table(config, records)
            return records
    fallback = load_fallback_pricing()
    _save_cached_pricing(config, fallback)
    upsert_pricing_table(config, fallback)
    return fallback
