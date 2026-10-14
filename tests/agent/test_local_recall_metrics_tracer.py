"""Initial vertical tracer: actual selected recall -> state metrics, no inference or live DB."""
from types import SimpleNamespace
from unittest.mock import MagicMock
from hermes_state import SessionDB
from agent.turn_context import _memory_turn_start_and_prefetch


def test_actual_prefetch_updates_ensured_session_recall_counters(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "isolated-recall-metrics"
        db.create_session(session_id=sid, source="cli", model="fixture")
        manager = MagicMock()
        manager.wait_for_session_boundary.return_value = True
        manager.prefetch_all.return_value = "selected remembered decision"
        manager.describe_recall.return_value = "recalled"
        agent = SimpleNamespace(_memory_manager=manager, _session_db=db,
                                _session_db_created=True, session_id=sid,
                                _user_turn_count=1, _emit_status=MagicMock(),
                                _last_auto_recall_observation=None)
        selected = _memory_turn_start_and_prefetch(agent, "What did we decide about the design?")
        assert selected == "selected remembered decision"
        manager.prefetch_all.assert_called_once_with("What did we decide about the design?", session_id=sid)
        row = db.get_session(sid)
        assert row is not None
        assert row.get("auto_recall_attempt_count", 0) == 1
        assert row["auto_recall_success_count"] == 1
        assert row["auto_recall_failure_count"] == 0
        assert row["auto_recall_total_latency_ms"] >= 0
        assert row["auto_recall_min_latency_ms"] == row["auto_recall_total_latency_ms"] == row["auto_recall_max_latency_ms"]
        assert agent._last_auto_recall_observation["attempted"] is True
        assert agent._last_auto_recall_observation["success"] is True
        assert agent._last_auto_recall_observation["context_chars"] == len(selected)
        assert "selected remembered decision" not in str(agent._last_auto_recall_observation)
    finally:
        db.close()
