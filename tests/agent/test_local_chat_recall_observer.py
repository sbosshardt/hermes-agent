"""Prefetch selection through native Chat Completions request and observer boundaries.

No client sends: native build_api_kwargs, middleware, lifecycle and debug paths run
against inert recording peers. This checks the final assembled request, not acceptance.
"""
from copy import deepcopy
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, call

from agent.chat_completion_helpers import build_api_kwargs
from agent.conversation_loop import _apply_context_engine_selection
from agent.transports.chat_completions import ChatCompletionsTransport
from agent.turn_api_request import build_api_request
from agent.turn_context import _memory_turn_start_and_prefetch, build_api_messages


PRIVATE_PREFIXES = ("_auto_recall_", "_chat_auto_recall_")


def assert_private_free(value):
    if isinstance(value, dict):
        assert all(not key.startswith(PRIVATE_PREFIXES) for key in value if isinstance(key, str))
        for part in value.values():
            assert_private_free(part)
    elif isinstance(value, (list, tuple)):
        for part in value:
            assert_private_free(part)


def assemble(monkeypatch, *, user="Question", result="remembered fact", query="What did we decide?",
             history=None, selected=None, transform=None, selector=None, sidecar=None,
             plugin="PLUGIN", alter=None, caplog=None):
    manager = MagicMock()
    manager.prefetch_all.return_value = result
    manager.describe_recall.return_value = "recalled"
    db = MagicMock()
    transport = ChatCompletionsTransport()
    agent = SimpleNamespace(
        _memory_manager=manager, _session_db=db, _session_db_created=True,
        session_id="fixture-session", _user_turn_count=1, _emit_status=MagicMock(),
        _last_auto_recall_observation=None, _current_turn_timestamp=1.0,
        api_mode="chat_completions", provider="custom", model="fixture-model", platform="cli",
        base_url="http://fixture.invalid/v1", _base_url_lower="http://fixture.invalid/v1",
        _base_url_hostname="fixture.invalid", max_tokens=128, tools=[],
        reasoning_config={"enabled": False}, request_overrides={},
        providers_allowed=None, providers_ignored=None, providers_order=None,
        provider_sort=None, provider_require_parameters=False, provider_data_collection=None,
        openrouter_min_coding_score=None, _ollama_num_ctx=None,
        _copy_reasoning_content_for_api=lambda *a: None,
        _should_sanitize_tool_calls=lambda: False, ephemeral_system_prompt="",
        _reset_stream_delivery_tracking=lambda: None,
        _reapply_reasoning_echo_for_provider=lambda *a: None,
        _is_openrouter_url=lambda: False, _is_copilot_url=lambda: False,
        _is_qwen_portal=lambda: False, _get_transport=lambda: transport,
        _resolved_api_call_timeout=lambda: 1.0,
        _max_tokens_param=lambda limit: {"max_tokens": limit},
        _prepare_messages_for_non_vision_model=lambda rows: rows,
        # No profile claims fixture.invalid: the native endpoint lookup returns None.
        _reasoning_endpoint_profile=lambda: None,
        _supports_reasoning_extra_body=lambda: False,
        _empty_content_retries=0, _force_ascii_payload=False, _image_rejecting_models=set(),
        _is_user_initiated_turn=False, context_compressor=selector,
        client=SimpleNamespace(), _api_request_payload_for_hook=lambda kwargs: deepcopy(kwargs),
    )
    if caplog is not None:
        caplog.set_level(logging.INFO, logger="agent.conversation_loop")
    prefetched = _memory_turn_start_and_prefetch(agent, query)
    db_calls_after_prefetch = list(db.mock_calls)
    agent._build_api_kwargs = lambda rows, **kwargs: build_api_kwargs(agent, rows, **kwargs)
    current = {"role": "user", "content": user}
    if sidecar is not None:
        current["api_content"] = sidecar
    rows = [deepcopy(history or {"role": "user", "content": "Earlier question"}), current]
    original_rows = deepcopy(rows)
    wire, system = build_api_messages(
        agent, rows, current_turn_user_idx=1,
        ext_prefetch_cache=prefetched if selected is None else selected,
        plugin_user_context=plugin, moa_config=None, active_system_prompt="system",
    )
    initial_wire = wire
    if selector is not None:
        monkeypatch.setattr("agent.conversation_loop._engine_overrides_hook", lambda *a: True)
    wire = _apply_context_engine_selection(agent, wire, rows, current, logger=MagicMock())
    if alter:
        alter(agent, wire)
    # Cache decoration is held constant; native kwargs construction and middleware still run.
    monkeypatch.setattr("agent.conversation_loop._redecorate_prompt_cache_for_provider",
                        lambda agent, messages, **kw: (messages, {"messages": deepcopy(messages)}, []))
    crossings, hooks, debug = [], [], []
    agent._dump_api_request_debug = lambda kwargs, **kw: debug.append(deepcopy(kwargs))
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda kind: True)

    def middleware(kind, **context):
        payload = context["request"]
        crossings.append(payload)
        assert payload["model"] == "fixture-model"
        return [{"request": transform(payload) if transform else payload, "source": "fixture"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_middleware", middleware)
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda kind: kind == "pre_api_request")
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda kind, **kw: hooks.append(kw))
    monkeypatch.setattr("agent.turn_api_request.env_var_enabled", lambda key: key == "HERMES_DUMP_REQUESTS")
    out = build_api_request(
        agent, api_messages=wire, _moa_prepared_request=None, tools_for_api=[],
        system_message=system, messages=rows, original_user_message=user,
        approx_tokens=1, total_chars=1, retry_count=0, api_call_count=0,
        api_request_id="fixture-request", api_start_time=1.0,
        effective_task_id="fixture-task", turn_id="fixture-turn",
    )
    assert rows == original_rows
    assert len(crossings) == len(hooks) == len(debug) == 1
    assert_private_free((rows, initial_wire, wire, crossings, hooks, debug,
                         out.api_kwargs, out._original_api_kwargs, out.api_messages,
                         out._moa_prepared_request, out._llm_middleware_trace))
    assert db.mock_calls == db_calls_after_prefetch + [call.get_compression_lineage("fixture-session")]
    return agent, out, crossings, hooks, debug


def test_selected_recall_reaches_final_standard_request_and_no_private_copy(monkeypatch, caplog):
    agent, out, _, hooks, debug = assemble(monkeypatch, caplog=caplog)
    observation = agent._last_auto_recall_observation
    assert observation["attempted"] is True and observation["success"] is True
    assert observation["memory_context_appended"] is True
    assert observation["append_stage"] == "assembled_provider_bound"
    assert observation["append_logged"] is True
    assert out.api_kwargs["model"] == "fixture-model"
    assert "remembered fact" in out.api_kwargs["messages"][-1]["content"]
    assert hooks[0]["conversation_history"][-1]["content"] == "Question"
    assert "remembered fact" in debug[0]["messages"][-1]["content"]
    assert "remembered fact" not in caplog.text and "PLUGIN" not in caplog.text
    assert "_auto_recall_" not in caplog.text
    assert "Auto-recall chat assembly: appended=True" in caplog.text
