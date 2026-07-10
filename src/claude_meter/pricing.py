"""Fetch and cache Bedrock pricing from models.dev / AWS / built-in fallback."""

import json
import logging
import os
import tempfile
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
import yaml

from claude_meter.config import Config, resolve_config_path
from claude_meter.db import get_connection
from claude_meter.models import PricingRecord

logger = logging.getLogger(__name__)

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


def _cache_path() -> Path:
    return resolve_config_path().parent / "pricing.json"


def _cache_meta_path() -> Path:
    return resolve_config_path().parent / "pricing-meta.yaml"


def _override_path() -> Path:
    return resolve_config_path().parent / "pricing-overrides.json"


def _per_1k(value: Any) -> float | None:
    """Convert a models.dev per-million-token price to a per-1k-token price."""
    if value is None:
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def load_fallback_pricing(config: Config | None = None) -> list[PricingRecord]:
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
    if config is None:
        return records
    return _merge_pricing_records(records, load_pricing_overrides(config))


def _load_cached_pricing(config: Config, allow_stale: bool = False) -> list[PricingRecord] | None:
    cache = _cache_path()
    meta = _cache_meta_path()
    if not cache.exists() or not meta.exists():
        return None
    try:
        meta_data = yaml.safe_load(meta.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(meta_data["updated_at"])
        ttl = timedelta(hours=config.pricing.cache_ttl_hours)
        if not allow_stale and datetime.now(timezone.utc) - updated > ttl:
            return None
        data = json.loads(cache.read_text(encoding="utf-8"))
        return [PricingRecord.model_validate(r) for r in data]
    except Exception as exc:
        logger.warning("Failed to load cached pricing from %s: %s", cache, exc)
        return None


def load_pricing_overrides(config: Config) -> list[PricingRecord]:
    path = _override_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [PricingRecord.model_validate(record) for record in data]
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Failed to load pricing overrides from %s: %s", path, exc)
        return []


def save_pricing_overrides(config: Config, records: list[PricingRecord]) -> None:
    path = _override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 直接上書きすると書き込み中断時にファイルが破損するため、tempfile + os.replace
    # でアトミックに差し替える。_save_cached_pricing と同じ方式。
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = Path(f.name)
            f.write(json.dumps([record.model_dump(mode="json") for record in records], indent=2))
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _merge_pricing_records(
    records: list[PricingRecord], overrides: list[PricingRecord]
) -> list[PricingRecord]:
    by_key = {(record.model, record.region): record for record in records}
    by_key.update({(record.model, record.region): record for record in overrides})
    return list(by_key.values())


def _apply_pricing_overrides(config: Config, records: list[PricingRecord]) -> list[PricingRecord]:
    return _merge_pricing_records(records, load_pricing_overrides(config))


def _save_cached_pricing(config: Config, records: list[PricingRecord]) -> None:
    cache = _cache_path()
    meta = _cache_meta_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    # tmpファイルに書いてから os.replace でアトミックに差し替える。中途のクラッシュにより
    # 半端なJSON/YAMLが残らないようにする。並行実行(CLI/UI/watcherの同時起動)で固定名の
    # tmpファイルが衝突しないよう、tempfileで呼び出しごとに一意なファイル名を生成する。
    cache_tmp: Path | None = None
    meta_tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=cache.parent,
            prefix=f"{cache.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            cache_tmp = Path(f.name)
            f.write(json.dumps([r.model_dump(mode="json") for r in records], indent=2))
        now = datetime.now(timezone.utc).isoformat()
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=meta.parent,
            prefix=f"{meta.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            meta_tmp = Path(f.name)
            f.write(yaml.safe_dump({"updated_at": now}))
        os.replace(cache_tmp, cache)
        os.replace(meta_tmp, meta)
    finally:
        if cache_tmp is not None:
            cache_tmp.unlink(missing_ok=True)
        if meta_tmp is not None:
            meta_tmp.unlink(missing_ok=True)


def upsert_pricing_table(config: Config, records: list[PricingRecord]) -> None:
    """Persist pricing records to the SQLite pricing table."""
    with closing(get_connection(config.storage.db_path)) as conn:
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
    except Exception as exc:
        logger.warning("Failed to fetch pricing from %s: %s", AWS_BEDROCK_PRICING_URL, exc)
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
        # cache系の usage_type（例: "CacheWriteInputTokens", "CacheReadInputTokens"）には
        # "input" という部分文字列も含まれるため、cache判定を先に行い誤分類を防ぐ。
        if "cache" in usage_type and ("write" in usage_type or "creation" in usage_type):
            entry["cache_creation_price_per_1k"] = price
        elif "cache" in usage_type and "read" in usage_type:
            entry["cache_read_price_per_1k"] = price
        elif "input" in inference_type or "input" in usage_type:
            entry["input_price_per_1k"] = price
        elif "output" in inference_type or "output" in usage_type:
            entry["output_price_per_1k"] = price

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
    except Exception as exc:
        logger.warning("Failed to fetch pricing from %s: %s", MODELS_DEV_URL, exc)
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


_FETCHERS_BY_SOURCE: dict[str, Callable[[], list[PricingRecord]]] = {
    "models_dev": fetch_models_dev,
    "aws_bedrock_json": fetch_aws_bedrock_json,
}


def _has_arn_style_keys(records: list[PricingRecord]) -> bool:
    """Return True if at least one record's model key looks like a Bedrock ARN-style
    id (e.g. 'anthropic.claude-...' or a region-prefixed variant such as
    'eu.anthropic.claude-...'). This is the key shape model_to_arn_keys()/
    calculate_cost() expect. Sources like aws_bedrock_json can return human-readable
    model names (e.g. 'Claude 2.1') that never match those lookups."""
    return any("anthropic.claude" in r.model.lower() for r in records)


def update_pricing(config: Config, force: bool = False) -> list[PricingRecord]:
    if not force:
        cached = _load_cached_pricing(config)
        if cached is not None:
            if _has_arn_style_keys(cached):
                effective_records = _apply_pricing_overrides(config, cached)
                upsert_pricing_table(config, effective_records)
                return effective_records
            logger.warning(
                "Ignoring cached pricing because it contains no ARN-style Bedrock model keys"
            )
    stale_cache = _load_cached_pricing(config, allow_stale=True)
    # Fetcher order follows config: primary_source then fallback_source. Unknown
    # source names are logged and skipped; a source's records are only accepted
    # when they contain ARN-style keys that calculate_cost() can actually look up
    # (e.g. aws_bedrock_json often returns human-readable model names that never
    # match). The bundled JSON is the final offline fallback.
    for name in (config.pricing.primary_source, config.pricing.fallback_source):
        fetcher = _FETCHERS_BY_SOURCE.get(name)
        if fetcher is None:
            logger.warning("Unknown pricing source %r in config; skipping", name)
            continue
        records = fetcher()
        if records and _has_arn_style_keys(records):
            _save_cached_pricing(config, records)
            effective_records = _apply_pricing_overrides(config, records)
            upsert_pricing_table(config, effective_records)
            return effective_records
    if stale_cache is not None and _has_arn_style_keys(stale_cache):
        logger.warning("Using stale cached pricing because all configured sources failed")
        effective_records = _apply_pricing_overrides(config, stale_cache)
        upsert_pricing_table(config, effective_records)
        return effective_records
    fallback = load_fallback_pricing(config)

    _save_cached_pricing(config, fallback)

    effective_records = _apply_pricing_overrides(config, fallback)

    upsert_pricing_table(config, effective_records)

    return effective_records
