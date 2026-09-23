"""Auto-recall telemetry across turn setup, wire composition and Codex transport."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_context import _memory_turn_start_and_prefetch, _mark_auto_recall_append


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


@pytest.mark.parametrize("query,boundary", [("hello", True), ("What did we decide?", False)])
def test_no_attempt_for_trivial_or_unadmitted_turn(query, boundary):
    agent, manager, db = _agent(boundary=boundary)
    agent._last_auto_recall_observation = {"attempted": True, "append_logged": True}
    agent._auto_recall_context = "prior turn"
    assert _memory_turn_start_and_prefetch(agent, query) == ""
    assert agent._last_auto_recall_observation is None
    assert agent._auto_recall_context == ""
    manager.prefetch_all.assert_not_called()
    db.update_auto_recall_metrics.assert_not_called()


def test_append_observation_is_once_and_uses_actual_recall_block():
    from agent.memory_manager import build_memory_context_block
    agent, _, _ = _agent()
    agent._last_auto_recall_observation = {"attempted": True}
    agent._auto_recall_context = "remembered fact"
    sent = "Question\n\n" + build_memory_context_block("remembered fact")
    _mark_auto_recall_append(agent, sent)
    _mark_auto_recall_append(agent, "other content")
    assert agent._last_auto_recall_observation["memory_context_appended"] is True
    assert agent._last_auto_recall_observation["append_logged"] is True


def test_append_observation_does_not_count_user_supplied_memory_tag():
    agent, _, _ = _agent()
    agent._last_auto_recall_observation = {"attempted": True}
    agent._auto_recall_context = "remembered fact"
    _mark_auto_recall_append(agent, "<memory-context>user input</memory-context>")
    assert agent._last_auto_recall_observation["memory_context_appended"] is False
    assert agent._last_auto_recall_observation["append_failure_reason"] == "build_block_empty"


def test_api_builder_marks_actual_sidecar_append_once():
    from agent.turn_context import build_api_messages, compose_user_api_content
    agent, _, _ = _agent()
    agent._current_turn_timestamp = 1.0
    agent._copy_reasoning_content_for_api = MagicMock()
    agent._should_sanitize_tool_calls = lambda: False
    agent.ephemeral_system_prompt = ""
    agent._last_auto_recall_observation = {"attempted": True}
    agent._auto_recall_context = "remembered fact"
    msg = {"role": "user", "content": "Question", "api_content":
           compose_user_api_content("Question", "remembered fact", "PLUGIN")}
    for _ in range(2):
        api_messages, _ = build_api_messages(
            agent, [msg], current_turn_user_idx=0, ext_prefetch_cache="remembered fact",
            plugin_user_context="PLUGIN", moa_config=None, active_system_prompt="system",
        )
        assert "remembered fact" in api_messages[-1]["content"]
    assert msg["content"] == "Question"
    assert agent._last_auto_recall_observation["memory_context_appended"] is True


def test_codex_early_return_injects_without_mutating_transcript():
    from agent.codex_runtime import run_codex_app_server_turn
    agent = MagicMock()
    agent.compression_checkpoint_required = False
    agent._codex_session.run_turn.return_value = SimpleNamespace(
        interrupted=False, error=None, should_retire=False, thread_id="thread",
        turn_id="turn", projected_messages=[], tool_iterations=0, final_text="ok",
    )
    agent._session_db = None
    agent._codex_session_prompt = None
    agent.tool_progress_callback = None
    agent._iters_since_skill = 0
    agent._skill_nudge_interval = 0
    agent.valid_tool_names = set()
    agent._last_auto_recall_observation = {"attempted": True}
    agent._auto_recall_context = "remembered fact"
    msg = {"role": "user", "content": "Question"}
    run_codex_app_server_turn(
        agent, user_message="Question", original_user_message="Question",
        messages=[msg], effective_task_id="task", ext_prefetch_cache="remembered fact",
        plugin_user_context="PLUGIN",
    )
    sent = agent._codex_session.run_turn.call_args.kwargs["user_input"]
    assert "remembered fact" in sent and "PLUGIN" in sent
    assert msg["content"] == "Question"
    assert agent._last_auto_recall_observation["memory_context_appended"] is True


def test_codex_start_failure_does_not_claim_recall_was_appended(monkeypatch):
    from agent.codex_runtime import run_codex_app_server_turn
    monkeypatch.setattr("agent.codex_runtime._ensure_codex_session", lambda agent, messages: None)
    monkeypatch.setattr("agent.codex_runtime._start_codex_thread", MagicMock(side_effect=OSError("offline")))
    monkeypatch.setattr("agent.codex_runtime._close_codex_session", lambda agent: None)
    monkeypatch.setattr("agent.codex_runtime._consume_user_interrupt", lambda agent: (False, None))
    agent = MagicMock(compression_checkpoint_required=False)
    agent._last_auto_recall_observation = {"attempted": True}
    agent._auto_recall_context = "remembered fact"
    result = run_codex_app_server_turn(
        agent, user_message="Question", original_user_message="Question",
        messages=[{"role": "user", "content": "Question"}], effective_task_id="task",
        ext_prefetch_cache="remembered fact",
    )
    assert result["completed"] is False
    agent._codex_session.run_turn.assert_not_called()
    assert "append_logged" not in agent._last_auto_recall_observation
