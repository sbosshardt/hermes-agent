"""Supplemental preservation: real prologue/compaction insert/RPC ACK/DB reload.

The compactor seam supplies its native in-place outcome; actual SQLite insert,
AIAgent turn loop, CodexAppServerSession ACK handling and persistence stay real.
The process peer and memory provider are inert. These augment the hash-bound
preimplementation RED; no new runtime change is authorized by this file.
"""
from unittest.mock import MagicMock

import pytest

from agent import codex_runtime, turn_context
from agent.context_compressor import _DB_PERSISTED_MARKER
from agent.codex_runtime_history_seed import SIDECAR_PROVENANCE_KEY, render_history_seed
from agent.turn_context import compose_user_api_content
from agent.turn_context_compaction import CompactionOutcome
from tests.agent._local_codex_carry_helpers import (
    InertPeer, inert_codex_boundaries, local_db_agent, local_session,
)

pytestmark = pytest.mark.usefixtures("inert_codex_boundaries")


@pytest.mark.parametrize("row_binding", ["retained", "lost", "stale", "unclaimed"])
@pytest.mark.parametrize("recall", ["", "approved copied-context fact"])
@pytest.mark.parametrize("accepted", [False, True])
def test_preflight_compaction_ack_uses_actual_durable_copy(
    tmp_path, monkeypatch, row_binding, recall, accepted,
):
    db, agent = local_db_agent(tmp_path, session_id="compaction-native-loop")
    sid = agent.session_id
    db.create_session(session_id=sid, source="cli", model="codex")
    agent._session_db_created = True
    archived_id = db.append_message(sid, "user", "older turn") if row_binding == "stale" else None
    visible = "[platform] clean question"
    memory = MagicMock()
    memory.build_system_prompt.return_value = ""
    memory.wait_for_session_boundary.return_value = True
    memory.prefetch_all.return_value = recall
    memory.describe_recall.return_value = ""
    agent._memory_manager = memory
    monkeypatch.setattr(turn_context, "_maybe_title_session_at_turn_start", lambda *a: None)
    monkeypatch.setattr(turn_context, "_collect_pre_llm_call_context", lambda *a, **kw: "")
    peer = InertPeer(ack={"turn": {"id": "accepted-turn"}} if accepted else {"turn": {"id": " "}})
    session = local_session(peer)
    agent._codex_session = session
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda *a: None)
    inserted = []

    def compact_in_place(_agent, *, messages, current_turn_user_idx,
                         active_system_prompt, conversation_history, **_kwargs):
        _agent._last_compaction_in_place = True
        copied = [{"role": "user", "content": visible, _DB_PERSISTED_MARKER: True}]
        db.archive_and_compact(sid, copied)
        inserted.append(copied[0]["_row_id"])
        if row_binding != "retained":
            copied[0].pop("_row_id")
        if row_binding == "stale":
            copied[0]["_row_id"] = archived_id
        return CompactionOutcome(copied, active_system_prompt, conversation_history, 0,
                                 compressed=row_binding != "unclaimed")

    monkeypatch.setattr("agent.turn_context_compaction.run_turn_start_compaction", compact_in_place)
    try:
        result = agent.run_conversation(visible, persist_user_message="clean question")
        assert result["completed"] is accepted
        submitted = [p for m, p in peer.requests if m == "turn/start"]
        wire = compose_user_api_content(visible, recall, "") or visible
        assert len(submitted) == 1 and submitted[0]["input"] == [{"type": "text", "text": wire}]
        rows = [r for r in db.get_messages(sid) if r["role"] == "user"]
        assert len(inserted) == len(rows) == 1
        assert rows[0]["id"] == inserted[0] and rows[0]["content"] == visible
        has_sidecar = accepted and bool(recall) and row_binding in {"retained", "lost"}
        assert rows[0]["api_content"] == (wire if has_sidecar else None)
        assert (SIDECAR_PROVENANCE_KEY in (rows[0]["display_metadata"] or {})) is has_sidecar
        loaded = db.get_messages_as_conversation(sid)
        assert loaded[0].get("api_content") == (wire if has_sidecar else None)
        if recall:
            seed = render_history_seed(loaded + [{"role": "user", "content": "next"}])
            assert (recall in seed) is has_sidecar
    finally:
        session.close()
        agent._memory_manager = None
        db.close()


@pytest.mark.parametrize("sanitize_raw", [False, True])
def test_non_codex_current_row_keeps_ordinary_sidecar_inference(tmp_path, sanitize_raw):
    db, agent = local_db_agent(tmp_path, session_id="ordinary-preservation")
    agent.api_mode = "chat_completions"
    agent._persist_user_message_idx = 0
    raw = "clean question <memory-context>literal user data</memory-context>"
    agent._persist_user_message_override = None if sanitize_raw else "clean question"
    try:
        agent._flush_messages_to_session_db([{"role": "user", "content": raw}], None)
        rows = db.get_messages(agent.session_id)
        assert len(rows) == 1 and rows[0]["api_content"] == raw
        assert rows[0]["content"] == (raw if sanitize_raw else "clean question")
        assert SIDECAR_PROVENANCE_KEY not in (rows[0]["display_metadata"] or {})
        loaded = db.get_messages_as_conversation(agent.session_id)
        assert loaded[0]["api_content"] == raw
        assert loaded[0]["content"] != raw
    finally:
        db.close()
