"""Normalize ClaudeCode internal model names to Bedrock ARN-style keys."""

import json
from importlib import resources


def _load_mapping() -> dict[str, list[str]]:
    resource = resources.files("claude_meter").joinpath("pricing_fallback.json")
    data = json.loads(resource.read_text(encoding="utf-8"))
    return {
        name: info["arn_keys"]
        for name, info in data.get("models", {}).items()
    }


_NORMALIZED_TO_ARNS = _load_mapping()


def normalize_model_name(raw_model: str) -> str | None:
    """Return a canonical key if we know this model, otherwise None."""
    raw = raw_model.strip().lower()
    if raw in _NORMALIZED_TO_ARNS:
        return raw
    if raw.startswith("anthropic.claude-"):
        return raw
    return None


def model_to_arn_keys(normalized: str) -> list[str]:
    """Return the Bedrock ARN-style price keys for a normalized model name."""
    return _NORMALIZED_TO_ARNS.get(normalized, [normalized])
