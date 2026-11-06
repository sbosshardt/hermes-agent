"""Native standard-chat assembly: ephemeral provenance never survives any retained copy."""
from copy import deepcopy
from dataclasses import replace
import socket

import pytest

from agent.memory_manager import sanitize_context
from agent.turn_context import compose_user_api_content
from tests.agent.test_local_chat_recall_observer import assemble, assert_private_free


@pytest.fixture(autouse=True)
def forbid_provider_socket(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("this request-assembly proof cannot perform provider I/O")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_native_middleware_receives_ephemeral_origin_then_every_retained_copy_is_clean(monkeypatch):
    seen_private = []
    def inspect(payload):
        seen_private.extend(key for key in payload["messages"][-1]
                            if isinstance(key, str) and key.startswith("_auto_recall_"))
        return deepcopy(payload)

    agent, out, _, hooks, debug = assemble(monkeypatch, transform=inspect)
    assert seen_private, "origin must be carried across the real middleware deep-copy boundary"
    assert agent._last_auto_recall_observation["memory_context_appended"] is True
    assert hooks[0]["request"]["messages"] == out.api_kwargs["messages"]
    assert debug[0]["messages"] == out.api_kwargs["messages"]
    assert_private_free((agent._last_auto_recall_observation, hooks, debug, out.api_kwargs))


@pytest.mark.parametrize("kind", ["duplicate", "forged", "wrong_type", "wrong_role", "suffix"])
def test_native_middleware_origin_and_suffix_refusals(monkeypatch, kind):
    def transform(payload):
        rows = deepcopy(payload["messages"])
        markers = [key for key in rows[-1] if isinstance(key, str) and key.startswith("_auto_recall_")]
        assert markers, "native builder must attach a private current-row proof before middleware"
        marker = markers[0]
        if kind == "duplicate":
            rows.append(deepcopy(rows[-1]))
        elif kind == "forged":
            rows[-1][marker] = "forged-fixture"
        elif kind == "wrong_type":
            rows[-1][marker] = {"invalid": True}
        elif kind == "wrong_role":
            rows[-1]["role"] = "assistant"
        else:
            rows[-1]["content"] += " changed suffix"
        return {**payload, "messages": rows}

    historical = {"role": "user", "content": compose_user_api_content(
        "Question", "remembered fact", "PLUGIN")}
    agent, _, _, _, _ = assemble(monkeypatch, history=historical, transform=transform)
    assert agent._last_auto_recall_observation["memory_context_appended"] is False


@pytest.mark.parametrize("result", ["", "   ", None, 17, {"unexpected": "private"}])
def test_native_absent_selection_never_claims_final_append(monkeypatch, result):
    agent, _, _, _, _ = assemble(monkeypatch, result=result)
    observation = agent._last_auto_recall_observation
    assert observation is not None
    assert observation["success"] is False
    assert observation.get("memory_context_appended") is not True


def test_native_selected_text_removed_by_actual_context_sanitizer_does_not_claim_append(monkeypatch):
    text = "<memory-context>nested recalled fixture</memory-context>"
    assert sanitize_context(text) == ""
    agent, _, _, _, _ = assemble(monkeypatch, result=text)
    assert agent._last_auto_recall_observation["success"] is True
    assert agent._last_auto_recall_observation["memory_context_appended"] is False


def test_native_null_character_selection_is_not_falsely_treated_as_empty(monkeypatch):
    assert sanitize_context("\x00") == "\x00"
    agent, _, _, _, _ = assemble(monkeypatch, result="\x00")
    assert agent._last_auto_recall_observation["memory_context_appended"] is True


@pytest.mark.parametrize("query", ["ok", [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]])
def test_native_skipped_turn_does_not_claim_append(monkeypatch, query):
    agent, _, _, _, _ = assemble(monkeypatch, query=query)
    assert agent._last_auto_recall_observation is None
    agent._memory_manager.prefetch_all.assert_not_called()


def test_native_original_multimodal_content_has_no_false_string_origin_claim(monkeypatch):
    agent, _, _, _, _ = assemble(monkeypatch, user=[{"type": "text", "text": "Question"}])
    assert agent._last_auto_recall_observation["memory_context_appended"] is False


def test_native_sidecar_must_match_actual_selected_recall(monkeypatch):
    correct = compose_user_api_content("Question", "remembered fact", "PLUGIN")
    wrong = compose_user_api_content("Question", "stale fact", "PLUGIN")
    good, _, _, _, _ = assemble(monkeypatch, sidecar=correct)
    bad, _, _, _, _ = assemble(monkeypatch, sidecar=wrong)
    assert good._last_auto_recall_observation["memory_context_appended"] is True
    assert bad._last_auto_recall_observation["memory_context_appended"] is False


def test_native_historical_private_marker_and_identical_text_cannot_supply_origin(monkeypatch):
    historical = {"role": "user", "content": compose_user_api_content(
        "Question", "remembered fact", "PLUGIN")}
    def stale_marker(payload):
        rows = deepcopy(payload["messages"])
        rows[-1] = rows[-2] | {"_auto_recall_current_turn_provenance": "untrusted-old-history"}
        return {**payload, "messages": rows}

    agent, _, _, hooks, _ = assemble(monkeypatch, history=historical, transform=stale_marker)
    assert agent._last_auto_recall_observation["memory_context_appended"] is False
    assert_private_free(hooks[0]["conversation_history"])


@pytest.mark.parametrize("kind", ["drop", "rewrite", "move"])
def test_native_inplace_selector_retained_detached_row_is_clean(monkeypatch, kind):
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

    agent, _, _, _, _ = assemble(monkeypatch, selector=Selector())
    assert agent._last_auto_recall_observation["memory_context_appended"] is False
    assert len(retained) == 1
    assert_private_free(retained)


@pytest.mark.parametrize("kind", ["lost", "bad_marker", "bad_position", "changed_observation", "stale_selection"])
def test_native_stale_or_malformed_request_binding_refuses_append(monkeypatch, kind):
    def alter(agent, wire):
        if kind == "changed_observation":
            agent._last_auto_recall_observation = dict(agent._last_auto_recall_observation)
        elif kind == "stale_selection":
            agent._auto_recall_context = "new selected context"
        else:
            proof = getattr(agent, "_chat_auto_recall_provenance", None)
            assert proof is not None, "the request must bind a current-row proof before assembly"
            if kind == "lost":
                agent._chat_auto_recall_provenance = None
            elif kind == "bad_marker":
                agent._chat_auto_recall_provenance = replace(proof, marker=17)
            else:
                agent._chat_auto_recall_provenance = replace(proof, position=True)

    agent, _, _, _, _ = assemble(monkeypatch, alter=alter)
    assert agent._last_auto_recall_observation["memory_context_appended"] is False


@pytest.mark.parametrize("stage", ["reset", "builder", "sanitizer"])
def test_native_failure_cleans_all_known_partially_built_copies(monkeypatch, stage):
    captured = {}
    def alter(agent, wire):
        captured["agent"] = agent
        captured["wire"] = wire
        def fail(*args, **kwargs):
            if stage == "sanitizer":
                captured["native_payload"] = args[1]
            raise OSError("fixture assembly failure")
        if stage == "reset":
            agent._reset_stream_delivery_tracking = fail
        elif stage == "builder":
            agent._build_api_kwargs = fail
        else:
            monkeypatch.setattr("agent.turn_api_request.sanitize_outbound_kwargs", fail)

    with pytest.raises(OSError, match="fixture assembly failure"):
        assemble(monkeypatch, alter=alter)
    assert_private_free({key: value for key, value in captured.items() if key != "agent"})
    agent = captured["agent"]
    assert getattr(agent, "_chat_auto_recall_cleanup_payloads", None) is None
    assert getattr(agent, "_chat_auto_recall_provenance", None) is None
    assert not agent._last_auto_recall_observation.get("append_logged", False)
