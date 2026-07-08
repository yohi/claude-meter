"""Streamlit entrypoint for claude-meter."""

import streamlit as st

from claude_meter.ui import (
    config_page,
    model_breakdown,
    overview,
    pricing_settings,
    project_breakdown,
    session_explorer,
)

PAGE_MAP = {
    "Overview": overview,
    "Project Breakdown": project_breakdown,
    "Model Breakdown": model_breakdown,
    "Session Explorer": session_explorer,
    "Pricing Settings": pricing_settings,
    "Config": config_page,
}


def main() -> None:
    st.set_page_config(page_title="claude-meter", layout="wide")
    page = st.sidebar.radio("Navigation", list(PAGE_MAP.keys()))
    PAGE_MAP[page].render()


if __name__ == "__main__":
    main()
