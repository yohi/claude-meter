"""Configuration editor page."""

import streamlit as st

from claude_meter.config import load_config, save_config


def render() -> None:
    config = load_config()
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
        config.pricing.cache_ttl_hours = int(cache_ttl_hours)
        save_config(config)
        st.success("Configuration saved.")
