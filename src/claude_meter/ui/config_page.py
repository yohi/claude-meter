"""Configuration editor page."""

import zoneinfo

import streamlit as st

from claude_meter.config import load_config, save_config

_AUTO_DETECT_LABEL = "(auto-detect)"


def _timezone_options() -> list[str]:
    """Return selectbox options: auto-detect sentinel first, then sorted IANA names."""
    return [_AUTO_DETECT_LABEL, *sorted(zoneinfo.available_timezones())]


def _timezone_to_option(name: str | None) -> str:
    """Map a stored config value (``None`` or IANA name) to a selectbox option."""
    return name if name else _AUTO_DETECT_LABEL


def _option_to_timezone(option: str) -> str | None:
    """Map a selectbox option back to a storable config value."""
    return None if option == _AUTO_DETECT_LABEL else option


def render() -> None:
    try:
        config = load_config()
    except (ValueError, OSError) as exc:
        # pydantic ValidationError (raised by Config.model_validate) subclasses ValueError,
        # so this also covers invalid config values; ValueError covers invalid YAML too.
        st.error(f"Failed to load configuration: {exc}")
        st.stop()
    st.title("Config")

    st.subheader("Current Configuration")
    st.json(config.model_dump(mode="json"))

    with st.form("config_form"):
        region = st.text_input("Claude region", value=config.claude.region)
        store_prompts = st.checkbox("Store prompts", value=config.privacy.store_prompts)
        show_prompts_in_ui = st.checkbox(
            "Show prompts in UI", value=config.privacy.show_prompts_in_ui
        )
        max_prompt_length = st.number_input(
            "Max prompt length",
            min_value=0,
            value=config.privacy.max_prompt_length,
            step=1,
        )
        host = st.text_input("UI host", value=config.ui.host)
        port = st.number_input(
            "UI port",
            min_value=1,
            max_value=65535,
            value=config.ui.port,
            step=1,
        )
        timezone_options = _timezone_options()
        timezone_option = st.selectbox(
            "UI timezone",
            options=timezone_options,
            index=timezone_options.index(_timezone_to_option(config.ui.timezone)),
            help=(
                "Used to bucket daily costs by local day. "
                f"{_AUTO_DETECT_LABEL} uses the system's local timezone."
            ),
        )
        cache_ttl_hours = st.number_input(
            "Pricing cache TTL (hours)",
            min_value=0,
            value=config.pricing.cache_ttl_hours,
            step=1,
        )
        submitted = st.form_submit_button("Save")

    if submitted:
        config.claude.region = region
        config.privacy.store_prompts = store_prompts
        config.privacy.show_prompts_in_ui = show_prompts_in_ui
        config.privacy.max_prompt_length = int(max_prompt_length)
        config.ui.host = host
        config.ui.port = int(port)
        config.ui.timezone = _option_to_timezone(timezone_option)
        config.pricing.cache_ttl_hours = int(cache_ttl_hours)
        try:
            save_config(config)
        except OSError as exc:
            st.error(f"Failed to save configuration: {exc}")
        else:
            st.success("Configuration saved.")
            st.rerun()
