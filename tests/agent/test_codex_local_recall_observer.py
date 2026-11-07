"""C05: a fresh selected recall is observed only after exact composed input ACK."""
import logging
from types import SimpleNamespace
import pytest

from agent import codex_runtime
from agent.turn_context import compose_user_api_content
from tests.agent._local_codex_carry_helpers import inert_codex_boundaries

pytestmark = pytest.mark.usefixtures("inert_codex_boundaries")


RECALL = "<selected-memory>approved staging candidate</selected-memory>"
PLUGIN = "<plugin-note>hold production cutover</plugin-note>"
VISIBLE = "Which candidate is approved?"
SEED = "[QUOTED PRIOR HISTORY]"
DELIMITER = "\n\n[CURRENT USER TURN]\n"


def _request(*, pending=False):
    observation = {"attempted": True, "success": True}
    agent = SimpleNamespace(session_id="local-observer", _last_auto_recall_observation=observation,
                            _auto_recall_context=RECALL)
    composed = compose_user_api_content(VISIBLE, RECALL, PLUGIN)
    snapshot = codex_runtime._capture_codex_recall_append(
        agent, selected_recall=RECALL, composed_input=composed,
        user_message=VISIBLE, plugin_user_context=PLUGIN,
        history_seed=SEED, seed_pending=pending,
    )
    return agent, observation, composed, snapshot


def _ack(text, *, accepted=True, turn_id="accepted-turn"):
    return SimpleNamespace(input_accepted=accepted, submitted_user_text=text, turn_id=turn_id)


def test_exact_fresh_selected_suffix_is_observed_once_without_payload_logging(caplog):
    agent, observation, wire, snapshot = _request(pending=True)
    assert snapshot is not None
    submitted = SEED + DELIMITER + wire
    agent._codex_session = SimpleNamespace(_history_seed_pending=False, _history_seed=None)
    with caplog.at_level(logging.INFO, logger="agent.codex_runtime"):
        codex_runtime._observe_codex_recall_append(agent, _ack(submitted), request_snapshot=snapshot)
        codex_runtime._observe_codex_recall_append(agent, _ack(submitted), request_snapshot=snapshot)
    assert observation["append_logged"] is True
    assert observation["memory_context_appended"] is True
    assert observation["memory_context_chars"] == len(submitted)
    lines = [record.getMessage() for record in caplog.records if "memory-context appended" in record.getMessage()]
    assert len(lines) == 1
    assert all(secret not in str(observation) and all(secret not in line for line in lines)
               for secret in (RECALL, PLUGIN, SEED, submitted))
