"""Command-line interface for claude-meter."""

from pathlib import Path

import subprocess
import sys
import threading

import click

from claude_meter.collector import parse_incremental
from claude_meter.config import Config, load_config, resolve_config_path, save_config
from claude_meter.cost import fill_missing_costs
from claude_meter.db import init_db
from claude_meter.pricing import update_pricing
from claude_meter.report import build_report, to_csv, to_json, to_markdown
from claude_meter.watcher import watch


def _config_and_db() -> Config:
    config = load_config()
    init_db(config.storage.db_path)
    return config

def _resolve_ui_endpoint(config: Config, port: int | None, host: str | None) -> tuple[int, str]:
    ui_port = port if port is not None else config.ui.port
    ui_host = host or config.ui.host
    return ui_port, ui_host

def _poll_explicitly_set(ctx: click.Context, param: click.Option, value: float) -> float:
    """Mark --poll as explicitly provided so we can warn without --watch."""
    if ctx.get_parameter_source(param.name) == click.core.ParameterSource.COMMANDLINE:
        ctx.ensure_object(dict)
        ctx.obj["poll_explicit"] = True
    return value


def _warn_poll_without_watch(ctx: click.Context, watch_logs: bool) -> None:
    if ctx.obj and ctx.obj.get("poll_explicit") and not watch_logs:
        click.echo(
            "Warning: --poll is ignored unless --watch is also set.", err=True
        )

def _start_watcher(config: Config, poll: float) -> threading.Thread:
    watcher_thread: threading.Thread = threading.Thread(
        target=watch, args=(config,), kwargs={"poll_interval": poll}, daemon=True
    )
    watcher_thread.start()
    click.echo(f"Watching ClaudeCode logs in background (poll={poll}s)...")
    return watcher_thread

def _launch_streamlit(ui_port: int, ui_host: str) -> None:
    """Launch the Streamlit dashboard as a subprocess."""
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(Path(__file__).resolve().parent / "ui" / "app.py"),
                "--server.port",
                str(ui_port),
                "--server.address",
                ui_host,
                "--server.showEmailPrompt",
                "false",
                "--client.toolbarMode",
                "viewer",
                "--browser.gatherUsageStats",
                "false",
            ],
            check=True,
        )
    except FileNotFoundError as exc:
        click.echo("Failed to launch Streamlit. Is it installed?", err=True)
        raise SystemExit(1) from exc
    except subprocess.CalledProcessError as exc:
        click.echo(f"Streamlit exited with code {exc.returncode}.", err=True)
        raise SystemExit(exc.returncode) from exc


@click.group()
@click.version_option(package_name="claude-meter")
def main() -> None:
    """Local ClaudeCode usage and cost analyzer."""
    pass


@main.command()
def init() -> None:
    """Create config file and SQLite database."""
    config = _config_and_db()
    config_path = resolve_config_path()
    if not config_path.exists():
        save_config(config, config_path)
    click.echo(f"Initialized: {config.storage.db_path}")


@main.command()
@click.option(
    "--reparse",
    is_flag=True,
    help="Truncate stored requests and re-ingest every JSONL file from the start.",
)
def collect(reparse: bool) -> None:
    """Parse ClaudeCode JSONL logs once."""
    config = _config_and_db()
    inserted = parse_incremental(config, reparse=reparse)
    fill_missing_costs(config)
    click.echo(f"Inserted {inserted} new records.")


@main.group()
def pricing() -> None:
    """Pricing cache and table commands."""
    pass


@pricing.command(name="update")
@click.option("--force", is_flag=True, help="Ignore cache TTL and refresh now.")
def pricing_update(force: bool) -> None:
    """Update Bedrock pricing cache."""
    config = _config_and_db()
    records = update_pricing(config, force=force)
    click.echo(f"Updated pricing for {len(records)} model/region entries.")


@main.command()
def config() -> None:
    """Print the configuration file path."""
    click.echo(resolve_config_path())


@main.command(name="report")
@click.option(
    "--days", default=None, type=int, help="Limit to the last N days (default: all time)."
)
@click.option(
    "--format",
    "output_format",
    default="markdown",
    show_default=True,
    type=click.Choice(["markdown", "csv", "json"]),
    help="Report output format.",
)
@click.option(
    "--output",
    "output_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write the report to a file instead of stdout.",
)
@click.option(
    "--actual-total",
    default=None,
    type=float,
    help="Actual AWS Bedrock total cost (USD) to compute the delta against the estimate.",
)
def report_cmd(
    days: int | None,
    output_format: str,
    output_path: str | None,
    actual_total: float | None,
) -> None:
    """Generate a cost reconciliation report (estimate vs actual Bedrock)."""
    config = _config_and_db()
    reconciliation = build_report(config, days=days, actual_total_cost=actual_total)
    if output_format == "csv":
        text = to_csv(reconciliation)
    elif output_format == "json":
        text = to_json(reconciliation)
    else:
        text = to_markdown(reconciliation)
    if output_path is not None:
        Path(output_path).write_text(text, encoding="utf-8")
        click.echo(f"Wrote {output_format} report to {output_path}")
    else:
        click.echo(text)


@main.command()
@click.option("--port", default=None, type=int, help="Streamlit port.")
@click.option("--host", default=None, help="Streamlit host.")
@click.option(
    "--watch",
    "watch_logs",
    is_flag=True,
    help="Also watch for new ClaudeCode logs in the background while the UI runs.",
)
@click.option(
    "--poll",
    default=5.0,
    show_default=True,
    type=float,
    callback=_poll_explicitly_set,
    help="Polling interval in seconds for --watch (watchdog fallback).",
)
@click.pass_context
def ui(ctx: click.Context, port: int | None, host: str | None, watch_logs: bool, poll: float) -> None:
    """Launch the Streamlit UI."""
    _warn_poll_without_watch(ctx, watch_logs)
    config = _config_and_db()
    ui_port, ui_host = _resolve_ui_endpoint(config, port, host)
    if watch_logs:
        _start_watcher(config, poll)
    _launch_streamlit(ui_port, ui_host)


@main.command()
@click.option("--port", default=None, type=int, help="Streamlit port.")
@click.option("--host", default=None, help="Streamlit host.")
@click.option(
    "--poll",
    default=5.0,
    show_default=True,
    type=float,
    help="Polling interval in seconds for the background watcher (watchdog fallback).",
)
def start(port: int | None, host: str | None, poll: float) -> None:
    """Initialize on first run, then launch the UI with background log watching."""
    config_path = resolve_config_path()
    first_launch = not config_path.exists()
    config = _config_and_db()
    if first_launch:
        save_config(config, config_path)
        click.echo(f"Initialized: {config.storage.db_path}")
        inserted = parse_incremental(config)
        fill_missing_costs(config)
        click.echo(f"Inserted {inserted} new records.")
    ui_port, ui_host = _resolve_ui_endpoint(config, port, host)
    _start_watcher(config, poll)
    _launch_streamlit(ui_port, ui_host)


@main.command(name="watch")
@click.option(
    "--poll", default=5.0, help="Polling interval in seconds (fallback when watchdog unavailable)."
)
def watch_cmd(poll: float) -> None:
    """Watch ~/.claude for new JSONL data."""
    config = _config_and_db()
    click.echo(f"Watching ClaudeCode logs for changes (poll={poll}s)...")
    watch(config, poll_interval=poll)


if __name__ == "__main__":
    main()
