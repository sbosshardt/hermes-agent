"""Real temporary DB refusal and metric edge controls; no live state or source edits."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from hermes_state import SessionDB
from hermes_state_errors import StateDbReplacedError, DeletedWalGenerationError, StateDbCorruptError
from agent.turn_context import _memory_turn_start_and_prefetch


@pytest.mark.parametrize("guard", ["closed", "read-only", "file-replaced", "lost-wal", "local-corrupt"])
def test_metric_writer_refuses_unavailable_generation_without_recovery(tmp_path, monkeypatch, guard):
    db = SessionDB(tmp_path / "state.db")
    original = db._conn
    assert original is not None
    try:
        db.create_session(session_id="ensured", source="cli")
        original_busy = original.execute("PRAGMA busy_timeout").fetchone()[0]
        write_count = db._write_count
        forbidden = []

        def refuse_recovery(*args, **kwargs):
            forbidden.append(True)
            raise AssertionError("telemetry must not repair, maintain, reopen, or capture")

        for name in ("_reopen_after_close_locked", "_enter_fts_fail_open", "_try_wal_checkpoint",
                     "_try_incremental_merge_fts", "_capture_retired_generation", "_halt_db_corrupt"):
            monkeypatch.setattr(db, name, refuse_recovery)
        with monkeypatch.context() as changes:
            if guard == "closed":
                changes.setattr(db, "_conn", None)
            elif guard == "read-only":
                changes.setattr(db, "read_only", True)
            elif guard == "file-replaced":
                changes.setattr(db, "_db_file_was_replaced", lambda: True)
            elif guard == "lost-wal":
                changes.setattr(db, "_wal_generation_was_lost", lambda: True)
            else:
                changes.setattr(db, "_db_corrupt", True)
            expected = {"closed": TimeoutError, "read-only": TimeoutError,
                        "file-replaced": StateDbReplacedError, "lost-wal": DeletedWalGenerationError,
                        "local-corrupt": StateDbCorruptError}[guard]
            with pytest.raises(expected):
                db.update_auto_recall_metrics("ensured", latency_ms=4)
        assert forbidden == []
        assert db._conn is original
        assert db._write_count == write_count
        assert not original.in_transaction
        assert original.execute("PRAGMA busy_timeout").fetchone()[0] == original_busy
        assert db.get_session("ensured")["auto_recall_attempt_count"] == 0
        db.update_auto_recall_metrics("ensured", latency_ms=4)
        assert db.get_session("ensured")["auto_recall_attempt_count"] == 1
    finally:
        db.close()


def test_metric_clamping_and_unsupplied_latency_preserve_nullable_extrema(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session(session_id="ensured", source="cli")
        db.update_auto_recall_metrics("ensured", attempts=-5, failures=7)
        row = db.get_session("ensured")
        assert (row["auto_recall_attempt_count"], row["auto_recall_success_count"], row["auto_recall_failure_count"]) == (0, 0, 0)
        assert row["auto_recall_min_latency_ms"] is row["auto_recall_max_latency_ms"] is None
        db.update_auto_recall_metrics("ensured", attempts=2, failures=5, latency_ms=-4)
        row = db.get_session("ensured")
        assert (row["auto_recall_attempt_count"], row["auto_recall_success_count"], row["auto_recall_failure_count"]) == (2, 0, 2)
        assert row["auto_recall_total_latency_ms"] == row["auto_recall_min_latency_ms"] == row["auto_recall_max_latency_ms"] == 0
    finally:
        db.close()


@pytest.mark.parametrize("skip", ["no-manager", "boundary", "trivial"])
def test_skipped_prefetch_clears_stale_observation_and_does_not_record_attempt(skip):
    manager = MagicMock()
    manager.wait_for_session_boundary.return_value = skip != "boundary"
    db = MagicMock()
    agent = SimpleNamespace(_memory_manager=None if skip == "no-manager" else manager,
                            _session_db=db, session_id="s", _user_turn_count=1,
                            _emit_status=MagicMock(), _last_auto_recall_observation={"attempted": True},
                            _auto_recall_context="stale recalled secret")
    prompt = "ok" if skip == "trivial" else "What was the agreed design?"
    assert _memory_turn_start_and_prefetch(agent, prompt) == ""
    manager.prefetch_all.assert_not_called()
    db.update_auto_recall_metrics.assert_not_called()
    assert agent._last_auto_recall_observation is None
    assert agent._auto_recall_context == ""


@pytest.mark.parametrize("selected", [{"invalid": "memory"}, ["invalid"], "   "])
def test_invalid_or_empty_selection_is_not_observed_as_success(selected):
    manager = MagicMock()
    manager.wait_for_session_boundary.return_value = True
    manager.prefetch_all.return_value = selected
    db = MagicMock()
    agent = SimpleNamespace(_memory_manager=manager, _session_db=db,
                            session_id="s", _user_turn_count=1, _emit_status=MagicMock())
    result = _memory_turn_start_and_prefetch(agent, "What was the agreed design?")
    assert result == (selected if isinstance(selected, str) else "")
    observation = agent._last_auto_recall_observation
    assert observation["attempted"] is True and observation["success"] is False
    assert observation["failure_reason"] == "empty_result"
    assert observation["latency_ms"] >= 0
    assert agent._auto_recall_context == ""
    assert db.update_auto_recall_metrics.call_args.kwargs["failures"] == 1
