from claude_meter.model_normalizer import (
    canonical_model_key,
    display_model_name,
    model_to_arn_keys,
    normalize_model_name,
)


def test_normalize_claude_code_internal_name() -> None:
    assert normalize_model_name("claude-sonnet-4-5-20260701") == "claude-sonnet-4-5-20260701"


def test_normalize_arn_is_unchanged() -> None:
    assert normalize_model_name("anthropic.claude-3-5-sonnet-20241022-v2:0") == "anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_unknown_model_returns_none() -> None:
    assert normalize_model_name("totally-unknown-model-xyz") is None


def test_display_model_name_replaces_unknown_model() -> None:
    assert display_model_name("totally-unknown-model-xyz") == "Unknown model"


def test_arn_keys_for_known_model() -> None:
    keys = model_to_arn_keys("claude-sonnet-4-5-20260701")
    assert "anthropic.claude-sonnet-4-5-20260701-v1:0" in keys


def test_normalize_current_internal_model_names() -> None:
    # 現行の ClaudeCode 内部名(fallback JSON 未登録)も既知として扱われること(乖離2回帰)。
    assert normalize_model_name("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5-20250929"
    assert normalize_model_name("claude-opus-4-5-20251101") == "claude-opus-4-5-20251101"
    assert normalize_model_name("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_normalize_region_prefixed_arn() -> None:
    assert (
        normalize_model_name("eu.anthropic.claude-haiku-4-5-20251001-v1:0")
        == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def test_normalize_non_claude_returns_none() -> None:
    assert normalize_model_name("gpt-4o") is None
    assert normalize_model_name("totally-unknown-model-xyz") is None


def test_canonical_model_key_strips_prefix_and_version() -> None:
    assert canonical_model_key("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"
    assert (
        canonical_model_key("anthropic.claude-haiku-4-5-20251001-v1:0")
        == "claude-haiku-4-5-20251001"
    )
    assert (
        canonical_model_key("eu.anthropic.claude-haiku-4-5-20251001-v1:0")
        == "claude-haiku-4-5-20251001"
    )
    assert canonical_model_key("global.anthropic.claude-sonnet-4-6") == "claude-sonnet-4-6"
