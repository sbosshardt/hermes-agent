"""Current-row identity survives only genuine final assembled Chat Completions requests."""
from copy import deepcopy

import pytest

from agent.turn_context import compose_user_api_content
from tests.agent.test_local_chat_recall_observer import assemble


SUFFIX = compose_user_api_content("Question", "remembered fact", "PLUGIN")


def _assert_claim(agent, expected):
    observation = agent._last_auto_recall_observation
    assert observation["memory_context_appended"] is expected
    assert observation["append_stage"] == "assembled_provider_bound"
    if not expected:
        assert observation["append_failure_reason"] == "not_in_final_current_user"


def test_identical_historical_text_does_not_substitute_for_rewritten_current(monkeypatch):
    class DropMemory:
        def select_context(self, api_messages, **kwargs):
            return [{**m, "content": "Question"} if m.get("content", "").startswith("Question") else m
                    for m in api_messages]

    agent, _, _, _, _ = assemble(monkeypatch, selector=DropMemory(),
                                  history={"role": "user", "content": SUFFIX})
    _assert_claim(agent, False)


def test_selected_current_row_may_share_identical_text_with_history(monkeypatch):
    agent, _, _, _, _ = assemble(monkeypatch, history={"role": "user", "content": SUFFIX})
    _assert_claim(agent, True)


def test_context_engine_removal_is_not_rescued_by_identical_historical_row(monkeypatch):
    class DropCurrent:
        def select_context(self, api_messages, **kwargs):
            return api_messages[:-1]

    agent, _, _, _, _ = assemble(monkeypatch, selector=DropCurrent(),
                                  history={"role": "user", "content": SUFFIX})
    _assert_claim(agent, False)


def test_stale_composed_prefetch_is_not_a_selected_recall(monkeypatch):
    agent, _, _, _, _ = assemble(monkeypatch, selected="different remembered fact")
    _assert_claim(agent, False)


@pytest.mark.parametrize("mutation", ["remove", "rewrite", "reorder", "replace", "copy_history"])
def test_deepcopied_middleware_cannot_relabel_replaced_or_reordered_row(monkeypatch, mutation):
    def transform(payload):
        rows = deepcopy(payload["messages"])
        if mutation == "remove":
            rows.pop()
        elif mutation == "rewrite":
            rows[-1]["content"] = "Question"
        elif mutation == "reorder":
            rows.reverse()
        elif mutation == "replace":
            rows[-1] = {"role": "user", "content": SUFFIX}
        else:
            rows[-1] = deepcopy(rows[-2])
        return {**payload, "messages": rows}

    agent, _, _, _, _ = assemble(monkeypatch, transform=transform,
                                  history={"role": "user", "content": SUFFIX})
    _assert_claim(agent, False)


def test_middleware_can_deepcopy_unmodified_current_without_losing_origin(monkeypatch):
    agent, _, _, _, _ = assemble(monkeypatch, transform=deepcopy,
                                  history={"role": "user", "content": SUFFIX})
    _assert_claim(agent, True)


@pytest.mark.parametrize("parts", [1, 2])
def test_known_text_parts_with_cache_control_preserve_selected_suffix(monkeypatch, parts):
    def text_parts(payload):
        rows = deepcopy(payload["messages"])
        text = rows[-1]["content"]
        chunks = [text] if parts == 1 else [text[:8], text[8:]]
        rows[-1]["content"] = [{"type": "text", "text": chunk,
                                "cache_control": {"type": "ephemeral"}} for chunk in chunks]
        return {**payload, "messages": rows}

    agent, out, _, _, _ = assemble(monkeypatch, transform=text_parts)
    _assert_claim(agent, True)
    assert out.api_kwargs["messages"][-1]["content"][-1]["text"].endswith("PLUGIN")


@pytest.mark.parametrize("format_kind", ["image", "unknown_key", "bad_cache", "wrong_text", "three_parts"])
def test_unrecognized_middleware_content_format_does_not_claim_selected_suffix(monkeypatch, format_kind):
    def transform(payload):
        rows = deepcopy(payload["messages"])
        parts = [{"type": "text", "text": rows[-1]["content"]}]
        if format_kind == "image":
            parts.append({"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}})
        elif format_kind == "unknown_key":
            parts[0]["fixture_extra"] = True
        elif format_kind == "bad_cache":
            parts[0]["cache_control"] = {"type": "ephemeral", "ttl": "invalid"}
        elif format_kind == "wrong_text":
            parts[0]["text"] += " changed"
        else:
            parts += [{"type": "text", "text": ""}] * 2
        rows[-1]["content"] = parts
        return {**payload, "messages": rows}

    agent, _, _, _, _ = assemble(monkeypatch, transform=transform)
    _assert_claim(agent, False)
