def test_ui_pages_importable() -> None:
    from claude_meter.ui import app
    from claude_meter.ui import overview, project_breakdown, model_breakdown  # noqa: F401
    from claude_meter.ui import session_explorer, pricing_settings, config_page  # noqa: F401
    assert hasattr(app, "PAGE_MAP")
