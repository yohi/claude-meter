"""Filesystem watcher for live JSONL ingestion."""

import logging
import time
from typing import Any

from claude_meter.config import Config, default_claude_dir
from claude_meter.cost import fill_missing_costs

logger = logging.getLogger(__name__)


def _collect_once(config: Config) -> int:
    from claude_meter.collector import parse_incremental

    inserted = parse_incremental(config)
    if inserted:
        fill_missing_costs(config)
    return inserted


def _safe_collect_once(config: Config) -> int:
    try:
        return _collect_once(config)
    except Exception:
        logger.exception("Failed to collect ClaudeCode logs")
        return 0


def watch(config: Config, poll_interval: float = 5.0) -> None:
    """Watch project and transcript JSONL data indefinitely."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        Observer = None  # type: ignore

    if Observer is not None:
        class _JsonlEventHandler(FileSystemEventHandler):
            def on_any_event(self, event: Any) -> None:
                paths = (event.src_path, getattr(event, "dest_path", ""))
                if any(path.endswith(".jsonl") for path in paths):
                    _safe_collect_once(config)
                super().on_any_event(event)

        handler = _JsonlEventHandler()
        observer = Observer()
        claude_dir = default_claude_dir()
        watch_dirs = {
            config.claude.projects_dir or claude_dir / "projects",
            config.claude.transcripts_dir or claude_dir / "transcripts",
        }
        for watch_dir in watch_dirs:
            watch_dir.mkdir(parents=True, exist_ok=True)
            observer.schedule(handler, str(watch_dir), recursive=True)
        observer.start()
        _safe_collect_once(config)
        try:
            while True:
                time.sleep(poll_interval)
        finally:
            observer.stop()
            observer.join()
    else:
        while True:
            _safe_collect_once(config)
            time.sleep(poll_interval)
