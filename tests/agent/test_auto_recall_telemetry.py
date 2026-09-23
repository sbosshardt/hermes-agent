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


def test_prefetch_metric_write_failure_is_reported_without_leaking_context(caplog):
    agent, _, db = _agent(result="private remembered fact")
    db.update_auto_recall_metrics.side_effect = OSError("sensitive database error: private token")
    assert _memory_turn_start_and_prefetch(agent, "What did we decide?") == "private remembered fact"
    assert "Auto-recall metrics persistence failed" in caplog.text
    assert "private token" not in caplog.text
    assert "private remembered fact" not in caplog.text


def _chat_observation_request(monkeypatch, *, user_text="Question", select=None, middleware=None,
                              history_text=None, prefetch="remembered fact", real_middleware=False,
                              inspect_request=None):
    """Exercise the real composition, selection hook and final request assembly without I/O."""
    from agent.turn_context import build_api_messages
    from agent.conversation_loop import _apply_context_engine_selection
    from agent.turn_api_request import build_api_request
    from agent.memory_manager import build_memory_context_block

    agent, _, _ = _agent()
    agent.api_mode = "chat_completions"
    agent._current_turn_timestamp = 1.0
    agent._copy_reasoning_content_for_api = MagicMock()
    agent._should_sanitize_tool_calls = lambda: False
    agent.ephemeral_system_prompt = ""
    agent._last_auto_recall_observation = {"attempted": True}
    agent._auto_recall_context = "remembered fact"
    agent._reset_stream_delivery_tracking = MagicMock()
    agent._reapply_reasoning_echo_for_provider = MagicMock()
    agent._build_api_kwargs = lambda msgs, **kw: {"messages": msgs}
    agent._is_openrouter_url = lambda: False
    agent._is_copilot_url = lambda: False
    agent._empty_content_retries = 0
    agent._force_ascii_payload = False
    agent._is_user_initiated_turn = False
    agent.tools = []
    agent.platform = "cli"
    agent.model = "test-model"
    agent.provider = "test-provider"
    agent.base_url = ""
    agent.max_tokens = 128
    agent.client = MagicMock()
    agent.context_compressor = select
    if select is not None:
        monkeypatch.setattr("agent.conversation_loop._engine_overrides_hook", lambda *args: True)
    monkeypatch.setattr("agent.conversation_loop._redecorate_prompt_cache_for_provider",
                        lambda agent, msgs, **kwargs: (msgs, None, []))
    if real_middleware:
        assert middleware is not None
        monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda kind: True)
        monkeypatch.setattr("hermes_cli.plugins.invoke_middleware",
                            lambda kind, **context: [{"request": middleware(**context)}])
    else:
        monkeypatch.setattr("hermes_cli.middleware.apply_llm_request_middleware",
                            lambda payload, **kwargs: SimpleNamespace(
                                payload=middleware(payload) if middleware else payload,
                                original_payload=payload, trace=[]))
    monkeypatch.setattr("agent.turn_api_request._fire_pre_api_request_hook",
                        lambda agent, api_kwargs, api_messages, *a, **k:
                        inspect_request(api_kwargs, api_messages) if inspect_request else None)
    if inspect_request:
        monkeypatch.setattr("agent.turn_api_request.env_var_enabled", lambda key: True)
        agent._dump_api_request_debug = lambda kwargs, **kw: inspect_request(kwargs, api_messages)
    monkeypatch.setattr("agent.turn_api_request.strip_images_for_rejecting_model", lambda *a: None)

    history = {"role": "user", "content": history_text if history_text is not None else
               "Earlier quote: " + build_memory_context_block("remembered fact")}
    current = {"role": "user", "content": user_text}
    rows = [history, current]
    api_messages, system = build_api_messages(
        agent, rows, current_turn_user_idx=1, ext_prefetch_cache=prefetch,
        plugin_user_context="", moa_config=None, active_system_prompt="system",
    )
    api_messages = _apply_context_engine_selection(agent, api_messages, rows, current,
                                                   logger=MagicMock())
    result = build_api_request(
        agent, api_messages=api_messages, _moa_prepared_request=None, tools_for_api=[],
        system_message=system, messages=rows, original_user_message=user_text,
        approx_tokens=1, total_chars=1, retry_count=0, api_call_count=0,
        api_request_id="req", api_start_time=1.0, effective_task_id="task", turn_id="turn",
    )
    # The request-local provenance must not enter the durable conversation or transport.
    assert all("_auto_recall_current_turn_provenance" not in row for row in rows)
    assert all("_auto_recall_current_turn_provenance" not in row
               for row in result.api_kwargs["messages"])
    if inspect_request:
        inspect_request(result._original_api_kwargs, result.api_messages)
    return agent._last_auto_recall_observation, result.api_kwargs


def test_chat_observation_requires_fresh_block_not_identical_quoted_history(monkeypatch):
    class DropMemory:
        def select_context(self, api_messages, **kwargs):
            return [{**m, "content": "Question"} if m.get("content", "").startswith("Question")
                    else m for m in api_messages]

    observation, _ = _chat_observation_request(monkeypatch, select=DropMemory())
    assert observation["memory_context_appended"] is False


def test_chat_observation_fails_when_context_engine_drops_current_turn(monkeypatch):
    from agent.memory_manager import build_memory_context_block

    class DropCurrent:
        def select_context(self, api_messages, **kwargs):
            return api_messages[:-1]

    # The surviving historical row is byte-for-byte identical to the composed current row.
    observation, _ = _chat_observation_request(
        monkeypatch, select=DropCurrent(),
        history_text="Question\n\n" + build_memory_context_block("remembered fact"),
    )
    assert observation["memory_context_appended"] is False


def test_chat_observation_rejects_stale_prefetch_even_when_it_is_composed(monkeypatch):
    observation, _ = _chat_observation_request(monkeypatch, prefetch="different remembered fact")
    assert observation["memory_context_appended"] is False


def test_chat_observation_rejects_middleware_replacing_current_with_identical_history(monkeypatch):
    from agent.turn_context import compose_user_api_content

    def substitute_history(payload):
        rows = payload["messages"]
        assert rows[-2]["content"] == rows[-1]["content"]
        return {**payload, "messages": [*rows[:-1], {**rows[-2]}]}

    observation, _ = _chat_observation_request(
        monkeypatch, middleware=substitute_history,
        history_text=compose_user_api_content("Question", "remembered fact", ""),
    )
    assert observation["memory_context_appended"] is False


def test_chat_observation_accepts_live_current_even_with_identical_history(monkeypatch):
    from agent.turn_context import compose_user_api_content

    observation, _ = _chat_observation_request(
        monkeypatch, history_text=compose_user_api_content("Question", "remembered fact", ""),
    )
    assert observation["memory_context_appended"] is True


@pytest.mark.parametrize("middleware", [
    lambda payload: {**payload, "messages": [m for m in payload["messages"]
                                             if not m.get("content", "").startswith("Question")]},
    lambda payload: {**payload, "messages": [
        {**m, "content": "Question"} if m.get("content", "").startswith("Question") else m
        for m in payload["messages"]]},
    lambda payload: {**payload, "messages": list(reversed(payload["messages"]))},
])
def test_chat_observation_fails_when_middleware_removes_rewrites_or_moves_current(monkeypatch, middleware):
    observation, _ = _chat_observation_request(monkeypatch, middleware=middleware)
    assert observation["memory_context_appended"] is False


def test_chat_observation_positive_only_after_final_request_assembly(monkeypatch):
    observation, kwargs = _chat_observation_request(monkeypatch)
    assert observation["memory_context_appended"] is True
    assert observation["append_logged"] is True
    assert observation["append_stage"] == "assembled_provider_bound"
    assert kwargs["messages"][-1]["role"] == "user"
    assert "remembered fact" in kwargs["messages"][-1]["content"]
    assert all("_auto_recall_current_turn_provenance" not in row for row in kwargs["messages"])


def test_chat_observation_allows_known_cache_text_part_normalization(monkeypatch):
    def cache_parts(payload):
        rows = list(payload["messages"])
        rows[-1] = {**rows[-1], "content": [
            {"type": "text", "text": rows[-1]["content"],
             "cache_control": {"type": "ephemeral"}},
        ]}
        return {**payload, "messages": rows}

    observation, _ = _chat_observation_request(monkeypatch, middleware=cache_parts)
    assert observation["memory_context_appended"] is True


def test_chat_observation_real_middleware_replacement_after_normalization_fails_closed(monkeypatch):
    """A copied historical row must not become a current turn just by matching text."""
    from agent.turn_context import _AUTO_RECALL_CURRENT_KEY, compose_user_api_content

    expected = compose_user_api_content("Question", "remembered fact", "")
    assert expected is not None

    def replace_with_normalized_history(**kwargs):
        rows = kwargs["request"]["messages"]
        assert rows[-2]["content"] != rows[-1]["content"]
        historical = {**rows[-2], "content": rows[-2]["content"].strip()}
        assert historical["content"] == rows[-1]["content"]
        return {**kwargs["request"], "messages": [*rows[:-1], historical],
                "test_replaced": True}

    def inspect(payload, api_messages):
        assert all(_AUTO_RECALL_CURRENT_KEY not in row for row in payload["messages"])
        assert all(_AUTO_RECALL_CURRENT_KEY not in row for row in api_messages)

    observation, kwargs = _chat_observation_request(
        monkeypatch, history_text=expected + "  ", middleware=replace_with_normalized_history,
        real_middleware=True, inspect_request=inspect,
    )
    assert kwargs["test_replaced"] is True  # real chain returned its replacement
    assert kwargs["messages"][-1]["content"] == expected
    assert observation["memory_context_appended"] is False


def test_chat_observation_real_middleware_normalized_current_and_all_copies_clean(monkeypatch):
    from agent.turn_context import _AUTO_RECALL_CURRENT_KEY

    inspections = []

    def inspect(payload, api_messages):
        assert all(_AUTO_RECALL_CURRENT_KEY not in row for row in payload["messages"])
        assert all(_AUTO_RECALL_CURRENT_KEY not in row for row in api_messages)
        inspections.append(True)

    def normalize(**kwargs):
        rows = list(kwargs["request"]["messages"])
        assert _AUTO_RECALL_CURRENT_KEY in rows[-1]  # proof crossed the real middleware copy
        rows[-1] = {**rows[-1], "content": [
            {"type": "text", "text": rows[-1]["content"],
             "cache_control": {"type": "ephemeral"}},
        ]}
        return {**kwargs["request"], "messages": rows}

    observation, _ = _chat_observation_request(
        monkeypatch, middleware=normalize, real_middleware=True, inspect_request=inspect,
    )
    assert observation["memory_context_appended"] is True
    assert len(inspections) == 3  # hook, debug dump, returned original payload


def test_chat_observation_real_middleware_duplicate_current_marker_fails_closed(monkeypatch):
    def duplicate(**kwargs):
        rows = kwargs["request"]["messages"]
        return {**kwargs["request"], "messages": [*rows, {**rows[-1]}]}

    observation, _ = _chat_observation_request(
        monkeypatch, middleware=duplicate, real_middleware=True,
    )
    assert observation["memory_context_appended"] is False


def test_chat_observation_real_middleware_lost_marker_fails_closed(monkeypatch):
    from agent.turn_context import _AUTO_RECALL_CURRENT_KEY

    def lose_marker(**kwargs):
        rows = list(kwargs["request"]["messages"])
        rows[-1] = {key: value for key, value in rows[-1].items()
                    if key != _AUTO_RECALL_CURRENT_KEY}
        return {**kwargs["request"], "messages": rows}

    observation, _ = _chat_observation_request(
        monkeypatch, middleware=lose_marker, real_middleware=True,
    )
    assert observation["memory_context_appended"] is False


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


def test_api_builder_does_not_mark_append_before_request_is_final():
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
    assert "memory_context_appended" not in agent._last_auto_recall_observation


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
