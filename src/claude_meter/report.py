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
    distinct_regions: list[str]
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


def _unit_prices(
    price: PricingRecord | None,
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """Resolve per-1k unit prices (input, output, cache_write_5m, cache_write_1h,
    cache_read) for a PricingRecord, or all-None when the model is unresolved."""
    if price is None:
        return None, None, None, None, None
    unit_1h = (
        None
        if price.input_price_per_1k is None
        else price.input_price_per_1k * CACHE_1H_INPUT_MULTIPLIER
    )
    return (
        price.input_price_per_1k,
        price.output_price_per_1k,
        price.cache_creation_price_per_1k,
        unit_1h,
        price.cache_read_price_per_1k,
    )


def _build_components(
    spec: list[tuple[str, int, float | None]], factor: float
) -> tuple[list[ComponentRow], float | None]:
    """Compute the per-token-type ComponentRows and their total cost (None when
    any component with used tokens has no resolvable price)."""
    components: list[ComponentRow] = []
    estimated: float | None = 0.0
    for token_type, tokens, unit in spec:
        cost = _component_cost(tokens, unit, factor)
        components.append(ComponentRow(token_type, tokens, unit, cost))
        if cost is None:
            estimated = None
        elif estimated is not None:
            estimated += cost
    return components, estimated


def _model_row(
    row: sqlite3.Row,
    default_region: str,
    pricing: dict[tuple[str, str], PricingRecord],
    canonical_index: dict[tuple[str, str], PricingRecord],
    factor: float,
) -> ModelRow:
    """Build one ModelRow from a grouped (model, region) aggregate.

    The aggregate carries its own ``region`` (see ``_fetch_rows``'s
    ``GROUP BY model, region``); a NULL region falls back to ``default_region``
    (the configured default), mirroring ``cost.fill_missing_costs``. That
    effective region is used both to resolve pricing and on the returned row.
    """
    model = str(row["model"])
    effective_region = str(row["region"]) if row["region"] is not None else default_region
    price = price_for_model(model, effective_region, pricing, canonical_index)
    cache_1h = int(row["cw1h"] or 0)
    cache_5m = max(0, int(row["cw"] or 0) - cache_1h)
    unit_input, unit_output, unit_5m, unit_1h, unit_read = _unit_prices(price)

    spec: list[tuple[str, int, float | None]] = [
        ("input", int(row["inp"] or 0), unit_input),
        ("output", int(row["outp"] or 0), unit_output),
        ("cache_write_5m", cache_5m, unit_5m),
        ("cache_write_1h", cache_1h, unit_1h),
        ("cache_read", int(row["cr"] or 0), unit_read),
    ]
    components, estimated = _build_components(spec, factor)

    stored = row["stored_cost"]
    return ModelRow(
        model=model,
        region=effective_region,
        requests=int(row["requests"] or 0),
        priced=estimated is not None,
        estimated_cost=None if estimated is None else round(estimated, 6),
        stored_cost_usd=None if stored is None else round(float(stored), 6),
        components=components,
    )


def _period_filter(days: int | None) -> tuple[str, tuple[str, ...], datetime | None, datetime]:
    """Build the SQL WHERE clause/params for a --days window, plus the resolved
    (period_from, period_to) bounds (period_from is None for all-time)."""
    period_to = datetime.now(timezone.utc)
    period_from = None if days is None else period_to - timedelta(days=days)
    where = "WHERE timestamp >= ?" if period_from is not None else ""
    params = (period_from.isoformat(),) if period_from is not None else ()
    return where, params, period_from, period_to


def _fetch_rows(
    conn: sqlite3.Connection, where: str, params: tuple[str, ...]
) -> tuple[list[sqlite3.Row], sqlite3.Row]:
    """Fetch the per-(model, region) token/cost aggregates and the overall totals row.

    The per-model aggregate is grouped by both ``model`` and the nullable
    ``region`` column, so requests recorded under different regions for the same
    model stay in separate rows (each later priced against its own region; a
    NULL region is resolved to the configured default in ``_model_row``). The
    ``totals`` row is intentionally left model/region-agnostic for overall
    coverage counts.
    """
    model_rows_raw = conn.execute(
        f"""SELECT model,
               region,
               COUNT(*) AS requests,
               SUM(COALESCE(input_tokens, 0)) AS inp,
               SUM(COALESCE(output_tokens, 0)) AS outp,
               SUM(COALESCE(cache_creation_input_tokens, 0)) AS cw,
               SUM(COALESCE(cache_creation_1h_tokens, 0)) AS cw1h,
               SUM(COALESCE(cache_read_input_tokens, 0)) AS cr,
               SUM(cost_usd) AS stored_cost
           FROM requests {where}
           GROUP BY model, region""",
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
    return model_rows_raw, totals


def _aggregate_models(
    model_rows_raw: list[sqlite3.Row],
    default_region: str,
    pricing: dict[tuple[str, str], PricingRecord],
    canonical_index: dict[tuple[str, str], PricingRecord],
    factor: float,
) -> tuple[list[ModelRow], float, int, list[str]]:
    """Build per-model rows and roll up the estimated total cost, the count of
    priced requests, and the sorted list of models that could not be priced."""
    models: list[ModelRow] = []
    estimated_total = 0.0
    priced_requests = 0
    unpriced_models: list[str] = []
    for raw in model_rows_raw:
        model_row = _model_row(raw, default_region, pricing, canonical_index, factor)
        models.append(model_row)
        if model_row.estimated_cost is not None:
            estimated_total += model_row.estimated_cost
            priced_requests += model_row.requests
        else:
            unpriced_models.append(model_row.model)
    models.sort(key=lambda m: (m.estimated_cost is not None, m.estimated_cost or 0.0), reverse=True)
    return models, round(estimated_total, 6), priced_requests, sorted(unpriced_models)


def _apply_actual_total(
    report: ReconciliationReport,
    actual_total_cost: float | None,
    estimated_total: float,
) -> None:
    """Populate the actual-cost/delta fields on report in place, when given."""
    if actual_total_cost is None:
        return
    report.actual_total_cost = actual_total_cost
    report.delta_abs = round(actual_total_cost - estimated_total, 6)
    report.delta_pct = (
        None
        if not estimated_total
        else round((actual_total_cost - estimated_total) / estimated_total * 100, 2)
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
    where, params, period_from, period_to = _period_filter(days)

    with closing(get_connection(config.storage.db_path)) as conn:
        model_rows_raw, totals = _fetch_rows(conn, where, params)

    models, estimated_total, priced_requests, unpriced_models = _aggregate_models(
        model_rows_raw, region, pricing, canonical_index, factor
    )
    distinct_regions = sorted({model_row.region for model_row in models})
    total_requests = int(totals["total"] or 0)

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
        unpriced_models=unpriced_models,
        distinct_regions=distinct_regions,
        cache_1h_present=int(totals["cw1h_total"] or 0) > 0,
        non_standard_tier_requests=int(totals["nonstd_tier"] or 0),
        non_standard_speed_requests=int(totals["nonstd_speed"] or 0),
        web_search_requests=int(totals["web_search"] or 0),
        web_fetch_requests=int(totals["web_fetch"] or 0),
        models=models,
    )
    _apply_actual_total(report, actual_total_cost, estimated_total)
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


def _markdown_actual_section(report: ReconciliationReport) -> list[str]:
    """The 'Actual vs estimate' section, or [] when no actual total was given."""
    if report.actual_total_cost is None:
        return []
    delta_pct = "n/a" if report.delta_pct is None else f"{report.delta_pct}%"
    delta_abs = 0.0 if report.delta_abs is None else report.delta_abs
    return [
        "",
        "## Actual vs estimate",
        "",
        f"- Actual Bedrock: ${report.actual_total_cost:.4f}",
        f"- Delta (actual - estimate): ${delta_abs:.4f} ({delta_pct})",
    ]


def _markdown_component_rows(report: ReconciliationReport) -> list[str]:
    """One Markdown table row per (model, token-type) component."""
    lines: list[str] = []
    for model_row in report.models:
        for component in model_row.components:
            unit = (
                "-"
                if component.unit_price_per_1k is None
                else f"{component.unit_price_per_1k:.6f}"
            )
            cost = "-" if component.cost is None else f"{component.cost:.6f}"
            lines.append(
                f"| {model_row.model} | {component.token_type} | "
                f"{component.tokens:,} | {unit} | {cost} |"
            )
    return lines


def to_markdown(report: ReconciliationReport) -> str:
    pricing_source = report.pricing_source or "n/a"
    pricing_updated_at = report.pricing_updated_at or "n/a"
    lines: list[str] = [
        "# claude-meter Reconciliation Report",
        "",
        f"- Generated: {report.generated_at}",
        f"- Period: {report.period_from or 'all'} .. {report.period_to}",
        f"- Region (assumed): {report.region}",
    ]
    if len(report.distinct_regions) > 1:
        lines.append(
            f"- WARNING multiple regions found in data: {', '.join(report.distinct_regions)}"
        )
    lines += [
        f"- Inference endpoint: {report.inference_endpoint} (factor x{report.endpoint_factor})",
        f"- Pricing source: {pricing_source} (updated {pricing_updated_at})",
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
    lines += _markdown_actual_section(report)
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
    lines += _markdown_component_rows(report)
    return "\n".join(lines) + "\n"
