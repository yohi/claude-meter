"""Pydantic models for usage and pricing data."""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class UsageRecord(BaseModel):
    timestamp: datetime
    session_id: str
    request_id: str
    project: str | None = None
    git_repository: str | None = None
    model: str
    region: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    response_time_ms: int | None = None
    cost_usd: float | None = None
    prompt_text: str | None = None
    response_text: str | None = None
    source_file: Path


class PricingRecord(BaseModel):
    model: str
    region: str
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    cache_creation_price_per_1k: float | None = None
    cache_read_price_per_1k: float | None = None
    source: str | None = None
    updated_at: datetime | None = None
