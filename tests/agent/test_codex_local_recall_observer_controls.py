"""Supplemental contracts for the already-green string ACK observer.

Exercise the real capture/observer/composition helpers with local data fixtures.
These controls are NOT external Codex SDK/server acceptance or full run-loop
fault injection. The separate vertical tracer covers actual native turn wiring;
existing transport/history/row suites cover the underlying native ACK contracts.
"""
import logging
from types import SimpleNamespace

import pytest

from agent import codex_runtime, memory_manager
from agent.turn_context import compose_user_api_content


RECALL = "<selected-memory>retain the reviewed release target</selected-memory>"
PLUGIN = "<plugin-note>production remains held</plugin-note>"
VISIBLE = "What is the release target?"
SEED = "<quoted-history>previous release discussion</quoted-history>"
DELIMITER = "\n\n[CURRENT USER TURN]\n"


def _request(*, seed_pending=False, seed=SEED, plugin=PLUGIN):
    observation = {"attempted": True, "success": True}
    agent = SimpleNamespace(session_id="isolated-observer-controls",
                            _last_auto_recall_observation=observation,
                            _auto_recall_context=RECALL)
    wire = compose_user_api_content(VISIBLE, RECALL, plugin)
    assert isinstance(wire, str)
    snapshot = codex_runtime._capture_codex_recall_append(
        agent, selected_recall=RECALL, composed_input=wire,
        user_message=VISIBLE, plugin_user_context=plugin,
        history_seed=seed, seed_pending=seed_pending,
    )
    return agent, observation, wire, snapshot


def _ack(text, *, accepted=True, turn_id="accepted-turn"):
    return SimpleNamespace(input_accepted=accepted, turn_id=turn_id,
                           submitted_user_text=text)


@pytest.mark.parametrize("pending", [False, True])
def test_exact_ack_uses_request_local_seed_snapshot(pending, caplog):
    agent, observation, wire, snapshot = _request(seed_pending=pending)
    assert snapshot is not None
    submitted = SEED + DELIMITER + wire if pending else wire
    # A transport's one-shot state may be consumed before it returns its ACK.
    agent._codex_session = SimpleNamespace(_history_seed_pending=False, _history_seed=None)
    with caplog.at_level(logging.INFO, logger="agent.codex_runtime"):
        codex_runtime._observe_codex_recall_append(agent, _ack(submitted), request_snapshot=snapshot)
        codex_runtime._observe_codex_recall_append(agent, _ack(submitted), request_snapshot=snapshot)
    assert observation["append_logged"] is True
    assert observation["memory_context_appended"] is True
    assert observation["memory_context_chars"] == len(submitted)
    logs = [record.getMessage() for record in caplog.records
            if "memory-context appended" in record.getMessage()]
    assert len(logs) == 1
    assert all(payload not in str(observation) and all(payload not in line for line in logs)
               for payload in [RECALL, PLUGIN, SEED, submitted])


@pytest.mark.parametrize("accepted,turn_id,submitted_kind", [
    (False, "accepted-turn", "exact"),
    (1, "accepted-turn", "exact"),
    ("true", "accepted-turn", "exact"),
    (True, None, "exact"),
    (True, "   ", "exact"),
    (True, 7, "exact"),
    (True, "accepted-turn", "nonstring"),
    (True, "accepted-turn", "missing"),
])
def test_malformed_or_refused_ack_does_not_observe(accepted, turn_id, submitted_kind, caplog):
    agent, observation, wire, snapshot = _request(seed_pending=True)
    submitted = {"exact": SEED + DELIMITER + wire, "nonstring": 7, "missing": None}[submitted_kind]
    before = dict(observation)
    with caplog.at_level(logging.INFO, logger="agent.codex_runtime"):
        codex_runtime._observe_codex_recall_append(
            agent, _ack(submitted, accepted=accepted, turn_id=turn_id), request_snapshot=snapshot,
        )
    assert observation == before
    assert not any("memory-context append" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("pending,submission", [
    (True, "foreign-prefix"),
    (True, "missing-prefix"),
    (True, "altered-suffix"),
    (False, "stale-prefix"),
])
def test_valid_ack_with_wrong_entire_input_records_only_mismatch(pending, submission, caplog):
    agent, observation, wire, snapshot = _request(seed_pending=pending)
    submitted = {
        "foreign-prefix": "<quoted-history>foreign</quoted-history>" + DELIMITER + wire,
        "missing-prefix": wire,
        "altered-suffix": SEED + DELIMITER + wire + " unexpected suffix",
        "stale-prefix": SEED + DELIMITER + wire,
    }[submission]
    with caplog.at_level(logging.INFO, logger="agent.codex_runtime"):
        codex_runtime._observe_codex_recall_append(agent, _ack(submitted), request_snapshot=snapshot)
    assert observation["append_logged"] is True
    assert observation["memory_context_appended"] is False
    assert observation["append_failure_reason"] == "input_mismatch"
    assert "memory_context_chars" not in observation
    logs = [record.getMessage() for record in caplog.records if "memory-context append" in record.getMessage()]
    assert len(logs) == 1 and "reason=input_mismatch" in logs[0]
    assert all(payload not in str(observation) and payload not in logs[0]
               for payload in [RECALL, PLUGIN, SEED, submitted])


@pytest.mark.parametrize("mutation", ["lost", "replaced", "changed-selection", "attempt-lost", "selection-failed"])
def test_lost_or_stale_admission_cannot_receive_ack_observation(mutation, caplog):
    agent, observation, wire, snapshot = _request()
    mutations = {
        "lost": lambda: setattr(agent, "_last_auto_recall_observation", None),
        "replaced": lambda: setattr(agent, "_last_auto_recall_observation", dict(observation)),
        "changed-selection": lambda: setattr(agent, "_auto_recall_context", "different selected recall"),
        "attempt-lost": lambda: observation.update(attempted=False),
        "selection-failed": lambda: observation.update(success=False),
    }
    mutations[mutation]()
    before = dict(observation)
    replacement = agent._last_auto_recall_observation
    replacement_before = dict(replacement) if isinstance(replacement, dict) else replacement
    with caplog.at_level(logging.INFO, logger="agent.codex_runtime"):
        codex_runtime._observe_codex_recall_append(agent, _ack(wire), request_snapshot=snapshot)
    assert observation == before
    assert replacement == replacement_before
    assert not any("memory-context append" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("case", ["plugin-only", "historical-only", "empty-after-sanitization", "blank-seed", "nonstring-seed", "already-logged"])
def test_ineligible_fresh_recall_has_no_request_snapshot(case, monkeypatch):
    agent, observation, wire, _ = _request()
    selected = RECALL
    seed = SEED
    pending = case in {"historical-only", "blank-seed", "nonstring-seed"}
    if case in {"plugin-only", "historical-only"}:
        selected = ""
        agent._auto_recall_context = ""
        seed = SEED + RECALL
        wire = compose_user_api_content(VISIBLE, selected, PLUGIN)
    elif case == "empty-after-sanitization":
        monkeypatch.setattr(memory_manager, "sanitize_context", lambda context: "")
    elif case == "blank-seed":
        seed = "  "
    elif case == "nonstring-seed":
        seed = {"quoted": SEED}
    else:
        observation["append_logged"] = True
    before = dict(observation)
    snapshot = codex_runtime._capture_codex_recall_append(
        agent, selected_recall=selected, composed_input=wire,
        user_message=VISIBLE, plugin_user_context=PLUGIN,
        history_seed=seed, seed_pending=pending,
    )
    assert snapshot is None
    assert observation == before


@pytest.mark.parametrize("plugin", [None, 9, {"not": "text"}, ["not text"]])
def test_malformed_optional_plugin_keeps_shared_string_composition(plugin):
    agent, observation, wire, snapshot = _request(plugin=plugin)
    assert snapshot is not None
    codex_runtime._observe_codex_recall_append(agent, _ack(wire), request_snapshot=snapshot)
    assert observation["memory_context_appended"] is True
    assert observation["memory_context_chars"] == len(wire)
