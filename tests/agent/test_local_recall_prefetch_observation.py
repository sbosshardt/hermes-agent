"""Initial actual prefetch observation carries, no future transport observer imports."""
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from agent.turn_context import _memory_turn_start_and_prefetch

def _agent(*, result="remembered fact", error=None, boundary=True):
    manager = MagicMock()
    manager.wait_for_session_boundary.return_value = boundary
    manager.prefetch_all.return_value = result
    manager.prefetch_all.side_effect = error
    manager.describe_recall.return_value = "recalled"
    db = MagicMock()
    return SimpleNamespace(
        _memory_manager=manager, _session_db=db, _session_db_created=True,
        session_id="s1", _user_turn_count=1, _emit_status=MagicMock(),
        _last_auto_recall_observation=None,
    ), manager, db


def test_prefetch_success_records_latency_and_persists(monkeypatch):
    agent, manager, db = _agent()
    times = iter((10.0, 10.012))
    monkeypatch.setattr("agent.turn_context.time.monotonic", lambda: next(times))
    assert _memory_turn_start_and_prefetch(agent, "What did we decide?") == "remembered fact"
    manager.prefetch_all.assert_called_once_with("What did we decide?", session_id="s1")
    assert agent._last_auto_recall_observation == {
        "mode": "prefetch", "attempted": True, "success": True,
        "latency_ms": 12, "context_chars": 15, "failure_reason": "",
    }
    db.update_auto_recall_metrics.assert_called_once_with("s1", attempts=1, failures=0, latency_ms=12)


@pytest.mark.parametrize("result,error,reason", [
    ("", None, "empty_result"), ("", TimeoutError("slow"), "TimeoutError"),
])
def test_prefetch_failure_is_observed_without_blocking_turn(monkeypatch, result, error, reason):
    agent, _, db = _agent(result=result, error=error)
    times = iter((20.0, 20.125))
    monkeypatch.setattr("agent.turn_context.time.monotonic", lambda: next(times))
    assert _memory_turn_start_and_prefetch(agent, "What did we decide?") == ""
    assert agent._last_auto_recall_observation["failure_reason"] == reason
    db.update_auto_recall_metrics.assert_called_once_with("s1", attempts=1, failures=1, latency_ms=125)


def test_prefetch_metric_write_failure_is_reported_without_leaking_context(caplog):
    agent, _, db = _agent(result="private remembered fact")
    db.update_auto_recall_metrics.side_effect = OSError("sensitive database error: private token")
    assert _memory_turn_start_and_prefetch(agent, "What did we decide?") == "private remembered fact"
    assert "Auto-recall metrics persistence failed" in caplog.text
    assert "private token" not in caplog.text
    assert "private remembered fact" not in caplog.text
