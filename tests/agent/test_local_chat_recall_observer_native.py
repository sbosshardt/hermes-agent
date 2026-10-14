"""Native target chat builder/middleware/hook proof; no provider I/O or real state.

The genuine prefetch helper selects a fixture manager result. Real wire composition,
context selection, build_api_kwargs, ChatCompletionsTransport, outbound sanitization,
request middleware and pre_api_request hook are exercised. The peer never sends.
"""
from copy import deepcopy
from dataclasses import replace
import logging
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from agent.chat_completion_helpers import build_api_kwargs
from agent.conversation_loop import _apply_context_engine_selection
from agent.transports.chat_completions import ChatCompletionsTransport
from agent.turn_api_request import build_api_request
from agent.turn_context import (
    _AUTO_RECALL_CURRENT_KEY, _AUTO_RECALL_PRIVATE_KEYS,
    _memory_turn_start_and_prefetch, build_api_messages, compose_user_api_content,
)


@pytest.fixture(autouse=True)
def no_provider_io(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("No socket transport is authorized in this proof")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _clean(payload):
    if isinstance(payload, dict):
        assert not set(payload) & set(_AUTO_RECALL_PRIVATE_KEYS)
        for value in payload.values():
            _clean(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            _clean(value)


def _assemble(monkeypatch, *, user="Question", result="remembered fact", boundary=True,
              query="What did we decide?", selected=None, plugin="PLUGIN", mode="chat_completions",
              history=None, sidecar=None, alter=None, transform=None, select=None):
    manager = MagicMock()
    manager.wait_for_session_boundary.return_value = boundary
    manager.prefetch_all.return_value = result
    manager.describe_recall.return_value = "recalled"
    db = MagicMock()
    transport = ChatCompletionsTransport()
    agent = SimpleNamespace(
        _memory_manager=manager, _session_db=db, _session_db_created=True,
        session_id="fixture-session", _user_turn_count=1, _emit_status=MagicMock(),
        _last_auto_recall_observation=None, _current_turn_timestamp=1.0,
        api_mode=mode, provider="custom", model="fixture-model", platform="cli",
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
        _supports_reasoning_extra_body=lambda: False,
        _empty_content_retries=0, _force_ascii_payload=False, _image_rejecting_models=set(),
        _is_user_initiated_turn=False, context_compressor=select,
        client=SimpleNamespace(), _api_request_payload_for_hook=lambda kwargs: deepcopy(kwargs),
    )
    # Real selected observation; no fabricated append flags or observation contract.
    prefetched = _memory_turn_start_and_prefetch(agent, query)
    db_before = list(db.mock_calls)
    hooks, debug, crossings = [], [], []
    agent._dump_api_request_debug = lambda kwargs, **kw: debug.append(deepcopy(kwargs))
    agent._build_api_kwargs = lambda rows, **kwargs: build_api_kwargs(agent, rows, **kwargs)
    current = {"role": "user", "content": user}
    if sidecar is not None:
        current["api_content"] = sidecar
    rows = [deepcopy(history or {"role": "user", "content": "Earlier question"}), current]
    frozen_history = deepcopy(rows)
    wire, system = build_api_messages(
        agent, rows, current_turn_user_idx=1,
        ext_prefetch_cache=prefetched if selected is None else selected,
        plugin_user_context=plugin, moa_config=None, active_system_prompt="system",
    )
    source_wire = wire
    if select is not None:
        monkeypatch.setattr("agent.conversation_loop._engine_overrides_hook", lambda *a: True)
    wire = _apply_context_engine_selection(agent, wire, rows, current, logger=MagicMock())
    if alter is not None:
        alter(agent, wire)
    # Keep cache redecoration deterministic; formatting is exercised by real middleware below.
    monkeypatch.setattr("agent.conversation_loop._redecorate_prompt_cache_for_provider",
                        lambda agent, msgs, **kwargs: (msgs, {"messages": deepcopy(msgs)}, []))
    monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda kind: True)
    def middleware(kind, **context):
        payload = context["request"]
        crossings.append(deepcopy(payload))
        assert "model" in payload  # actual native kwargs builder was reached
        return [{"request": transform(payload) if transform else payload, "source": "fixture"}]
    monkeypatch.setattr("hermes_cli.plugins.invoke_middleware", middleware)
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda kind: kind == "pre_api_request")
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda kind, **kw: hooks.append(kw))
    monkeypatch.setattr("agent.turn_api_request.env_var_enabled", lambda key: key == "HERMES_DUMP_REQUESTS")
    assembled = build_api_request(
        agent, api_messages=wire, _moa_prepared_request=None, tools_for_api=[],
        system_message=system, messages=rows, original_user_message=user,
        approx_tokens=1, total_chars=1, retry_count=0, api_call_count=0,
        api_request_id="fixture-request", api_start_time=1.0,
        effective_task_id="fixture-task", turn_id="fixture-turn",
    )
    assert rows == frozen_history  # no durable or user-content mutation
    assert agent._chat_auto_recall_cleanup_payloads is None
    assert agent._chat_auto_recall_provenance is None
    assert len(hooks) == len(debug) == len(crossings) == 1
    # The native kwargs builder reads cache-scope lineage; no additional DB writes occur.
    assert db.mock_calls == db_before + [call.get_compression_lineage("fixture-session")]
    for payload in (assembled.api_kwargs, assembled._original_api_kwargs,
                    assembled.api_messages, assembled._moa_prepared_request,
                    assembled._llm_middleware_trace, source_wire, hooks, debug):
        _clean(payload)
    observation = agent._last_auto_recall_observation
    if isinstance(observation, dict) and observation.get("append_logged"):
        assert observation["append_stage"] == "assembled_provider_bound"
        if observation["memory_context_appended"] is False:
            assert observation["append_failure_reason"] == "not_in_final_current_user"
    return agent, assembled, crossings, hooks, debug


def test_native_builder_real_middleware_hook_and_every_retained_copy_clean(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="agent.conversation_loop")
    agent, out, crossing, hooks, debug = _assemble(monkeypatch)
    assert _AUTO_RECALL_CURRENT_KEY in crossing[0]["messages"][-1]
    assert out.api_kwargs["model"] == "fixture-model"
    assert agent._last_auto_recall_observation["memory_context_appended"] is True
    assert hooks[0]["conversation_history"][-1]["content"] == "Question"
    assert "remembered fact" in debug[0]["messages"][-1]["content"]
    assert "remembered fact" not in caplog.text and "PLUGIN" not in caplog.text
    assert "Auto-recall chat assembly: appended=True" in caplog.text


@pytest.mark.parametrize("mutation", ["lost", "duplicate", "forged", "wrong_type", "legacy", "wrong_role",
                                       "move", "rewrite", "substitute", "suffix"])
def test_native_final_origin_refusals_reach_final_observer(monkeypatch, mutation):
    def change(payload):
        rows = deepcopy(payload["messages"])
        if mutation == "lost": rows[-1].pop(_AUTO_RECALL_CURRENT_KEY)
        elif mutation == "duplicate": rows.append(deepcopy(rows[-1]))
        elif mutation == "forged": rows[-1][_AUTO_RECALL_CURRENT_KEY] = "forged-fixture"
        elif mutation == "wrong_type": rows[-1][_AUTO_RECALL_CURRENT_KEY] = {"marker": "invalid"}
        elif mutation == "legacy": rows[-1]["_auto_recall_current_turn_provenance"] = "forged-fixture"
        elif mutation == "wrong_role": rows[-1]["role"] = "assistant"
        elif mutation == "move": rows = list(reversed(rows))
        elif mutation == "rewrite": rows[-1]["content"] = "Question"
        elif mutation == "substitute": rows[-1] = deepcopy(rows[-2])
        elif mutation == "suffix": rows[-1]["content"] += " changed suffix"
        return {**payload, "messages": rows}
    history = {"role": "user", "content": compose_user_api_content("Question", "remembered fact", "PLUGIN")}
    agent, _, _, _, _ = _assemble(monkeypatch, transform=change, history=history)
    assert agent._last_auto_recall_observation["memory_context_appended"] is False


@pytest.mark.parametrize("cache", [None, {"type": "ephemeral"}, {"type": "ephemeral", "ttl": "1h"}])
@pytest.mark.parametrize("parts", [1, 2])
def test_native_known_one_two_text_cache_parts_preserve_exact_current_suffix(monkeypatch, cache, parts):
    def format(payload):
        rows = deepcopy(payload["messages"])
        text = rows[-1]["content"]
        texts = [text] if parts == 1 else [text[:8], text[8:]]
        rows[-1]["content"] = [{"type": "text", "text": t, **({"cache_control": cache} if cache else {})} for t in texts]
        return {**payload, "messages": rows}
    agent, _, _, _, _ = _assemble(monkeypatch, transform=format)
    assert agent._last_auto_recall_observation["memory_context_appended"] is True


@pytest.mark.parametrize("kind", ["three_parts", "image", "bad_cache", "unknown_key", "bad_type", "not_list", "wrong_text"])
def test_native_unsupported_format_refusals_reach_observer(monkeypatch, kind):
    def format(payload):
        rows = deepcopy(payload["messages"]);text = rows[-1]["content"]
        content = [{"type": "text", "text": text}]
        if kind == "three_parts": content += [{"type": "text", "text": ""}] * 2
        elif kind == "image": content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}})
        elif kind == "bad_cache": content[0]["cache_control"] = {"type": "ephemeral", "ttl": "invalid"}
        elif kind == "unknown_key": content[0]["fixture_extra"] = True
        elif kind == "bad_type": content[0]["text"] = 17
        elif kind == "not_list": content = {"type": "text", "text": text}
        elif kind == "wrong_text": content[0]["text"] = text + "semantic rewrite"
        rows[-1]["content"] = content
        return {**payload, "messages": rows}
    agent, _, _, _, _ = _assemble(monkeypatch, transform=format)
    assert agent._last_auto_recall_observation["memory_context_appended"] is False


@pytest.mark.parametrize("tamper", ["lost_snapshot", "invalid_snapshot", "bad_marker", "bad_position", "stale_context", "new_observation"])
def test_native_stale_or_malformed_request_snapshot_refuses(monkeypatch, tamper):
    def alter(agent, rows):
        proof = agent._chat_auto_recall_provenance
        if tamper == "lost_snapshot": agent._chat_auto_recall_provenance = None
        elif tamper == "invalid_snapshot": agent._chat_auto_recall_provenance = {"marker": proof.marker}
        elif tamper == "bad_marker": agent._chat_auto_recall_provenance = replace(proof, marker=17)
        elif tamper == "bad_position": agent._chat_auto_recall_provenance = replace(proof, position=True)
        elif tamper == "stale_context": agent._auto_recall_context = "new selected context"
        elif tamper == "new_observation": agent._last_auto_recall_observation = dict(agent._last_auto_recall_observation)
    agent, _, _, _, _ = _assemble(monkeypatch, alter=alter)
    assert agent._last_auto_recall_observation["memory_context_appended"] is False


@pytest.mark.parametrize("context", ["", "   ", None, 17, {"bad": "value"}])
def test_native_empty_or_malformed_selection_cannot_claim_append(monkeypatch, context):
    agent, _, _, _, _ = _assemble(monkeypatch, result=context)
    observation = agent._last_auto_recall_observation
    assert observation.get("memory_context_appended") is not True
    assert not observation.get("append_logged", False)


def test_native_null_only_is_nonempty_under_the_actual_sanitizer(monkeypatch):
    from agent.memory_manager import sanitize_context
    assert sanitize_context("\x00") == "\x00"  # the target has no control-character-empty policy
    agent, _, _, _, _ = _assemble(monkeypatch, result="\x00")
    assert agent._last_auto_recall_observation["memory_context_appended"] is True


def test_native_genuinely_sanitization_empty_selection_cannot_claim_append(monkeypatch):
    from agent.memory_manager import sanitize_context
    selected = "<memory-context>nested recalled fixture</memory-context>"
    assert sanitize_context(selected) == ""
    agent, _, _, _, _ = _assemble(monkeypatch, result=selected)
    observation = agent._last_auto_recall_observation
    assert observation["success"] is True  # raw selected result was nonempty
    assert observation["memory_context_appended"] is False
    assert observation["append_failure_reason"] == "not_in_final_current_user"


@pytest.mark.parametrize("query,boundary", [("hello", True), ("What did we decide?", False)])
def test_native_trivial_or_unadmitted_turn_has_no_append_claim(monkeypatch, query, boundary):
    agent, _, _, _, _ = _assemble(monkeypatch, query=query, boundary=boundary)
    assert agent._last_auto_recall_observation is None
    agent._memory_manager.prefetch_all.assert_not_called()


def test_native_identical_historical_text_and_stale_markers_do_not_supply_origin(monkeypatch):
    historical = {"role": "user", "content": compose_user_api_content("Question", "remembered fact", "PLUGIN"),
                  **{key: "untrusted-history" for key in _AUTO_RECALL_PRIVATE_KEYS}}
    agent, _, _, hooks, _ = _assemble(monkeypatch, history=historical)
    assert agent._last_auto_recall_observation["memory_context_appended"] is True
    _clean(hooks[0]["conversation_history"])


@pytest.mark.parametrize("sidecar_ok", [True, False])
def test_native_current_sidecar_must_match_selected_recall_and_plugin_suffix(monkeypatch, sidecar_ok):
    sidecar = compose_user_api_content("Question", "remembered fact" if sidecar_ok else "stale fact", "PLUGIN")
    agent, _, _, _, _ = _assemble(monkeypatch, sidecar=sidecar)
    assert agent._last_auto_recall_observation["memory_context_appended"] is sidecar_ok


def test_native_original_multipart_sends_without_false_string_origin_claim(monkeypatch):
    agent, _, _, _, _ = _assemble(monkeypatch, user=[{"type": "text", "text": "Question"}])
    assert agent._last_auto_recall_observation["memory_context_appended"] is False


@pytest.mark.parametrize("kind", ["drop", "rewrite", "move"])
def test_native_context_engine_current_origin_refusal(monkeypatch, kind):
    class Selector:
        def select_context(self, api_messages, **kwargs):
            rows = deepcopy(api_messages)
            if kind == "drop": return rows[:-1]
            if kind == "move": return list(reversed(rows))
            rows[-1]["content"] = "Question"
            return rows
    agent, _, _, _, _ = _assemble(monkeypatch, select=Selector())
    assert agent._last_auto_recall_observation["memory_context_appended"] is False


def test_native_stale_prefetch_not_recovered_from_substring(monkeypatch):
    agent, _, _, _, _ = _assemble(monkeypatch, selected="different remembered fact")
    assert agent._last_auto_recall_observation["memory_context_appended"] is False


@pytest.mark.parametrize("kind", ["drop", "rewrite", "move"])
def test_inplace_selector_detached_original_rows_are_also_clean(monkeypatch, kind):
    retained = []
    class Selector:
        def select_context(self, api_messages, **kwargs):
            retained.append(api_messages[-1])
            if kind == "drop":
                api_messages.pop()
            elif kind == "move":
                api_messages.reverse()
            else:
                api_messages[-1] = {**api_messages[-1], "content": "Question"}
            return api_messages
    agent, _, _, _, _ = _assemble(monkeypatch, select=Selector())
    assert agent._last_auto_recall_observation["memory_context_appended"] is False
    assert len(retained) == 1
    _clean(retained)


@pytest.mark.parametrize("stage", ["reset", "native_builder", "sanitize"])
def test_assembly_failure_releases_original_and_known_partial_copies(monkeypatch, stage):
    captured = {}
    def alter(agent, wire):
        captured.update(agent=agent, wire=wire, retained=agent._chat_auto_recall_cleanup_payloads)
        assert _AUTO_RECALL_CURRENT_KEY in wire[-1]
        def fail(*args, **kwargs):
            if stage == "sanitize":
                captured["native_payload"] = args[1]
            raise OSError("fixture assembly failure")
        if stage == "reset":
            agent._reset_stream_delivery_tracking = fail
        elif stage == "native_builder":
            agent._build_api_kwargs = fail
        else:
            monkeypatch.setattr("agent.turn_api_request.sanitize_outbound_kwargs", fail)
    with pytest.raises(OSError, match="fixture assembly failure"):
        _assemble(monkeypatch, alter=alter)
    agent = captured.pop("agent")
    assert agent._chat_auto_recall_cleanup_payloads is None
    assert agent._chat_auto_recall_provenance is None
    assert not agent._last_auto_recall_observation.get("append_logged", False)
    _clean(captured)


def test_fresh_rebuild_retires_abandoned_binding_without_touching_history():
    from tests.agent.test_local_chat_recall_observer import _agent
    agent, _, _ = _agent()
    agent.api_mode = "chat_completions"
    agent._current_turn_timestamp = 1.0
    agent._copy_reasoning_content_for_api = lambda *a: None
    agent._should_sanitize_tool_calls = lambda: False
    agent.ephemeral_system_prompt = ""
    agent.provider = "custom"
    agent.model = "fixture-model"
    selected = _memory_turn_start_and_prefetch(agent, "What did we decide?")
    rows = [{"role": "user", "content": "Question"}]
    original = deepcopy(rows)
    first, _ = build_api_messages(
        agent, rows, current_turn_user_idx=0, ext_prefetch_cache=selected,
        plugin_user_context="PLUGIN", moa_config=None, active_system_prompt="system",
    )
    first_binding = agent._chat_auto_recall_cleanup_payloads
    assert _AUTO_RECALL_CURRENT_KEY in first[-1]
    second, _ = build_api_messages(
        agent, rows, current_turn_user_idx=0, ext_prefetch_cache=selected,
        plugin_user_context="PLUGIN", moa_config=None, active_system_prompt="system",
    )
    _clean(first_binding)
    assert _AUTO_RECALL_CURRENT_KEY in second[-1]
    assert agent._chat_auto_recall_cleanup_payloads[0] is second
    assert rows == original
    from agent.turn_api_request import _release_chat_recall_cleanup
    _release_chat_recall_cleanup(agent)
    _release_chat_recall_cleanup(agent)  # release is idempotent, not an append observation
    _clean(second)
    assert agent._chat_auto_recall_cleanup_payloads is None
    assert agent._chat_auto_recall_provenance is None
    assert "append_logged" not in agent._last_auto_recall_observation
