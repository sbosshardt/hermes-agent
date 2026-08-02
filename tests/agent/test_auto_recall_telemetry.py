"""Regression tests for synchronous auto-recall observability."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_context import _collect_external_memory_context, _mark_auto_recall_append


def _agent(*, result="remembered fact", error=None):
    manager = MagicMock()
    if error is not None:
        manager.recall_sync_all.side_effect = error
    else:
        manager.recall_sync_all.return_value = result
    session_db = MagicMock()
    agent = SimpleNamespace(
        _memory_manager=manager,
        _memory_sync_recall=True,
        _session_db=session_db,
        _session_db_created=True,
        session_id="session-1",
        _last_auto_recall_observation=None,
    )
    return agent, manager, session_db


def test_sync_auto_recall_records_success_and_persists_metrics(monkeypatch):
    agent, manager, session_db = _agent()
    times = iter((10.0, 10.012))
    monkeypatch.setattr("agent.turn_context.time.monotonic", lambda: next(times))

    context = _collect_external_memory_context(agent, "current request")

    assert context == "remembered fact"
    manager.recall_sync_all.assert_called_once_with(
        "current request", session_id="session-1"
    )
    assert agent._last_auto_recall_observation == {
        "mode": "sync_recall",
        "attempted": True,
        "success": True,
        "latency_ms": 12,
        "context_chars": 15,
        "failure_reason": "",
    }
    session_db.update_auto_recall_metrics.assert_called_once_with(
        "session-1", attempts=1, failures=0, latency_ms=12
    )


def test_sync_auto_recall_records_failure_without_raising(monkeypatch):
    agent, _, session_db = _agent(error=TimeoutError("slow backend"))
    times = iter((20.0, 20.125))
    monkeypatch.setattr("agent.turn_context.time.monotonic", lambda: next(times))

    context = _collect_external_memory_context(agent, "current request")

    assert context == ""
    assert agent._last_auto_recall_observation["success"] is False
    assert agent._last_auto_recall_observation["failure_reason"] == "TimeoutError"
    assert agent._last_auto_recall_observation["latency_ms"] == 125
    session_db.update_auto_recall_metrics.assert_called_once_with(
        "session-1", attempts=1, failures=1, latency_ms=125
    )


def test_append_observation_is_logged_once():
    agent, _, _ = _agent()
    agent._last_auto_recall_observation = {
        "mode": "sync_recall",
        "attempted": True,
    }

    _mark_auto_recall_append(
        agent,
        "Question\n\n<memory-context>\nremembered fact\n</memory-context>",
    )
    _mark_auto_recall_append(agent, "second provider pass without memory")

    observation = agent._last_auto_recall_observation
    assert observation["append_logged"] is True
    assert observation["memory_context_appended"] is True
    assert observation["memory_context_chars"] > 0
    assert "append_failure_reason" not in observation


def test_append_observation_records_missing_block():
    agent, _, _ = _agent()
    agent._last_auto_recall_observation = {
        "mode": "sync_recall",
        "attempted": True,
    }

    _mark_auto_recall_append(agent, "Question without memory")

    observation = agent._last_auto_recall_observation
    assert observation["append_logged"] is True
    assert observation["memory_context_appended"] is False
    assert observation["append_failure_reason"] == "build_block_empty"
