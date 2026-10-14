"""Vertical tracer: selected prefetch -> actual fresh-row composition/final assembly.
No provider inference, I/O or live state. Durable counters tested independently.
"""
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
    # Admit the selected state through the actual current prefetch helper.
    selected = _memory_turn_start_and_prefetch(agent, "What was the agreed design?")
    assert selected == "remembered fact"
    assert agent._last_auto_recall_observation["success"] is True
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


def test_actual_prefetch_to_final_current_request_observation(monkeypatch):
    observation, kwargs = _chat_observation_request(monkeypatch)
    assert "remembered fact" in kwargs["messages"][-1]["content"]
    assert observation.get("memory_context_appended") is True
    assert observation["append_stage"] == "assembled_provider_bound"
    assert observation["append_logged"] is True
    assert all("_auto_recall_current_turn_provenance" not in row for row in kwargs["messages"])
