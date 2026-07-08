from datetime import datetime, timezone
from pathlib import Path

from claude_meter.db import get_connection, init_db
from claude_meter.ui.overview import _summary_for_period


def test_summary_for_period_aggregates(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO requests (timestamp, session_id, request_id, model, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), "s", "r", "m", 100, 50, 0.123),
    )
    conn.commit()
    summary = _summary_for_period(conn, "2026-07-01", "2026-07-31")
    assert summary["total_cost"] == 0.123
    assert summary["total_input_tokens"] == 100
    assert summary["total_output_tokens"] == 50
