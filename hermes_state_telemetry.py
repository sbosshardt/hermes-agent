"""Opt-in, bounded session telemetry writer; never changes transcript write policy."""

from contextlib import contextmanager
import sqlite3

from hermes_state_errors import (
    _DELETED_WAL_GENERATION_MSG, _STATE_DB_REPLACED_MSG,
    DeletedWalGenerationError, StateDbCorruptError, StateDbReplacedError,
)
from hermes_state_health import STORAGE_CORRUPT, storage_state

_LOCAL_LOCK_TIMEOUT_S = 0.05
_SQLITE_BUSY_TIMEOUT_MS = 100


@contextmanager
def _bounded_telemetry_lock(db):
    """Own the *current thread's* native connection lock through settlement.

    Optional counters may lose a sample under contention, but must neither stall a
    transcript nor borrow its retry, repair, reopen or WAL-capture policy.
    """
    if not db._lock.acquire(timeout=_LOCAL_LOCK_TIMEOUT_S):
        raise TimeoutError("optional session telemetry lock busy")
    try:
        yield
    finally:
        db._lock.release()


def _nonnegative_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def update_auto_recall_metrics(db, session_id, *, attempts=0, failures=0, latency_ms=None):
    """UPDATE an existing row only. No session creation or ordinary write side effects."""
    if isinstance(attempts, (int, float)) and attempts < 0:
        return
    count = _nonnegative_int(attempts)
    failed = min(count, _nonnegative_int(failures))
    if not count:
        count = max(1, _nonnegative_int(failures), int(latency_ms is not None))
        failed = min(count, _nonnegative_int(failures))
    if not count:
        return
    latency = None if latency_ms is None else _nonnegative_int(latency_ms)
    with _bounded_telemetry_lock(db):
        if db.read_only or db._conn is None:
            raise sqlite3.ProgrammingError("optional session telemetry requires an open writable handle")
        if db._db_corrupt or storage_state(db.db_path) == STORAGE_CORRUPT:
            raise StateDbCorruptError("optional session telemetry refused on corrupt handle")
        if db._db_replaced or db._db_file_was_replaced():
            raise StateDbReplacedError(_STATE_DB_REPLACED_MSG)
        if db._db_wal_generation_lost or db._wal_generation_was_lost():
            raise DeletedWalGenerationError(_DELETED_WAL_GENERATION_MSG)
        conn = db._conn
        old_busy_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        try:
            conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            conn.execute("BEGIN IMMEDIATE")
            try:
                changed = conn.execute(
                    """UPDATE sessions SET
                       auto_recall_attempt_count = auto_recall_attempt_count + ?,
                       auto_recall_success_count = auto_recall_success_count + ?,
                       auto_recall_failure_count = auto_recall_failure_count + ?,
                       auto_recall_total_latency_ms = auto_recall_total_latency_ms + COALESCE(?, 0),
                       auto_recall_min_latency_ms = CASE WHEN ? IS NULL THEN auto_recall_min_latency_ms
                           WHEN auto_recall_min_latency_ms IS NULL THEN ?
                           ELSE MIN(auto_recall_min_latency_ms, ?) END,
                       auto_recall_max_latency_ms = CASE WHEN ? IS NULL THEN auto_recall_max_latency_ms
                           WHEN auto_recall_max_latency_ms IS NULL THEN ?
                           ELSE MAX(auto_recall_max_latency_ms, ?) END
                       WHERE id = ?""",
                    (count, count - failed, failed, latency, latency, latency, latency,
                     latency, latency, latency, session_id),
                ).rowcount
                if changed != 1:
                    raise LookupError("missing session for optional telemetry")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.execute(f"PRAGMA busy_timeout={int(old_busy_ms)}")
