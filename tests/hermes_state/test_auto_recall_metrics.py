"""Auto-recall counter storage and declarative migration."""
import sqlite3

from hermes_state import SessionDB
from hermes_state_common import SCHEMA_SQL


RECALL_COLS = (
    "auto_recall_attempt_count", "auto_recall_success_count", "auto_recall_failure_count",
    "auto_recall_total_latency_ms", "auto_recall_min_latency_ms", "auto_recall_max_latency_ms",
)


def test_counter_updates_and_latency_extrema(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="s1", source="cli")
        db.update_auto_recall_metrics("s1", attempts=1, failures=0, latency_ms=48000)
        db.update_auto_recall_metrics("s1", attempts=1, failures=1, latency_ms=12000)
        row = db.get_session("s1")
        assert tuple(row[c] for c in RECALL_COLS) == (2, 1, 1, 60000, 12000, 48000)
    finally:
        db.close()


def test_reopens_and_reconciles_legacy_sessions_columns(tmp_path):
    path = tmp_path / "legacy.db"
    # Snapshot the pre-telemetry schema in an isolated file, never the live state DB.
    sql = "\n".join(line for line in SCHEMA_SQL.splitlines()
                    if not any(col in line for col in RECALL_COLS))
    conn = sqlite3.connect(path)
    conn.executescript(sql)
    conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('old', 'cli', 1)")
    conn.commit()
    conn.close()
    db = SessionDB(db_path=path)
    try:
        assert set(RECALL_COLS).issubset({r[1] for r in db._conn.execute("PRAGMA table_info(sessions)")})
        assert db.get_session("old")["source"] == "cli"
        db.update_auto_recall_metrics("old", failures=1, latency_ms=15)
        assert db.get_session("old")["auto_recall_failure_count"] == 1
    finally:
        db.close()
