import zoneinfo

from claude_meter.ui.config_page import (
    _AUTO_DETECT_LABEL,
    _option_to_timezone,
    _timezone_options,
    _timezone_to_option,
)


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
