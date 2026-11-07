"""C05 negative observation controls: no substring/quote/false ACK evidence."""
import logging

import pytest

from agent import codex_runtime, memory_manager
from agent.turn_context import compose_user_api_content
from tests.agent._local_codex_carry_helpers import inert_codex_boundaries
from tests.agent.test_codex_local_recall_observer import (
    _ack, _request, DELIMITER, PLUGIN, RECALL, SEED, VISIBLE,
)

pytestmark = pytest.mark.usefixtures("inert_codex_boundaries")


@pytest.mark.parametrize("accepted,turn_id,submitted", [
    (False, "accepted-turn", "exact"), (1, "accepted-turn", "exact"),
    ("true", "accepted-turn", "exact"), (True, None, "exact"),
    (True, "  ", "exact"), (True, 7, "exact"),
    (True, "accepted-turn", None), (True, "accepted-turn", 9),
])
def test_refused_or_unverifiable_ack_never_observes(accepted, turn_id, submitted, caplog):
    agent, observation, wire, snapshot = _request(pending=True)
    text = SEED + DELIMITER + wire if submitted == "exact" else submitted
    before = dict(observation)
    with caplog.at_level(logging.INFO, logger="agent.codex_runtime"):
        codex_runtime._observe_codex_recall_append(agent, _ack(text, accepted=accepted, turn_id=turn_id),
                                                    request_snapshot=snapshot)
    assert observation == before
    assert not any("memory-context append" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize("pending,variation", [
    (True, "foreign-prefix"), (True, "missing-prefix"),
    (True, "altered-suffix"), (False, "stale-prefix"),
    (False, "quoted-selected"),
])
def test_valid_ack_with_wrong_full_input_records_mismatch_not_append(pending, variation, caplog):
    agent, observation, wire, snapshot = _request(pending=pending)
    text = {
        "foreign-prefix": "other history" + DELIMITER + wire,
        "missing-prefix": wire,
        "altered-suffix": SEED + DELIMITER + wire + " and another suffix",
        "stale-prefix": SEED + DELIMITER + wire,
        "quoted-selected": VISIBLE + "\n\nThe user quoted " + RECALL,
    }[variation]
    with caplog.at_level(logging.INFO, logger="agent.codex_runtime"):
        codex_runtime._observe_codex_recall_append(agent, _ack(text), request_snapshot=snapshot)
    assert observation["append_logged"] is True
    assert observation["memory_context_appended"] is False
    assert observation["append_failure_reason"] == "input_mismatch"
    assert "memory_context_chars" not in observation
    assert all(payload not in str(observation) for payload in (RECALL, PLUGIN, SEED, text))
    assert all(payload not in record.getMessage() for record in caplog.records
               for payload in (RECALL, PLUGIN, SEED, text))


@pytest.mark.parametrize("change", ["lost", "replacement", "selection-changed", "not-attempted", "failed"])
def test_stale_or_lost_admission_cannot_be_credited(change):
    agent, observation, wire, snapshot = _request()
    if change == "lost":
        agent._last_auto_recall_observation = None
    elif change == "replacement":
        agent._last_auto_recall_observation = dict(observation)
    elif change == "selection-changed":
        agent._auto_recall_context = "different selection"
    elif change == "not-attempted":
        observation["attempted"] = False
    else:
        observation["success"] = False
    before = dict(observation)
    codex_runtime._observe_codex_recall_append(agent, _ack(wire), request_snapshot=snapshot)
    assert observation == before


@pytest.mark.parametrize("case", ["plugin-only", "history-only", "sanitized-empty", "blank-seed", "nonstring-seed", "already-logged"])
def test_ineligible_selection_has_no_capture(case, monkeypatch):
    agent, observation, wire, _ = _request()
    selected, history, pending = RECALL, SEED, False
    if case in ("plugin-only", "history-only"):
        selected = ""
        agent._auto_recall_context = ""
        history = SEED + RECALL
        wire = compose_user_api_content(VISIBLE, "", PLUGIN)
        pending = case == "history-only"
    elif case == "sanitized-empty":
        monkeypatch.setattr(memory_manager, "sanitize_context", lambda value: "")
    elif case == "blank-seed":
        history, pending = "  ", True
    elif case == "nonstring-seed":
        history, pending = {"quoted": SEED}, True
    else:
        observation["append_logged"] = True
    before = dict(observation)
    snapshot = codex_runtime._capture_codex_recall_append(
        agent, selected_recall=selected, composed_input=wire,
        user_message=VISIBLE, plugin_user_context=PLUGIN,
        history_seed=history, seed_pending=pending,
    )
    assert snapshot is None and observation == before
