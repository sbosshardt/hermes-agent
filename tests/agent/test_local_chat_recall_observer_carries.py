"""Carried chat refusal contracts, grounded in the actual target prefetch tracer.
The original tracer and prior tests are not changed.
"""
import pytest
from tests.agent.test_local_chat_recall_observer import _chat_observation_request as _request
from agent.turn_context import _AUTO_RECALL_PRIVATE_KEYS

def _chat_observation_request(*args, **kwargs):
    observation, payload = _request(*args, **kwargs)
    assert all(not(set(row) & set(_AUTO_RECALL_PRIVATE_KEYS)) for row in payload["messages"])
    if observation.get("memory_context_appended") is False:
        assert observation["append_stage"] == "assembled_provider_bound"
        assert observation["append_failure_reason"] == "not_in_final_current_user"
    return observation, payload


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
