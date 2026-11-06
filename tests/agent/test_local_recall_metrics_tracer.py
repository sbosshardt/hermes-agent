"""Real prefetch selection through the native, temporary session-store writer."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.turn_context import _memory_turn_start_and_prefetch
from hermes_state import SessionDB


def test_actual_prefetch_updates_only_ensured_session_and_records_no_content(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "isolated-recall-metrics"
        db.create_session(sid, "cli", model="fixture")
        manager = MagicMock()
        manager.prefetch_all.return_value = "selected remembered decision"
        manager.describe_recall.return_value = "recalled"
        agent = SimpleNamespace(
            _memory_manager=manager, _session_db=db, _session_db_created=True,
            session_id=sid, _user_turn_count=1, _emit_status=MagicMock(),
            _last_auto_recall_observation=None,
        )
        selected = _memory_turn_start_and_prefetch(agent, "What did we decide about the design?")
        assert selected == "selected remembered decision"
        manager.prefetch_all.assert_called_once_with("What did we decide about the design?", session_id=sid)
        row = db.get_session(sid)
        assert row["auto_recall_attempt_count"] == row["auto_recall_success_count"] == 1
        assert row["auto_recall_failure_count"] == 0
        assert row["auto_recall_total_latency_ms"] >= 0
        assert row["auto_recall_min_latency_ms"] == row["auto_recall_total_latency_ms"]
        assert row["auto_recall_max_latency_ms"] == row["auto_recall_total_latency_ms"]
        observation = agent._last_auto_recall_observation
        assert observation["attempted"] is observation["success"] is True
        assert observation["context_chars"] == len(selected)
        assert selected not in str(observation)
        assert db.get_session("another-session") is None
    finally:
        db.close()
