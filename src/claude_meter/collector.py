"""Incremental parsing of ClaudeCode JSONL logs into SQLite."""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from claude_meter.config import Config, default_claude_dir
from claude_meter.db import get_connection
from claude_meter.model_normalizer import normalize_model_name
from claude_meter.models import UsageRecord


def collect_files(config: Config) -> list[Path]:
    """Return all project JSONL files under the configured projects directory."""
    base = config.claude.projects_dir or default_claude_dir() / "projects"
    if not base.exists():
        return []
    return sorted(base.rglob("*.jsonl"))


def load_history_project_hints(config: Config) -> dict[str, str]:
    """Load optional display-name hints keyed by project path from history.jsonl."""
    claude_dir = default_claude_dir()
    history_path = claude_dir / "history.jsonl"
    if not history_path.exists():
        return {}
    hints: dict[str, str] = {}
    try:
        with history_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = record.get("cwd") or record.get("projectPath")
                display = record.get("display")
                if isinstance(cwd, str) and cwd and isinstance(display, str) and display:
                    hints[cwd] = display
    except OSError:
        return {}
    return hints


def _read_sync_state(conn: sqlite3.Connection, file_path: Path) -> tuple[int, int | None]:
    row = conn.execute(
        "SELECT last_size, last_line FROM sync_state WHERE file_path = ?",
        (str(file_path),),
    ).fetchone()
    if row is None:
        return 0, None
    return int(row["last_line"]), row["last_size"]


def _update_sync_state(conn: sqlite3.Connection, file_path: Path, size: int, line_no: int) -> None:
    conn.execute(
        """INSERT INTO sync_state (file_path, last_size, last_line, last_modified)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
               last_size=excluded.last_size,
               last_line=excluded.last_line,
               last_modified=excluded.last_modified""",
        (str(file_path), size, line_no, datetime.now(timezone.utc).isoformat()),
    )


def derive_project(cwd: str) -> tuple[str | None, str | None]:
    """Return (project_name, git_repository) from a working directory."""
    if not cwd:
        return None, None
    path = Path(cwd)
    project = path.name or None
    repo: str | None = None
    for parent in (path, *path.parents):
        git_config = parent / ".git" / "config"
        if git_config.exists():
            try:
                text = git_config.read_text(encoding="utf-8")
            except OSError:
                break
            m = re.search(r"url\s*=\s*(.+)", text)
            if m:
                raw_url = m.group(1).strip()
                # ssh or https -> extract owner/repo
                repo_match = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", raw_url)
                if repo_match:
                    repo = repo_match.group(1)
            break
    return project, repo


def _parse_iso_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_transcripts(config: Config) -> dict[tuple[str, str | None], tuple[str, str, datetime]]:
    """Load user/assistant message pairs keyed by (session_id, request_id)."""
    base = config.claude.transcripts_dir or default_claude_dir() / "transcripts"
    if not base.exists():
        return {}
    pairs: dict[tuple[str, str | None], tuple[str, str, datetime]] = {}
    for file_path in sorted(base.glob("*.jsonl")):
        try:
            fh = file_path.open("r", encoding="utf-8")
        except OSError:
            continue
        with fh as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_type = record.get("type")
                key = (record.get("sessionId", ""), record.get("requestId"))
                raw_content = record.get("message", {}).get("content", "")
                if isinstance(raw_content, list):
                    content = "".join(
                        block.get("text", "")
                        for block in raw_content
                        if isinstance(block, dict)
                    )
                elif isinstance(raw_content, str):
                    content = raw_content
                else:
                    content = str(raw_content)
                try:
                    ts = _parse_iso_ts(record.get("timestamp", "1970-01-01T00:00:00Z"))
                except (ValueError, TypeError):
                    ts = datetime(1970, 1, 1, tzinfo=timezone.utc)
                if msg_type == "user":
                    existing = pairs.get(key, ("", "", ts))
                    pairs[key] = (content, existing[1], ts)
                elif msg_type == "assistant":
                    existing = pairs.get(key, ("", "", ts))
                    pairs[key] = (existing[0], content, existing[2])
    return pairs


def _compute_response_time(
    session_id: str,
    request_id: str | None,
    assistant_ts: datetime,
    transcripts: dict[tuple[str, str | None], tuple[str, str, datetime]],
) -> int | None:
    """Compute response time in milliseconds from user to assistant message."""
    key = (session_id, request_id)
    entry = transcripts.get(key)
    if entry is None:
        return None
    user_ts = entry[2]
    delta = assistant_ts - user_ts
    return max(0, int(delta.total_seconds() * 1000))

def parse_incremental(config: Config) -> int:
    files = collect_files(config)
    history_hints = load_history_project_hints(config)
    transcripts = _load_transcripts(config)
    inserted = 0
    with get_connection(config.storage.db_path) as conn:
        for file_path in files:
            try:
                current_size = file_path.stat().st_size
            except OSError:
                continue
            start_line, last_size = _read_sync_state(conn, file_path)
            if last_size is not None and current_size < last_size:
                start_line = 0
            try:
                fh = file_path.open("r", encoding="utf-8")
            except OSError:
                continue
            with fh as f:
                line_no = start_line
                for line_no, line in enumerate(f, start=1):
                    if line_no <= start_line:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") != "assistant":
                        continue
                    ts_raw = record.get("timestamp")
                    if not ts_raw:
                        continue
                    try:
                        timestamp = _parse_iso_ts(ts_raw)
                    except (ValueError, TypeError):
                        continue
                    message = record.get("message")
                    if not isinstance(message, dict):
                        message = {}
                    usage = message.get("usage")
                    if not isinstance(usage, dict):
                        usage = {}
                    model = message.get("model", "unknown")
                    if not isinstance(model, str) or not model:
                        model = "unknown"
                    if normalize_model_name(model) is None:
                        # still store the raw model so users see the row
                        pass
                    cwd = record.get("cwd", "")
                    project, repo = derive_project(cwd)
                    project = history_hints.get(cwd, project)
                    rec = UsageRecord(
                        timestamp=timestamp,
                        session_id=record.get("sessionId", ""),
                        request_id=record.get("requestId") or f"missing-{file_path.name}-{line_no}",
                        project=project,
                        git_repository=repo,
                        model=model,
                        input_tokens=usage.get("input_tokens", 0) or 0,
                        output_tokens=usage.get("output_tokens", 0) or 0,
                        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
                        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0) or 0,
                        source_file=file_path,
                    )
                    # Transcript matching must use the raw requestId (which may be
                    # None) because `_load_transcripts` keys its dict the same way.
                    # The synthesized `rec.request_id` above only exists to satisfy
                    # the DB's NOT NULL/UNIQUE(session_id, request_id) constraint and
                    # would never match a transcript entry.
                    raw_request_id = record.get("requestId")
                    key = (rec.session_id, raw_request_id)
                    pair = transcripts.get(key)
                    if pair is not None:
                        rec.response_time_ms = _compute_response_time(
                            rec.session_id, raw_request_id, rec.timestamp, transcripts
                        )
                        if config.privacy.store_prompts:
                            rec.prompt_text = pair[0][: config.privacy.max_prompt_length]
                            rec.response_text = pair[1][: config.privacy.max_response_length]
                    _insert_usage(conn, rec)
                    inserted += 1
            _update_sync_state(conn, file_path, current_size, line_no)
        conn.commit()
    return inserted


def _insert_usage(conn: sqlite3.Connection, rec: UsageRecord) -> None:
    conn.execute(
        """INSERT INTO requests (
            timestamp, session_id, request_id, project, git_repository,
            model, region, input_tokens, output_tokens, cache_creation_input_tokens,
            cache_read_input_tokens, response_time_ms, cost_usd, prompt_text,
            response_text, source_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, request_id) DO UPDATE SET
            input_tokens=excluded.input_tokens,
            output_tokens=excluded.output_tokens,
            cache_creation_input_tokens=excluded.cache_creation_input_tokens,
            cache_read_input_tokens=excluded.cache_read_input_tokens,
            source_file=excluded.source_file,
            response_time_ms=excluded.response_time_ms,
            prompt_text=excluded.prompt_text,
            response_text=excluded.response_text""",
        (
            rec.timestamp.isoformat(),
            rec.session_id,
            rec.request_id,
            rec.project,
            rec.git_repository,
            rec.model,
            rec.region,
            rec.input_tokens,
            rec.output_tokens,
            rec.cache_creation_input_tokens,
            rec.cache_read_input_tokens,
            rec.response_time_ms,
            rec.cost_usd,
            rec.prompt_text,
            rec.response_text,
            str(rec.source_file),
        ),
    )
