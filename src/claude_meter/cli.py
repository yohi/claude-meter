"""Command-line interface for claude-meter."""

from pathlib import Path

import subprocess
import sys

import click

from claude_meter.collector import parse_incremental
from claude_meter.config import Config, load_config, resolve_config_path
from claude_meter.cost import fill_missing_costs
from claude_meter.db import init_db
from claude_meter.pricing import update_pricing
from claude_meter.watcher import watch


def _config_and_db() -> Config:
    config = load_config()
    init_db(config.storage.db_path)
    return config


@click.group()
@click.version_option(package_name="claude-meter")
def main() -> None:
    """Local ClaudeCode usage and cost analyzer."""
    pass


@main.command()
def init() -> None:
    """Create config file and SQLite database."""
    config = load_config()
    init_db(config.storage.db_path)
    click.echo(f"Initialized: {config.storage.db_path}")


@main.command()
def collect() -> None:
    """Parse ClaudeCode JSONL logs once."""
    config = _config_and_db()
    inserted = parse_incremental(config)
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


@main.command()
@click.option("--port", default=None, type=int, help="Streamlit port.")
@click.option("--host", default=None, help="Streamlit host.")
def ui(port: int | None, host: str | None) -> None:
    """Launch the Streamlit UI."""
    config = load_config()
    ui_port = port or config.ui.port
    ui_host = host or config.ui.host
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
            "--browser.gatherUsageStats",
            "false",
        ],
        check=True,
    )


@main.command(name="watch")
@click.option("--poll", default=5.0, help="Polling interval in seconds (fallback when watchdog unavailable).")
def watch_cmd(poll: float) -> None:
    """Watch ~/.claude for new JSONL data."""
    config = _config_and_db()
    click.echo(f"Watching ClaudeCode logs for changes (poll={poll}s)...")
    watch(config, poll_interval=poll)


if __name__ == "__main__":
    main()
