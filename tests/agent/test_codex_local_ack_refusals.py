"""C08/C09 refusal controls: invalid ACK, row identity and compaction."""
import pytest

from agent.turn_context import compose_user_api_content
from tests.agent._local_codex_carry_helpers import accepted_turn, inert_codex_boundaries, local_db_agent
from tests.agent.test_codex_local_ack_persistence import _run_with_local_turn

pytestmark = pytest.mark.usefixtures("inert_codex_boundaries")


@pytest.mark.parametrize("mutation", [
    {"input_accepted": False}, {"input_accepted": None}, {"input_accepted": 1},
    {"input_accepted": "true"}, {"turn_id": None}, {"turn_id": ""},
    {"turn_id": "  "}, {"turn_id": 7}, {"submitted_user_text": None},
    {"submitted_user_text": "foreign input"}, {"submitted_user_text": ["not text"]},
])
def test_invalid_ack_never_authorizes_current_context(tmp_path, monkeypatch, mutation):
    db, agent = local_db_agent(tmp_path, session_id="invalid-ack")
    recall = "private selected history"
    _run_with_local_turn(agent, monkeypatch,
                         lambda wire: accepted_turn(wire, **mutation), recall=recall)
    try:
        agent.run_conversation("What was approved?")
        user = [r for r in db.get_messages(agent.session_id) if r["role"] == "user"]
        assert len(user) == 1 and user[0]["api_content"] is None
        assert "_codex_history_sidecar" not in (user[0]["display_metadata"] or {})
    finally:
        agent._memory_manager = None
        db.close()


@pytest.mark.parametrize("guard", ["other-session", "wrong-role", "archived", "rewritten", "invalid-id"])
def test_native_row_id_writer_rejects_unauthorized_equal_text_fallback(tmp_path, guard):
    db, agent = local_db_agent(tmp_path, session_id="row-owner")
    try:
        sid, other, content = agent.session_id, "other-owner", "same repeated text"
        db.create_session(session_id=sid, source="cli", model="codex")
        db.create_session(session_id=other, source="cli", model="codex")
        old_id = db.append_message(sid, "user", content)
        row_id = old_id
        if guard == "other-session":
            row_id = db.append_message(other, "user", content)
        elif guard == "wrong-role":
            row_id = db.append_message(sid, "assistant", content)
        elif guard == "archived":
            db.archive_and_compact(sid, [{"role": "user", "content": content}])
        elif guard == "rewritten":
            db.set_user_message_content(sid, old_id, "rewritten")
        elif guard == "invalid-id":
            row_id = True
        db.append_message(sid, "user", content)  # equal-text neighbour is not the ACK's row
        before = db.get_messages(sid, include_inactive=True)
        assert db.set_message_api_content(sid, row_id, content, "unsent replay text") == 0
        assert db.get_messages(sid, include_inactive=True) == before
    finally:
        db.close()


def test_native_in_place_compaction_retained_id_updates_only_compacted_row(tmp_path):
    db, agent = local_db_agent(tmp_path, session_id="compacted-row")
    try:
        sid = agent.session_id
        db.create_session(session_id=sid, source="cli", model="codex")
        old = db.append_message(sid, "user", "old turn")
        copied = {"role": "user", "content": "current turn"}
        db.archive_and_compact(sid, [copied])
        current = copied["_row_id"]
        assert db.set_message_api_content(sid, old, "old turn", "wrong") == 0
        wire = compose_user_api_content("current turn", "new selected fact", "")
        assert db.set_message_api_content(sid, current, "current turn", wire) == 1
        assert db.get_messages(sid)[0]["api_content"] == wire
    finally:
        db.close()
