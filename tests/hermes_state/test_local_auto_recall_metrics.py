"""Opt-in local auto-recall counters on the native SessionDB writer.

These are RED contracts: the baseline has no recall columns or metric writer.
"""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Timer

import pytest

from hermes_state import SessionDB
from hermes_state_common import SCHEMA_SQL


COLS = (
    "auto_recall_attempt_count", "auto_recall_success_count", "auto_recall_failure_count",
    "auto_recall_total_latency_ms", "auto_recall_min_latency_ms", "auto_recall_max_latency_ms",
)


def _db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("ensured", "cli")
    return db


def test_default_session_has_zero_counters_and_nullable_latency(tmp_path):
    db = _db(tmp_path)
    try:
        row = db.get_session("ensured")
        assert tuple(row[name] for name in COLS) == (0, 0, 0, 0, None, None)
    finally:
        db.close()


def test_existing_session_counts_failures_successes_and_extrema(tmp_path):
    db = _db(tmp_path)
    try:
        db.update_auto_recall_metrics("ensured", attempts=1, failures=0, latency_ms=48)
        db.update_auto_recall_metrics("ensured", attempts=1, failures=1, latency_ms=12)
        assert tuple(db.get_session("ensured")[name] for name in COLS) == (2, 1, 1, 60, 12, 48)
    finally:
        db.close()


def test_invalid_counts_clamp_and_missing_latency_preserves_nullable_extrema(tmp_path):
    db = _db(tmp_path)
    try:
        db.update_auto_recall_metrics("ensured", attempts=-5, failures=7)
        row = db.get_session("ensured")
        assert tuple(row[name] for name in COLS) == (0, 0, 0, 0, None, None)
        db.update_auto_recall_metrics("ensured", attempts=2, failures=5, latency_ms=-4)
        assert tuple(db.get_session("ensured")[name] for name in COLS) == (2, 0, 2, 0, 0, 0)
    finally:
        db.close()


def test_metric_write_is_opt_in_and_does_not_ensure_or_upsert(tmp_path, monkeypatch):
    db = _db(tmp_path)
    try:
        monkeypatch.setattr(db, "_insert_session_row", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("metrics must not upsert a session")))
        db.update_auto_recall_metrics("ensured", latency_ms=4)
        assert db.get_session("ensured")["auto_recall_attempt_count"] == 1
        with pytest.raises(LookupError, match="missing session"):
            db.update_auto_recall_metrics("not-ensured", latency_ms=4)
        assert db.get_session("not-ensured") is None
    finally:
        db.close()


def test_metric_writer_does_not_wait_out_native_local_lock(tmp_path):
    db = _db(tmp_path)
    started = Event()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db._lock:
                def write():
                    started.set()
                    db.update_auto_recall_metrics("ensured", latency_ms=4)

                future = pool.submit(write)
                assert started.wait(2)
                with pytest.raises(TimeoutError):
                    future.result(timeout=2)
                assert future.done(), "a best-effort write may not wait for the transcript lock"
        assert db.get_session("ensured")["auto_recall_attempt_count"] == 0
        db.update_auto_recall_metrics("ensured", latency_ms=4)
        assert db.get_session("ensured")["auto_recall_attempt_count"] == 1
    finally:
        db.close()


def test_metric_writer_sqlite_busy_is_bounded_and_restores_busy_timeout(tmp_path):
    db = _db(tmp_path)
    holder = sqlite3.connect(db.db_path, timeout=2)
    try:
        previous = db._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
            db.update_auto_recall_metrics("ensured", latency_ms=4)
        assert time.monotonic() - started < 2
        assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == previous
        holder.rollback()
        assert db.get_session("ensured")["auto_recall_attempt_count"] == 0
        db.update_auto_recall_metrics("ensured", latency_ms=4)
        assert db.get_session("ensured")["auto_recall_attempt_count"] == 1
    finally:
        holder.rollback()
        holder.close()
        db.close()


def test_normal_transcript_writer_still_waits_for_sqlite_after_metric_write(tmp_path):
    db = _db(tmp_path)
    holder = sqlite3.connect(db.db_path, timeout=2, check_same_thread=False)
    release = None
    try:
        db.update_auto_recall_metrics("ensured", latency_ms=4)
        holder.execute("BEGIN IMMEDIATE")
        release = Timer(0.7, holder.rollback)
        release.start()
        started = time.monotonic()
        db.create_session("later", "cli")
        assert time.monotonic() - started >= 0.5
        assert db.get_session("later") is not None
    finally:
        if release is not None:
            release.join(timeout=2)
        holder.rollback()
        holder.close()
        db.close()


def test_legacy_schema_reconciles_counters_without_losing_existing_row(tmp_path):
    path = tmp_path / "legacy.db"
    # Canonical pre-feature DDL, in a disposable file; the actual schema owner reconciles it.
    legacy_sql = "\n".join(line for line in SCHEMA_SQL.splitlines()
                           if not any(column in line for column in COLS))
    conn = sqlite3.connect(path)
    try:
        conn.executescript(legacy_sql)
        conn.execute("INSERT INTO sessions(id, source, started_at) VALUES ('old', 'cli', 1)")
        conn.commit()
    finally:
        conn.close()
    db = SessionDB(path)
    try:
        assert set(COLS).issubset({r[1] for r in db._conn.execute("PRAGMA table_info(sessions)")})
        assert db.get_session("old")["source"] == "cli"
        db.update_auto_recall_metrics("old", failures=1, latency_ms=15)
        assert tuple(db.get_session("old")[name] for name in COLS) == (1, 0, 1, 15, 15, 15)
    finally:
        db.close()
