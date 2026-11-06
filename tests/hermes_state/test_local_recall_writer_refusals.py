"""Refusal boundaries for best-effort recall metrics on a real, isolated SessionDB."""
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_errors import DeletedWalGenerationError, StateDbCorruptError, StateDbReplacedError


@pytest.mark.parametrize("guard", ["closed", "read_only", "replaced", "lost_wal", "corrupt"])
def test_counter_refuses_bad_handle_without_recovery_or_generation_mutation(tmp_path, monkeypatch, guard):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("ensured", "cli")
        conn = db._conn
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        count = db._write_count
        generation = db._db_file_application_id
        touched = []

        def forbidden(*args, **kwargs):
            touched.append(True)
            raise AssertionError("metric writer cannot reopen, repair or capture a generation")

        for name in ("_reopen_after_close_locked", "_enter_fts_fail_open",
                     "_try_wal_checkpoint", "_try_incremental_merge_fts",
                     "_capture_retired_generation", "_halt_db_corrupt"):
            monkeypatch.setattr(db, name, forbidden)
        with monkeypatch.context() as changed:
            if guard == "closed":
                changed.setattr(db, "_conn", None)
            elif guard == "read_only":
                changed.setattr(db, "read_only", True)
            elif guard == "replaced":
                changed.setattr(db, "_db_file_was_replaced", lambda: True)
            elif guard == "lost_wal":
                changed.setattr(db, "_wal_generation_was_lost", lambda: True)
            else:
                changed.setattr(db, "_db_corrupt", True)
            expected = {
                "closed": (TimeoutError, sqlite3.Error),
                "read_only": (TimeoutError, sqlite3.Error),
                "replaced": StateDbReplacedError,
                "lost_wal": DeletedWalGenerationError,
                "corrupt": StateDbCorruptError,
            }[guard]
            with pytest.raises(expected):
                db.update_auto_recall_metrics("ensured", latency_ms=4)
        assert not touched
        assert db._conn is conn
        assert db._write_count == count
        assert db._db_file_application_id == generation
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == busy
        assert not conn.in_transaction
        assert db.get_session("ensured")["auto_recall_attempt_count"] == 0
    finally:
        # Synthetic guard state is not a real lost WAL generation; cleanup remains native.
        db._db_replaced = False
        db._db_wal_generation_lost = False
        db._db_corrupt = False
        db.close()


def test_metric_trigger_failure_never_enters_fts_repair_or_periodic_maintenance(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("ensured", "cli")
        def forbidden(*args, **kwargs):
            raise AssertionError("metrics cannot repair FTS or run maintenance")
        for name in ("_enter_fts_fail_open", "_try_wal_checkpoint", "_try_incremental_merge_fts"):
            monkeypatch.setattr(db, name, forbidden)
        db._write_count = db._CHECKPOINT_EVERY_N_WRITES - 1
        db._conn.execute("CREATE TRIGGER metric_failure BEFORE UPDATE OF auto_recall_attempt_count "
                         "ON sessions BEGIN SELECT RAISE(FAIL, 'fts5: messages_fts corrupt'); END")
        with pytest.raises(sqlite3.IntegrityError, match="fts5: messages_fts corrupt"):
            db.update_auto_recall_metrics("ensured", latency_ms=4)
        assert not db._conn.in_transaction
        assert db.get_session("ensured")["auto_recall_attempt_count"] == 0
    finally:
        db.close()
