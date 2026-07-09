"""Normalize ClaudeCode internal model names to Bedrock ARN-style keys."""

import json
import re
from importlib import resources


def _load_mapping() -> dict[str, list[str]]:
    resource = resources.files("claude_meter").joinpath("pricing_fallback.json")
    data = json.loads(resource.read_text(encoding="utf-8"))
    return {
        name: info.get("arn_keys", [name])
        for name, info in data.get("models", {}).items()
        if isinstance(info, dict)
    }


_NORMALIZED_TO_ARNS = _load_mapping()


# Bedrock inference-profile region prefixes (e.g. "eu.anthropic.claude-...").
# models.dev exposes these as distinct model ids; they must reduce to the same
# core key as the bare "anthropic.claude-..." id for pricing lookups.
_INFERENCE_PROFILE_PREFIXES = ("us", "eu", "au", "jp", "apac", "global")

# Trailing Bedrock version suffix, e.g. "-v1:0" / "-v2:0".
_VERSION_SUFFIX_RE = re.compile(r"-v\d+:\d+$")


def _strip_inference_profile_prefix(value: str) -> str:
    """Strip a leading Bedrock inference-profile region prefix (e.g. "eu.").

    Returns ``value`` unchanged if it has no recognized prefix, so callers can
    detect "no match" via ``result == value``.
    """
    head, sep, tail = value.partition(".")
    if sep and head in _INFERENCE_PROFILE_PREFIXES:
        return tail
    return value


def normalize_model_name(raw_model: str) -> str | None:
    """Return a canonical key if we recognize this model, otherwise None.

    Recognized shapes: built-in whitelist names, any ClaudeCode internal name
    ("claude-..."), bare Bedrock ARN ids ("anthropic.claude-..."), and
    region/inference-profile-prefixed ARN ids ("eu.anthropic.claude-...").
    """
    raw = raw_model.strip().lower()
    if raw in _NORMALIZED_TO_ARNS:
        return raw
    if raw.startswith(("claude-", "anthropic.claude-")):
        return raw
    stripped = _strip_inference_profile_prefix(raw)
    if stripped != raw and stripped.startswith("anthropic.claude-"):
        return raw
    return None


def model_to_arn_keys(normalized: str) -> list[str]:
    """Return the Bedrock ARN-style price keys for a normalized model name."""
    return _NORMALIZED_TO_ARNS.get(normalized, [normalized])


def canonical_model_key(model_id: str) -> str:
    """Reduce any ClaudeCode/Bedrock model id to a comparable core key.

    Strips the inference-profile region prefix, the "anthropic." provider prefix,
    and the trailing Bedrock version suffix ("-v1:0"), lowercasing the result. This
    lets a ClaudeCode internal name ("claude-haiku-4-5-20251001") match a
    region-prefixed pricing id ("eu.anthropic.claude-haiku-4-5-20251001-v1:0") for
    the same underlying model.
    """
    key = model_id.strip().lower()
    key = _strip_inference_profile_prefix(key)
    if key.startswith("anthropic."):
        key = key[len("anthropic.") :]
    return _VERSION_SUFFIX_RE.sub("", key)
