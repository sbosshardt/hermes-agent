"""Auto-recall counter storage and declarative migration."""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Timer

import pytest

from hermes_state import SessionDB
from hermes_state_common import SCHEMA_SQL
from hermes_state_errors import DeletedWalGenerationError


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


def test_existing_session_counter_write_uses_short_patience_without_upsert(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="s1", source="cli")
        original_execute = db._execute_write
        patience = []

        def bounded_write(fn, patience_s=None, *, lock_timeout_s=None, best_effort=False):
            patience.append((patience_s, lock_timeout_s, best_effort))
            return original_execute(fn, patience_s=patience_s, lock_timeout_s=lock_timeout_s,
                                    best_effort=best_effort)

        monkeypatch.setattr(db, "_execute_write", bounded_write)
        monkeypatch.setattr(db, "_insert_session_row", lambda *args, **kwargs: (
            _ for _ in ()).throw(AssertionError("telemetry must not upsert the ensured session")))
        db.update_auto_recall_metrics("s1", failures=0, latency_ms=42)
        assert len(patience) == 1
        assert all(value is not None and 0 < value <= 0.5 for value in patience[0][:2])
        assert patience[0][2] is True
        assert db.get_session("s1")["auto_recall_attempt_count"] == 1
    finally:
        db.close()


def test_counter_write_does_not_wait_for_transcript_lock(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="s1", source="cli")
        started = Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with db._lock:
                def record():
                    started.set()
                    db.update_auto_recall_metrics("s1", latency_ms=4)
                future = executor.submit(record)
                assert started.wait(2)
                with pytest.raises(TimeoutError):
                    future.result(timeout=1.5)
                assert future.done(), "telemetry must stop waiting before the DB lock is released"
        assert db.get_session("s1")["auto_recall_attempt_count"] == 0
        db.update_auto_recall_metrics("s1", latency_ms=4)
        assert db.get_session("s1")["auto_recall_attempt_count"] == 1
    finally:
        db.close()


def test_counter_write_does_not_wait_on_sqlite_writer_lock(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    holder = sqlite3.connect(db.db_path, timeout=2)
    try:
        db.create_session(session_id="s1", source="cli")
        original_busy_timeout = db._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.update_auto_recall_metrics("s1", latency_ms=4)
        elapsed = time.monotonic() - started
        assert elapsed < 0.8, f"telemetry waited {elapsed:.3f}s for SQLite's writer lock"
        assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == original_busy_timeout
        holder.rollback()
        assert db.get_session("s1")["auto_recall_attempt_count"] == 0
        db.update_auto_recall_metrics("s1", latency_ms=4)
        assert db.get_session("s1")["auto_recall_attempt_count"] == 1
    finally:
        holder.rollback()
        holder.close()
        db.close()


def test_normal_transcript_writer_retains_busy_patience_after_telemetry(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    holder = sqlite3.connect(db.db_path, timeout=2, check_same_thread=False)
    release = None
    try:
        db.create_session(session_id="s1", source="cli")
        db.update_auto_recall_metrics("s1", latency_ms=4)
        holder.execute("BEGIN IMMEDIATE")
        release = Timer(0.7, holder.rollback)
        release.start()
        started = time.monotonic()
        db.create_session(session_id="s2", source="cli")
        elapsed = time.monotonic() - started
        assert elapsed >= 0.55, f"transcript write did not wait for SQLite ({elapsed:.3f}s)"
        assert db.get_session("s2") is not None
    finally:
        if release is not None:
            release.join(timeout=2)
        holder.rollback()
        holder.close()
        db.close()


def test_telemetry_fts_failure_does_not_attempt_repair_or_maintenance(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="s1", source="cli")
        def fail(*args, **kwargs):
            raise AssertionError("telemetry cannot run FTS repair or maintenance")
        monkeypatch.setattr(db, "_enter_fts_fail_open", fail)
        monkeypatch.setattr(db, "_try_wal_checkpoint", fail)
        monkeypatch.setattr(db, "_try_incremental_merge_fts", fail)
        db._write_count = db._CHECKPOINT_EVERY_N_WRITES - 1
        # sqlite3.Connection.execute is read-only; a real failing trigger exercises the same
        # callback-error path without replacing the connection or simulating the write itself.
        db._conn.execute("CREATE TRIGGER recall_failure BEFORE UPDATE OF auto_recall_attempt_count "
                         "ON sessions BEGIN SELECT RAISE(FAIL, 'fts5: messages_fts corrupt'); END")
        with pytest.raises(sqlite3.IntegrityError, match="fts5: messages_fts corrupt"):
            db.update_auto_recall_metrics("s1", latency_ms=4)
        assert not db._conn.in_transaction
        db._conn.execute("DROP TRIGGER recall_failure")
        db.update_auto_recall_metrics("s1", latency_ms=4)
        assert db.get_session("s1")["auto_recall_attempt_count"] == 1
    finally:
        db.close()


def test_telemetry_does_not_capture_lost_wal_generation(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="s1", source="cli")
        with monkeypatch.context() as patches:
            patches.setattr(db, "_wal_generation_was_lost", lambda: True)
            patches.setattr(db, "_capture_retired_generation", lambda *args: (
                _ for _ in ()).throw(AssertionError("telemetry cannot capture WAL")))
            with pytest.raises(DeletedWalGenerationError):
                db.update_auto_recall_metrics("s1", latency_ms=4)
            assert not db._db_wal_generation_lost  # a normal write can run the forensic path
        db.update_auto_recall_metrics("s1", latency_ms=4)
        assert db.get_session("s1")["auto_recall_attempt_count"] == 1
    finally:
        # Undo only the test's synthetic loss; a real lost generation must be captured.
        db._db_wal_generation_lost = False
        db.close()


def test_missing_session_does_not_silently_drop_recall_counters(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        with pytest.raises(LookupError, match="missing session"):
            db.update_auto_recall_metrics("not-ensured", latency_ms=12)
        assert db.get_session("not-ensured") is None
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
