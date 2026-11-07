"""C09: only acknowledged exact Codex input receives replay provenance."""
from unittest.mock import MagicMock

import pytest

from agent import codex_runtime, turn_context
from agent.codex_runtime_history_seed import render_history_seed
from tests.agent._local_codex_carry_helpers import accepted_turn, inert_codex_boundaries, local_db_agent

pytestmark = pytest.mark.usefixtures("inert_codex_boundaries")


def _run_with_local_turn(agent, monkeypatch, turn_factory, *, recall="", plugin=""):
    memory = MagicMock()
    memory.build_system_prompt.return_value = ""
    memory.wait_for_session_boundary.return_value = True
    memory.prefetch_all.return_value = recall
    memory.describe_recall.return_value = ""
    agent._memory_manager = memory
    monkeypatch.setattr(turn_context, "_maybe_title_session_at_turn_start", lambda *a: None)
    monkeypatch.setattr(turn_context, "_collect_pre_llm_call_context", lambda *a, **kw: plugin)
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda *a: None)

    class LocalSession:
        _history_seed_pending = False
        _history_seed = None

        def ensure_started(self):
            return "fresh-thread"

        def run_turn(self, user_input, **kwargs):
            return turn_factory(user_input)

        def close(self):
            pass

    agent._codex_session = LocalSession()
    return memory


@pytest.mark.parametrize("accepted", [False, True])
def test_current_context_is_persisted_only_after_exact_input_ack(tmp_path, monkeypatch, accepted):
    db, agent = local_db_agent(tmp_path, session_id="ack-current")
    visible, selected = "What is our staging plan?", "reviewed candidate only"
    _run_with_local_turn(agent, monkeypatch,
                         lambda wire: accepted_turn(wire, input_accepted=accepted), recall=selected)
    try:
        assert agent.run_conversation(visible)["completed"] is True
        stored = db.get_messages(agent.session_id)[0]
        assert stored["content"] == visible
        assert (stored["api_content"] is not None) is accepted
        assert ("_codex_history_sidecar" in (stored["display_metadata"] or {})) is accepted
        loaded = db.get_messages_as_conversation(agent.session_id)
        seed = render_history_seed(loaded + [{"role": "user", "content": "next"}])
        assert (selected in seed) is accepted
    finally:
        agent._memory_manager = None
        db.close()


@pytest.mark.parametrize("wire", ["  spaced question \n", "literal <memory-context> tag"])
def test_raw_acknowledged_bytes_survive_normalized_reload(tmp_path, monkeypatch, wire):
    db, agent = local_db_agent(tmp_path, session_id="raw-ack")
    _run_with_local_turn(agent, monkeypatch, accepted_turn)
    try:
        assert agent.run_conversation(wire)["completed"] is True
        raw = db.get_messages(agent.session_id)[0]
        loaded = db.get_messages_as_conversation(agent.session_id)[0]
        assert loaded["content"] != wire
        assert raw["api_content"] == loaded["api_content"] == wire
        seed = render_history_seed([loaded, {"role": "user", "content": "next"}])
        assert wire in seed
    finally:
        agent._memory_manager = None
        db.close()


def test_failed_first_flush_then_ack_writes_first_row_with_provenance(tmp_path, monkeypatch):
    db, agent = local_db_agent(tmp_path, session_id="late-ack")
    visible, recall = "What is the release target?", "approved staging candidate"
    _run_with_local_turn(agent, monkeypatch, accepted_turn, recall=recall)
    original = db.append_messages_batch
    failures = []

    def fail_once(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise OSError("disposable initial INSERT failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "append_messages_batch", fail_once)
    try:
        assert agent.run_conversation(visible)["completed"] is True
        rows = [r for r in db.get_messages(agent.session_id) if r["role"] == "user"]
        assert failures and len(rows) == 1
        assert rows[0]["content"] == visible and recall in rows[0]["api_content"]
        assert rows[0]["display_metadata"]
    finally:
        agent._memory_manager = None
        db.close()


@pytest.mark.parametrize("persist_override", [None, "question"])
def test_clean_retry_after_failed_insert_never_infers_raw_sidecar(tmp_path, monkeypatch, persist_override):
    db, agent = local_db_agent(tmp_path, session_id="clean-retry")
    _run_with_local_turn(agent, monkeypatch, accepted_turn)
    original = db.append_messages_batch
    first = True

    def fail_once(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            raise OSError("disposable initial INSERT failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "append_messages_batch", fail_once)
    raw = "question <memory-context>literal tag</memory-context>"
    try:
        assert agent.run_conversation("question", persist_user_message=persist_override or raw)["completed"] is True
        row = db.get_messages(agent.session_id)[0]
        assert row["api_content"] is None
        loaded = db.get_messages_as_conversation(agent.session_id)[0]
        assert loaded["content"] == "question"
        assert raw not in render_history_seed([loaded, {"role": "user", "content": "next"}])
    finally:
        agent._memory_manager = None
        db.close()


def test_ack_merges_provenance_with_concurrent_durable_reaction(tmp_path, monkeypatch):
    db, agent = local_db_agent(tmp_path, session_id="reaction-merge")
    sid = agent.session_id
    db.create_session(session_id=sid, source="cli", model="gpt-5-codex")
    row_id = db.append_message(sid, "user", "clean question", display_metadata={"original": True})
    live = db.get_messages_as_conversation(sid, include_row_ids=True)[0]
    assert live["_row_id"] == row_id
    db.set_message_reaction(sid, row_id, "👍")

    class LocalSession:
        _history_seed_pending = False
        _history_seed = None

        def ensure_started(self):
            return "fresh-thread"

        def run_turn(self, user_input, **kwargs):
            return accepted_turn(user_input)

        def close(self):
            pass

    agent._codex_session = LocalSession()
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda *a: None)
    try:
        result = codex_runtime.run_codex_app_server_turn(
            agent, user_message="[platform] clean question", original_user_message="clean question",
            messages=[live], effective_task_id="local-reaction",
        )
        assert result["completed"] is True
        stored = db.get_messages(sid)[0]
        assert stored["api_content"] == "[platform] clean question"
        assert "_codex_history_sidecar" in stored["display_metadata"]
        assert stored["display_metadata"]["original"] is True
        assert stored["display_metadata"][db.REACTIONS_METADATA_KEY][0]["emoji"] == "👍"
    finally:
        db.close()


def test_rewritten_raw_row_cannot_replay_old_authenticated_bytes(tmp_path, monkeypatch):
    db, agent = local_db_agent(tmp_path, session_id="raw-collision")
    raw = "question <memory-context>literal tag"
    _run_with_local_turn(agent, monkeypatch, accepted_turn)
    try:
        assert agent.run_conversation(raw)["completed"] is True
        before = db.get_messages(agent.session_id)[0]
        loaded = db.get_messages_as_conversation(agent.session_id)[0]
        assert before["api_content"] == raw and loaded["content"] != raw
        assert raw in render_history_seed([loaded, {"role": "user", "content": "next"}])
        db.set_user_message_content(agent.session_id, before["id"], loaded["content"])
        rewritten = db.get_messages_as_conversation(agent.session_id)[0]
        assert rewritten["content"] == loaded["content"]
        assert raw not in render_history_seed([rewritten, {"role": "user", "content": "next"}])
    finally:
        agent._memory_manager = None
        db.close()


def test_historical_sanitized_sidecars_remain_outside_current_ack_gate(tmp_path):
    db, agent = local_db_agent(tmp_path, session_id="prior-sidecars")
    sid = agent.session_id
    try:
        db.create_session(session_id=sid, source="cli", model="gpt-5-codex")
        agent._session_db_created = True
        agent._persist_user_message_idx = 2
        old_user = "old <memory-context>literal user</memory-context>"
        old_reply = "reply <memory-context>literal assistant</memory-context>"
        rows = [{"role": "user", "content": old_user},
                {"role": "assistant", "content": old_reply},
                {"role": "user", "content": "current"}]
        agent._flush_messages_to_session_db(rows, None)
        stored = db.get_messages(sid)
        assert [r["api_content"] for r in stored] == [old_user, old_reply, None]
        loaded = db.get_messages_as_conversation(sid)
        assert [r.get("api_content") for r in loaded] == [old_user, old_reply, None]
    finally:
        db.close()
