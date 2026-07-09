from pathlib import Path

from claude_meter.db import get_connection, init_db
from claude_meter.ui.session_explorer import _list_sessions


def test_list_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "data.db"
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO requests (timestamp, session_id, request_id, model) VALUES (?, ?, ?, ?)",
        ("2026-07-08T10:00:00Z", "sess-1", "r-1", "m"),
    )
    conn.commit()
    sessions = _list_sessions(conn)
    assert len(sessions) == 1
    assert sessions.iloc[0]["session_id"] == "sess-1"
