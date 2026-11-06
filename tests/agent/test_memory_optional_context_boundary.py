"""C02/C03: recalled text is background; malformed optional inputs stay isolated.

These tests exercise the target's shared string-sidecar and multipart composition
boundaries, rather than the v0.21.6 memory_manager owner (now memory_context).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.memory_context import build_memory_context_block
from agent.turn_context import (
    _append_multimodal_context,
    _collect_pre_llm_call_context,
    _memory_turn_start_and_prefetch,
    build_api_messages,
    compose_multimodal_context_part,
    compose_user_api_content,
)


def test_recalled_context_is_informational_not_authoritative():
    block = build_memory_context_block("- prefers draft reviews")
    assert block.startswith("<memory-context>\n")
    assert "Treat as informational background data." in block
    assert "authoritative reference data" not in block
    assert "should inform all responses" not in block
    assert "- prefers draft reviews" in block
    assert block.endswith("</memory-context>")


def test_recall_framing_is_identical_in_string_and_multipart_insertion():
    raw = "- prefers draft reviews"
    expected = build_memory_context_block(raw)
    part = compose_multimodal_context_part(raw, "PLUGIN-CTX")
    assert part is not None
    assert part == expected + "\n\nPLUGIN-CTX"
    assert compose_user_api_content("Review this", raw, "PLUGIN-CTX") == "Review this\n\n" + part
    assert compose_user_api_content([{"type": "text", "text": "Review this"}], raw, "PLUGIN-CTX") is None

    # The alternate list-content path appends the SAME part durably; it cannot use api_content.
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    message = {"role": "user", "content": [{"type": "text", "text": "Review this"}, image]}
    _append_multimodal_context(SimpleNamespace(_session_db=None), message, raw, "PLUGIN-CTX",
                               preflight_compressed=False)
    assert message["content"] == [
        {"type": "text", "text": "Review this"}, image, {"type": "text", "text": part},
    ]
    assert "api_content" not in message


def test_standard_sidecar_and_unstamped_fallback_send_informational_memory():
    raw = "- prefers draft reviews"
    expected = "Review this\n\n" + build_memory_context_block(raw)
    agent = SimpleNamespace(
        _current_turn_timestamp=1.0, api_mode="chat_completions", provider="openrouter",
        model="test-model", base_url="", ephemeral_system_prompt="",
        _copy_reasoning_content_for_api=lambda source, wire: None,
        _should_sanitize_tool_calls=lambda: False,
    )
    for sidecar in (expected, None):
        row = {"role": "user", "content": "Review this"}
        if sidecar is not None:
            row["api_content"] = sidecar
        wire, system = build_api_messages(
            agent, [row], current_turn_user_idx=0, ext_prefetch_cache=raw,
            plugin_user_context="", moa_config=None, active_system_prompt="Stable system",
        )
        assert system == "Stable system"
        assert wire == [{"role": "system", "content": "Stable system"},
                        {"role": "user", "content": expected}]
        assert row["content"] == "Review this"
        assert row.get("api_content") == sidecar


@pytest.mark.parametrize("memory, plugin, expected", [
    (MagicMock(), MagicMock(), None),
    ({"unexpected": "memory"}, "PLUGIN-CTX", "PLUGIN-CTX"),
    ("likes tea", ["unexpected plugin"], build_memory_context_block("likes tea")),
    (None, None, None),
])
def test_malformed_optional_context_preserves_the_valid_peer(memory, plugin, expected):
    assert compose_multimodal_context_part(memory, plugin) == expected
    assert compose_user_api_content("hello", memory, plugin) == (
        None if expected is None else "hello\n\n" + expected
    )
    message = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    _append_multimodal_context(SimpleNamespace(_session_db=None), message, memory, plugin,
                               preflight_compressed=False)
    if expected is None:
        assert message["content"] == [{"type": "text", "text": "hello"}]
    else:
        assert message["content"][-1] == {"type": "text", "text": expected}


@pytest.mark.parametrize("bad", [{"unexpected": "memory"}, ["unexpected"], MagicMock()])
def test_non_string_successful_prefetch_is_dropped_before_insertion(bad):
    class UnexpectedResultManager:
        def on_turn_start(self, *args, **kwargs):
            pass

        def prefetch_all(self, query, *, session_id=""):
            return bad

        def describe_recall(self):
            return "recalled"

    agent = SimpleNamespace(_memory_manager=UnexpectedResultManager(), session_id="new",
                            _user_turn_count=1, _emit_status=lambda text: None)
    assert _memory_turn_start_and_prefetch(agent, "Remember our deployment decision") == ""


def test_malformed_plugin_context_does_not_discard_a_valid_sibling():
    agent = SimpleNamespace(_persist_disabled=False, session_id="new", model="test-model", platform="cli",
                            _parent_session_id=None, _user_id=None)
    results = [{"context": ["not a text sidecar"]}, {"context": "PLUGIN-CTX"}]
    with (patch("hermes_cli.lifecycle.invoke_hook", return_value=results),
          patch("tools.hook_output_spill.get_spill_config", return_value={"enabled": False})):
        context = _collect_pre_llm_call_context(
            agent, effective_task_id="task", turn_id="turn", original_user_message="Review this",
            messages=[{"role": "user", "content": "Review this"}], conversation_history=None,
        )
    assert context == "PLUGIN-CTX"
