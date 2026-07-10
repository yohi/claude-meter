"""Incremental parsing of ClaudeCode JSONL logs into SQLite.

Prompt/response bodies and response times are derived entirely from the project
logs (``~/.claude/projects/*/*.jsonl``). Each ``assistant`` record is one billing
row; its prompt text is the nearest human ``user`` utterance found by walking the
``parentUuid`` chain, and its response text is the concatenation of the record's
own ``text`` content blocks.
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_meter.config import Config, default_claude_dir
from claude_meter.db import get_connection
from claude_meter.models import UsageRecord

# Walking parentUuid back to the human utterance can span very long agentic
# chains (tool_use/tool_result loops). Real transcripts have been observed to
# need >140 hops, and a ceiling of 20 fails to resolve the majority of them.
# Keep the limit large so the walk effectively always reaches the human
# utterance, while still guarding against pathological or cyclic files.
_MAX_PARENT_HOPS = 10000


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


def _try_parse_ts(ts_raw: object) -> datetime | None:
    """Best-effort ISO-8601 parse; returns None for missing/invalid values."""
    if not isinstance(ts_raw, str) or not ts_raw:
        return None
    try:
        return _parse_iso_ts(ts_raw)
    except (ValueError, TypeError):
        return None


def _extract_text_blocks(content: object) -> str:
    """Concatenate the ``text`` of ``type == "text"`` blocks in a content value.

    ``thinking`` and ``tool_use`` blocks are ignored. A bare string content is
    returned verbatim (human ``user`` records store their prompt this way).
    """
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if isinstance(content, str):
        return content
    return ""


def _is_human_user(record: dict[str, Any]) -> bool:
    """True when a ``user`` record is a human utterance rather than a tool result.

    Human prompts arrive as a plain string (or a block array containing text);
    tool results arrive as a block array composed solely of ``tool_result``
    blocks.
    """
    if record.get("type") != "user":
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        types = [block.get("type") for block in content if isinstance(block, dict)]
        if not types:
            return False
        if all(t == "tool_result" for t in types):
            return False
        return True
    return False


def _human_user_text(record: dict[str, Any]) -> str:
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    return _extract_text_blocks(message.get("content"))


def _compute_response_time(input_ts: datetime, assistant_ts: datetime) -> int:
    """Milliseconds from the triggering input record to the assistant reply.

    Clamped to ``>= 0`` so out-of-order timestamps never yield a negative value.
    """
    return max(0, int((assistant_ts - input_ts).total_seconds() * 1000))


def _resolve_prompt_text(
    record: dict[str, Any],
    by_uuid: dict[str, dict[str, Any]],
    fallback_text: str | None,
    max_length: int,
) -> str | None:
    """Walk ``parentUuid`` links to the nearest human utterance and return its text.

    ``by_uuid`` only holds records read in the current batch. When the walk
    reaches a parent that lives in an earlier batch (absent from ``by_uuid``),
    fall back to the carried-over ``fallback_text`` (already truncated). Returns
    None when the chain reaches the file root without a human utterance.
    """
    seen: set[str] = set()
    current = record
    for _ in range(_MAX_PARENT_HOPS):
        parent_uuid = current.get("parentUuid")
        if not isinstance(parent_uuid, str) or not parent_uuid:
            return None
        if parent_uuid in seen:
            return None
        seen.add(parent_uuid)
        parent = by_uuid.get(parent_uuid)
        if parent is None:
            return fallback_text
        if _is_human_user(parent):
            return _human_user_text(parent)[:max_length]
        current = parent
    return None


@dataclass
class _FileSyncState:
    """Per-file ingestion progress plus batch-boundary carry-over context."""

    start_line: int
    last_size: int | None
    pending_prompt_text: str | None
    last_input_ts: datetime | None


def _read_sync_state(conn: sqlite3.Connection, file_path: Path) -> _FileSyncState:
    row = conn.execute(
        """SELECT last_size, last_line, pending_prompt_text, last_input_ts
           FROM sync_state WHERE file_path = ?""",
        (str(file_path),),
    ).fetchone()
    if row is None:
        return _FileSyncState(0, None, None, None)
    return _FileSyncState(
        start_line=int(row["last_line"] or 0),
        last_size=row["last_size"],
        pending_prompt_text=row["pending_prompt_text"],
        last_input_ts=_try_parse_ts(row["last_input_ts"]),
    )


def _update_sync_state(
    conn: sqlite3.Connection,
    file_path: Path,
    size: int,
    line_no: int,
    pending_prompt_text: str | None,
    last_input_ts: datetime | None,
) -> None:
    conn.execute(
        """INSERT INTO sync_state (
               file_path, last_size, last_line, last_modified,
               pending_prompt_text, last_input_ts
           )
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
               last_size=excluded.last_size,
               last_line=excluded.last_line,
               last_modified=excluded.last_modified,
               pending_prompt_text=excluded.pending_prompt_text,
               last_input_ts=excluded.last_input_ts""",
        (
            str(file_path),
            size,
            line_no,
            datetime.now(timezone.utc).isoformat(),
            pending_prompt_text,
            last_input_ts.isoformat() if last_input_ts is not None else None,
        ),
    )


def parse_incremental(config: Config, *, reparse: bool = False) -> int:
    files = collect_files(config)
    history_hints = load_history_project_hints(config)
    store_prompts = config.privacy.store_prompts
    max_prompt = config.privacy.max_prompt_length
    max_response = config.privacy.max_response_length
    inserted = 0
    with get_connection(config.storage.db_path) as conn:
        if reparse:
            # Full rebuild: drop all rows and per-file progress so every file is
            # re-read from line 0. The caller's fill_missing_costs() then
            # recomputes cost/region for the freshly inserted rows.
            conn.execute("DELETE FROM requests")
            conn.execute("DELETE FROM sync_state")
            conn.commit()
        for file_path in files:
            try:
                current_size = file_path.stat().st_size
            except OSError:
                continue
            state = _read_sync_state(conn, file_path)
            start_line = state.start_line
            pending_prompt_text = state.pending_prompt_text
            last_input_ts = state.last_input_ts
            if state.last_size is not None and current_size < state.last_size:
                # File shrank (truncated/rotated): restart and drop stale context.
                start_line = 0
                pending_prompt_text = None
                last_input_ts = None
            try:
                fh = file_path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            by_uuid: dict[str, dict[str, Any]] = {}
            with fh as f:
                line_no = start_line
                for line_no, raw_line in enumerate(f, start=1):
                    if line_no <= start_line:
                        continue
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    uuid = record.get("uuid")
                    if isinstance(uuid, str) and uuid:
                        by_uuid[uuid] = record
                    record_type = record.get("type")
                    timestamp = _try_parse_ts(record.get("timestamp"))
                    if record_type == "user":
                        # Every user record (human prompt or tool result) is the
                        # input the next assistant turn responds to.
                        if timestamp is not None:
                            last_input_ts = timestamp
                        if _is_human_user(record):
                            if store_prompts:
                                pending_prompt_text = _human_user_text(record)[:max_prompt]
                            else:
                                pending_prompt_text = None
                        continue
                    if record_type != "assistant":
                        continue
                    if timestamp is None:
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
                    cwd = record.get("cwd", "")
                    project, repo = derive_project(cwd)
                    project = history_hints.get(cwd, project)
                    # response_time_ms depends only on timestamps, so it is
                    # computed regardless of the store_prompts privacy setting.
                    response_time_ms = (
                        _compute_response_time(last_input_ts, timestamp)
                        if last_input_ts is not None
                        else None
                    )
                    prompt_text: str | None = None
                    response_text: str | None = None
                    if store_prompts:
                        prompt_text = _resolve_prompt_text(
                            record, by_uuid, pending_prompt_text, max_prompt
                        )
                        response_text = _extract_text_blocks(message.get("content"))[:max_response]
                    if isinstance(uuid, str) and uuid:
                        request_id = uuid
                    else:
                        request_id = f"missing-{file_path.name}-{line_no}"
                    rec = UsageRecord(
                        timestamp=timestamp,
                        session_id=record.get("sessionId", ""),
                        request_id=request_id,
                        project=project,
                        git_repository=repo,
                        model=model,
                        region=config.claude.region,
                        input_tokens=usage.get("input_tokens", 0) or 0,
                        output_tokens=usage.get("output_tokens", 0) or 0,
                        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0)
                        or 0,
                        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0) or 0,
                        response_time_ms=response_time_ms,
                        prompt_text=prompt_text,
                        response_text=response_text,
                        source_file=file_path,
                    )
                    if _insert_usage(conn, rec):
                        inserted += 1
            _update_sync_state(
                conn,
                file_path,
                current_size,
                line_no,
                pending_prompt_text,
                last_input_ts,
            )
            conn.commit()
    return inserted


def _insert_usage(conn: sqlite3.Connection, rec: UsageRecord) -> bool:
    existing = conn.execute(
        "SELECT 1 FROM requests WHERE session_id = ? AND request_id = ?",
        (rec.session_id, rec.request_id),
    ).fetchone()
    conn.execute(
        """INSERT INTO requests (
            timestamp, session_id, request_id, project, git_repository,
            model, region, input_tokens, output_tokens, cache_creation_input_tokens,
            cache_read_input_tokens, response_time_ms, cost_usd, prompt_text,
            response_text, source_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, request_id) DO UPDATE SET
            timestamp=excluded.timestamp,
            project=excluded.project,
            git_repository=excluded.git_repository,
            model=excluded.model,
            region=excluded.region,
            input_tokens=excluded.input_tokens,
            output_tokens=excluded.output_tokens,
            cache_creation_input_tokens=excluded.cache_creation_input_tokens,
            cache_read_input_tokens=excluded.cache_read_input_tokens,
            source_file=excluded.source_file,
            response_time_ms=excluded.response_time_ms,
            cost_usd=excluded.cost_usd,
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
    return existing is None
