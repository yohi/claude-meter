import zoneinfo

from claude_meter.ui.config_page import (
    _AUTO_DETECT_LABEL,
    _get_timezone_option_index,
    _option_to_timezone,
    _timezone_options,
    _timezone_to_option,
)


def test_timezone_to_option_maps_empty_string_to_empty_string() -> None:
    """Empty string should be returned as-is (not mapped to auto-detect)."""
    assert _timezone_to_option("") == ""


def test_get_timezone_option_index_returns_valid_index() -> None:
    """Valid option should return its index."""
    options = _timezone_options()
    idx = _get_timezone_option_index(_AUTO_DETECT_LABEL, options)
    assert idx == 0
    assert options[idx] == _AUTO_DETECT_LABEL


def test_get_timezone_option_index_returns_zero_for_invalid_option() -> None:
    """Invalid option should return 0 (fallback to auto-detect)."""
    options = _timezone_options()
    idx = _get_timezone_option_index("Invalid/Nonexistent", options)
    assert idx == 0
    assert options[idx] == _AUTO_DETECT_LABEL


def test_get_timezone_option_index_handles_empty_string() -> None:
    """Empty string should return 0 (fallback to auto-detect)."""
    options = _timezone_options()
    idx = _get_timezone_option_index("", options)
    assert idx == 0


def test_timezone_options_includes_auto_detect_first() -> None:
    options = _timezone_options()
    assert options[0] == _AUTO_DETECT_LABEL
    assert "Asia/Tokyo" in options
    assert options[1:] == sorted(options[1:])


def test_timezone_to_option_maps_none_to_auto_detect() -> None:
    assert _timezone_to_option(None) == _AUTO_DETECT_LABEL


def test_timezone_to_option_passes_through_explicit_name() -> None:
    assert _timezone_to_option("Asia/Tokyo") == "Asia/Tokyo"


def test_option_to_timezone_maps_auto_detect_to_none() -> None:
    assert _option_to_timezone(_AUTO_DETECT_LABEL) is None


def test_option_to_timezone_passes_through_explicit_name() -> None:
    assert _option_to_timezone("Asia/Tokyo") == "Asia/Tokyo"


def test_timezone_options_are_all_valid_zoneinfo_names() -> None:
    for name in _timezone_options()[1:]:
        zoneinfo.ZoneInfo(name)
