"""Reconciliation report: estimated cost vs actual AWS Bedrock, by model and token type.

Produces a breakdown grouped by model x token type (input / output / cache-write 5m /
cache-write 1h / cache-read) that maps onto Bedrock's per-model, per-usage-type billing,
plus coverage metrics (priced vs unpriced) and divergence flags (cache-TTL mix, non-standard
service tier / speed, server tool use). Optionally computes the delta against an actual
Bedrock amount for TotalCost divergence investigation.

Costs are recomputed from the *current* effective pricing (not the stored ``cost_usd``),
so a reconciliation run reflects the latest prices; ``stored_total_cost`` is surfaced
alongside as a cross-check (a large gap signals stale stored costs).
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from claude_meter.config import Config
from claude_meter.cost import (
    CACHE_1H_INPUT_MULTIPLIER,
    build_canonical_pricing_index,
    endpoint_cost_factor,
    price_for_model,
)
from claude_meter.db import get_connection
from claude_meter.models import PricingRecord
from claude_meter.pricing import update_pricing

# Token types in the order they appear per model; these map onto Bedrock usage-type
# line items so a row can be reconciled against a Cost Explorer / CUR entry.
TOKEN_TYPES = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")


@dataclass
class ComponentRow:
    """One (model, token-type) reconciliation cell."""

    token_type: str
    tokens: int
    unit_price_per_1k: float | None
    cost: float | None


@dataclass
class ModelRow:
    """Per-model aggregate: token/cost breakdown across the five token types."""

    model: str
    region: str
    requests: int
    priced: bool
    estimated_cost: float | None
    stored_cost_usd: float | None
    components: list[ComponentRow]


@dataclass
class ReconciliationReport:
    """Full reconciliation report: meta, coverage, divergence flags, and model rows."""

    generated_at: str
    period_from: str | None
    period_to: str
    region: str
    inference_endpoint: str
    endpoint_factor: float
    pricing_source: str | None
    pricing_updated_at: str | None
    total_requests: int
    priced_requests: int
    unpriced_requests: int
    estimated_total_cost: float
    stored_total_cost: float
    unpriced_models: list[str]
    cache_1h_present: bool
    non_standard_tier_requests: int
    non_standard_speed_requests: int
    web_search_requests: int
    web_fetch_requests: int
    models: list[ModelRow] = field(default_factory=list)
    actual_total_cost: float | None = None
    delta_abs: float | None = None
    delta_pct: float | None = None


def _component_cost(tokens: int, unit_price_per_1k: float | None, factor: float) -> float | None:
    """Mirror cost._component: 0 tokens -> 0.0 (price irrelevant); tokens but no
    price -> None (avoid undercounting); otherwise tokens * price / 1000 * factor."""
    if tokens <= 0:
        return 0.0
    if unit_price_per_1k is None:
        return None
    return tokens * unit_price_per_1k / 1000 * factor


def _most_common_source(records: list[PricingRecord]) -> str | None:
    counts: dict[str, int] = {}
    for record in records:
        if record.source:
            counts[record.source] = counts.get(record.source, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda key: counts[key])


def _max_updated_at(records: list[PricingRecord]) -> str | None:
    stamps = [record.updated_at for record in records if record.updated_at is not None]
    if not stamps:
        return None
    return max(stamps).isoformat()


def _model_row(
    row: sqlite3.Row,
    region: str,
    pricing: dict[tuple[str, str], PricingRecord],
    canonical_index: dict[tuple[str, str], PricingRecord],
    factor: float,
) -> ModelRow:
    model = str(row["model"])
    price = price_for_model(model, region, pricing, canonical_index)
    cache_1h = int(row["cw1h"] or 0)
    cache_agg = int(row["cw"] or 0)
    cache_5m = max(0, cache_agg - cache_1h)

    if price is None:
        unit_input = unit_output = unit_5m = unit_1h = unit_read = None
    else:
        unit_input = price.input_price_per_1k
        unit_output = price.output_price_per_1k
        unit_5m = price.cache_creation_price_per_1k
        unit_1h = (
            None
            if price.input_price_per_1k is None
            else price.input_price_per_1k * CACHE_1H_INPUT_MULTIPLIER
        )
        unit_read = price.cache_read_price_per_1k

    spec: list[tuple[str, int, float | None]] = [
        ("input", int(row["inp"] or 0), unit_input),
        ("output", int(row["outp"] or 0), unit_output),
        ("cache_write_5m", cache_5m, unit_5m),
        ("cache_write_1h", cache_1h, unit_1h),
        ("cache_read", int(row["cr"] or 0), unit_read),
    ]
    components: list[ComponentRow] = []
    estimated: float | None = 0.0
    for token_type, tokens, unit in spec:
        cost = _component_cost(tokens, unit, factor)
        components.append(ComponentRow(token_type, tokens, unit, cost))
        if cost is None:
            estimated = None
        elif estimated is not None:
            estimated += cost

    stored = row["stored_cost"]
    return ModelRow(
        model=model,
        region=region,
        requests=int(row["requests"] or 0),
        priced=estimated is not None,
        estimated_cost=None if estimated is None else round(estimated, 6),
        stored_cost_usd=None if stored is None else round(float(stored), 6),
        components=components,
    )


def build_report(
    config: Config,
    *,
    days: int | None = None,
    actual_total_cost: float | None = None,
) -> ReconciliationReport:
    """Build a reconciliation report for the requests table.

    ``days`` limits to the last N days (UTC); ``None`` covers all time.
    ``actual_total_cost`` (if given) adds the delta against the estimate.
    """
    records = update_pricing(config)
    pricing: dict[tuple[str, str], PricingRecord] = {(r.model, r.region): r for r in records}
    canonical_index = build_canonical_pricing_index(pricing)
    factor = endpoint_cost_factor(config.claude.inference_endpoint)
    region = config.claude.region

    period_to = datetime.now(timezone.utc)
    period_from: datetime | None = None if days is None else period_to - timedelta(days=days)
    where = "WHERE timestamp >= ?" if period_from is not None else ""
    params: tuple[str, ...] = (period_from.isoformat(),) if period_from is not None else ()

    with closing(get_connection(config.storage.db_path)) as conn:
        model_rows_raw = conn.execute(
            f"""SELECT model,
                   COUNT(*) AS requests,
                   SUM(COALESCE(input_tokens, 0)) AS inp,
                   SUM(COALESCE(output_tokens, 0)) AS outp,
                   SUM(COALESCE(cache_creation_input_tokens, 0)) AS cw,
                   SUM(COALESCE(cache_creation_1h_tokens, 0)) AS cw1h,
                   SUM(COALESCE(cache_read_input_tokens, 0)) AS cr,
                   SUM(cost_usd) AS stored_cost
               FROM requests {where}
               GROUP BY model""",
            params,
        ).fetchall()
        totals = conn.execute(
            f"""SELECT COUNT(*) AS total,
                   COALESCE(SUM(cost_usd), 0.0) AS stored_total,
                   SUM(COALESCE(cache_creation_1h_tokens, 0)) AS cw1h_total,
                   SUM(
                       CASE WHEN service_tier IS NOT NULL AND service_tier != 'standard'
                            THEN 1 ELSE 0 END
                   ) AS nonstd_tier,
                   SUM(
                       CASE WHEN speed IS NOT NULL AND speed != 'standard'
                            THEN 1 ELSE 0 END
                   ) AS nonstd_speed,
                   SUM(COALESCE(web_search_requests, 0)) AS web_search,
                   SUM(COALESCE(web_fetch_requests, 0)) AS web_fetch
               FROM requests {where}""",
            params,
        ).fetchone()

    models: list[ModelRow] = []
    estimated_total = 0.0
    priced_requests = 0
    unpriced_models: list[str] = []
    for raw in model_rows_raw:
        model_row = _model_row(raw, region, pricing, canonical_index, factor)
        models.append(model_row)
        if model_row.estimated_cost is not None:
            estimated_total += model_row.estimated_cost
            priced_requests += model_row.requests
        else:
            unpriced_models.append(model_row.model)
    models.sort(key=lambda m: (m.estimated_cost is not None, m.estimated_cost or 0.0), reverse=True)

    total_requests = int(totals["total"] or 0)
    estimated_total = round(estimated_total, 6)
    report = ReconciliationReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        period_from=None if period_from is None else period_from.isoformat(),
        period_to=period_to.isoformat(),
        region=region,
        inference_endpoint=config.claude.inference_endpoint,
        endpoint_factor=factor,
        pricing_source=_most_common_source(records),
        pricing_updated_at=_max_updated_at(records),
        total_requests=total_requests,
        priced_requests=priced_requests,
        unpriced_requests=total_requests - priced_requests,
        estimated_total_cost=estimated_total,
        stored_total_cost=round(float(totals["stored_total"] or 0.0), 6),
        unpriced_models=sorted(unpriced_models),
        cache_1h_present=int(totals["cw1h_total"] or 0) > 0,
        non_standard_tier_requests=int(totals["nonstd_tier"] or 0),
        non_standard_speed_requests=int(totals["nonstd_speed"] or 0),
        web_search_requests=int(totals["web_search"] or 0),
        web_fetch_requests=int(totals["web_fetch"] or 0),
        models=models,
    )
    if actual_total_cost is not None:
        report.actual_total_cost = actual_total_cost
        report.delta_abs = round(actual_total_cost - estimated_total, 6)
        report.delta_pct = (
            None
            if estimated_total == 0.0
            else round((actual_total_cost - estimated_total) / estimated_total * 100, 2)
        )
    return report


def to_json(report: ReconciliationReport) -> str:
    return json.dumps(asdict(report), indent=2)


def to_csv(report: ReconciliationReport) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["model", "region", "token_type", "tokens", "unit_price_per_1k", "estimated_cost"]
    )
    for model_row in report.models:
        for component in model_row.components:
            writer.writerow(
                [
                    model_row.model,
                    model_row.region,
                    component.token_type,
                    component.tokens,
                    "" if component.unit_price_per_1k is None else component.unit_price_per_1k,
                    "" if component.cost is None else round(component.cost, 6),
                ]
            )
    return buf.getvalue()


def to_markdown(report: ReconciliationReport) -> str:
    lines: list[str] = [
        "# claude-meter Reconciliation Report",
        "",
        f"- Generated: {report.generated_at}",
        f"- Period: {report.period_from or 'all'} .. {report.period_to}",
        f"- Region (assumed): {report.region}",
        f"- Inference endpoint: {report.inference_endpoint} (factor x{report.endpoint_factor})",
        f"- Pricing source: {report.pricing_source} (updated {report.pricing_updated_at})",
        "",
        "## Coverage",
        "",
        f"- Requests: {report.total_requests} "
        f"(priced {report.priced_requests}, unpriced {report.unpriced_requests})",
        f"- Estimated total cost: ${report.estimated_total_cost:.4f}",
        f"- Stored total cost (DB): ${report.stored_total_cost:.4f}",
    ]
    if report.unpriced_models:
        lines.append(f"- WARNING unpriced models: {', '.join(report.unpriced_models)}")
    if report.actual_total_cost is not None:
        delta_pct = "n/a" if report.delta_pct is None else f"{report.delta_pct}%"
        delta_abs = 0.0 if report.delta_abs is None else report.delta_abs
        lines += [
            "",
            "## Actual vs estimate",
            "",
            f"- Actual Bedrock: ${report.actual_total_cost:.4f}",
            f"- Delta (actual - estimate): ${delta_abs:.4f} ({delta_pct})",
        ]
    lines += [
        "",
        "## Divergence flags",
        "",
        f"- 1-hour cache writes present: {report.cache_1h_present}",
        f"- Non-standard service-tier requests: {report.non_standard_tier_requests}",
        f"- Non-standard speed (fast) requests: {report.non_standard_speed_requests}",
        f"- Server web_search requests: {report.web_search_requests}",
        f"- Server web_fetch requests: {report.web_fetch_requests}",
        "",
        "## Cost by model x token type",
        "",
        "| Model | Token type | Tokens | Unit $/1k | Est. cost |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for model_row in report.models:
        for component in model_row.components:
            unit = "-" if component.unit_price_per_1k is None else f"{component.unit_price_per_1k:.6f}"
            cost = "-" if component.cost is None else f"{component.cost:.6f}"
            lines.append(
                f"| {model_row.model} | {component.token_type} | "
                f"{component.tokens:,} | {unit} | {cost} |"
            )
    return "\n".join(lines) + "\n"
