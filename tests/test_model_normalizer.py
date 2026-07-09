from claude_meter.model_normalizer import model_to_arn_keys, normalize_model_name


def test_normalize_claude_code_internal_name() -> None:
    assert normalize_model_name("claude-sonnet-4-5-20260701") == "claude-sonnet-4-5-20260701"


def test_normalize_arn_is_unchanged() -> None:
    assert normalize_model_name("anthropic.claude-3-5-sonnet-20241022-v2:0") == "anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_unknown_model_returns_none() -> None:
    assert normalize_model_name("totally-unknown-model-xyz") is None


def test_arn_keys_for_known_model() -> None:
    keys = model_to_arn_keys("claude-sonnet-4-5-20260701")
    assert "anthropic.claude-sonnet-4-5-20260701-v1:0" in keys
