"""Observed prefetch is the real turn-start selection, not a provider-send inference."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_context import _memory_turn_start_and_prefetch


def make_agent(*, result="selected private fact", error=None, manager=True):
    memory = MagicMock() if manager else None
    if memory is not None:
        memory.prefetch_all.return_value = result
        memory.prefetch_all.side_effect = error
        memory.describe_recall.return_value = "recalled"
    db = MagicMock()
    agent = SimpleNamespace(
        _memory_manager=memory, _session_db=db, _session_db_created=True,
        session_id="isolated", _user_turn_count=1, _emit_status=MagicMock(),
        _last_auto_recall_observation=None,
    )
    return agent, memory, db


def test_success_is_selected_and_records_one_attempt_with_latency(monkeypatch):
    agent, memory, db = make_agent()
    clock = iter((10.0, 10.012))
    monkeypatch.setattr("agent.turn_context.time.monotonic", lambda: next(clock))
    assert _memory_turn_start_and_prefetch(agent, "What did we decide?") == "selected private fact"
    memory.prefetch_all.assert_called_once_with("What did we decide?", session_id="isolated")
    assert agent._last_auto_recall_observation == {
        "mode": "prefetch", "attempted": True, "success": True, "latency_ms": 12,
        "context_chars": len("selected private fact"), "failure_reason": "",
    }
    assert "selected private fact" not in str(agent._last_auto_recall_observation)
    db.update_auto_recall_metrics.assert_called_once_with(
        "isolated", attempts=1, failures=0, latency_ms=12)


@pytest.mark.parametrize("result,error,reason", [
    ("", None, "empty_result"), ("   ", None, "empty_result"),
    ({"unexpected": "private"}, None, "empty_result"),
    ("", TimeoutError("private provider token"), "TimeoutError"),
])
def test_empty_invalid_or_failed_selection_is_observed_without_blocking_turn(
    monkeypatch, result, error, reason,
):
    agent, _, db = make_agent(result=result, error=error)
    clock = iter((20.0, 20.125))
    monkeypatch.setattr("agent.turn_context.time.monotonic", lambda: next(clock))
    assert _memory_turn_start_and_prefetch(agent, "What did we decide?") == ""
    observation = agent._last_auto_recall_observation
    assert observation["attempted"] is True
    assert observation["success"] is False
    assert observation["failure_reason"] == reason
    assert observation["latency_ms"] == 125
    db.update_auto_recall_metrics.assert_called_once_with(
        "isolated", attempts=1, failures=1, latency_ms=125)


@pytest.mark.parametrize("mode", ["no_manager", "boundary", "trivial", "image_only"])
def test_skipped_prefetch_clears_previous_observation(mode):
    agent, memory, db = make_agent(manager=mode != "no_manager")
    if mode == "boundary":
        # The real manager exposes this C04 fence; a failed rebind must not prefetch.
        assert memory is not None
        memory.wait_for_session_boundary.return_value = False
    agent._last_auto_recall_observation = {"attempted": True, "success": True}
    agent._auto_recall_context = "stale private context"
    query = {"trivial": "ok", "image_only": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}.get(
        mode, "What did we decide?")
    assert _memory_turn_start_and_prefetch(agent, query) == ""
    if memory is not None:
        memory.prefetch_all.assert_not_called()
        if mode == "boundary":
            memory.wait_for_session_boundary.assert_called_once_with()
    db.update_auto_recall_metrics.assert_not_called()
    assert agent._last_auto_recall_observation is None
    assert agent._auto_recall_context == ""


def test_persistence_failure_is_best_effort_and_does_not_log_private_context(caplog):
    agent, _, db = make_agent()
    db.update_auto_recall_metrics.side_effect = OSError("private database token")
    assert _memory_turn_start_and_prefetch(agent, "What did we decide?") == "selected private fact"
    assert agent._last_auto_recall_observation["success"] is True
    assert "Auto-recall metrics persistence failed" in caplog.text
    assert "private database token" not in caplog.text
    assert "selected private fact" not in caplog.text


def test_uncreated_session_db_is_not_ensured_to_store_optional_metrics():
    agent, _, db = make_agent()
    agent._session_db_created = False
    assert _memory_turn_start_and_prefetch(agent, "What did we decide?") == "selected private fact"
    assert agent._last_auto_recall_observation["success"] is True
    db.ensure_session.assert_not_called()
    db.create_session.assert_not_called()
    db.update_auto_recall_metrics.assert_not_called()
