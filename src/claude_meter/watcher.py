"""Filesystem watcher for live JSONL ingestion."""

import time
from typing import Any

from claude_meter.config import Config, default_claude_dir
from claude_meter.cost import fill_missing_costs


def _collect_once(config: Config) -> int:
    from claude_meter.collector import parse_incremental
    inserted = parse_incremental(config)
    if inserted:
        fill_missing_costs(config)
    return inserted


def watch(config: Config, poll_interval: float = 5.0) -> None:
    """Watch project and transcript JSONL data indefinitely."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        Observer = None  # type: ignore

    if Observer is not None:
        handler = FileSystemEventHandler()
        original_on_any_event = handler.on_any_event

        def on_event(event: Any) -> None:
            if event.src_path.endswith(".jsonl"):
                _collect_once(config)
            original_on_any_event(event)

        handler.on_any_event = on_event  # type: ignore[method-assign]
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
        try:
            while True:
                time.sleep(poll_interval)
        finally:
            observer.stop()
            observer.join()
    else:
        while True:
            _collect_once(config)
            time.sleep(poll_interval)
