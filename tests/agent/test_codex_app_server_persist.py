"""Regression for #49225 — codex app-server turns must reach the session DB
exactly once.

The codex app-server runtime (``run_codex_app_server_turn``) is an early-return
path that bypasses ``conversation_loop`` and therefore never runs the loop's
per-step ``_persist_session()`` flushes. Before the fix, the projected
assistant/tool messages were persisted *nowhere* (state.db got only
session_meta rows), leaving ``session_search`` (FTS) and conversation-distill
blind to real gateway conversations.

The fix has the codex runtime flush its own projected messages via
``_flush_messages_to_session_db()`` (idempotent through the intrinsic
``_DB_PERSISTED_MARKER``) and return ``agent_persisted=True`` so the gateway
skips its own ``append_to_transcript`` DB write. This is critical: the inbound
user turn is already flushed at turn start (``turn_context._persist_session``),
and ``append_message`` is a raw INSERT with no dedup — a gateway re-write would
duplicate the user turn (#860 / #42039). This test locks in:

1. ``run_codex_app_server_turn`` flushes projected messages and returns
   ``agent_persisted=True``.
2. Exactly-once persistence: the already-flushed user turn is NOT re-written,
   and the new projected assistant message lands once.
3. The gateway resolution expression preserves standard-runtime behaviour.
"""

import tempfile
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from agent.codex_runtime import run_codex_app_server_turn
from agent.turn_context import compose_user_api_content
from hermes_state import SessionDB
from run_agent import AIAgent


def _make_turn():
    return SimpleNamespace(
        interrupted=False,
        error=None,
        thread_id="thread-1",
        turn_id="turn-1",
        projected_messages=[{"role": "assistant", "content": "CODEX_ASSISTANT"}],
        tool_iterations=0,
        final_text="CODEX_ASSISTANT",
        should_retire=False,
    )


def _make_agent(session_db=None, session_id="sess-codex"):
    agent = MagicMock()
    # Pre-seed the session so run_codex_app_server_turn skips the spawn block.
    agent._codex_session = MagicMock()
    agent._codex_session.run_turn.return_value = _make_turn()
    agent._codex_session_prompt = None  # seeded session: no recorded composition to compare
    agent.tool_progress_callback = None
    agent._iters_since_skill = 0
    agent._skill_nudge_interval = 0
    agent.valid_tool_names = set()
    agent._session_db = session_db
    agent._session_db_created = True
    agent.session_id = session_id
    return agent


def test_codex_success_flushes_and_reports_persisted():
    """Codex success turn must self-persist and return agent_persisted=True."""
    agent = _make_agent(session_db=None)  # no DB -> flush is a no-op, still True
    result = run_codex_app_server_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task-1",
    )
    assert result["completed"] is True
    assert isinstance(result["messages"][-1]["timestamp"], float)
    # With the agent as sole persister, the gateway must SKIP its DB write.
    assert result["agent_persisted"] is True


def test_codex_user_interrupt_is_reported_and_cleared():
    agent = _make_agent(session_db=None)
    turn = _make_turn()
    turn.interrupted = True
    turn.final_text = ""
    agent._codex_session.run_turn.return_value = turn
    agent._interrupt_requested = True
    agent._interrupt_message = "new correction"

    def clear_interrupt():
        agent._interrupt_requested = False
        agent._interrupt_message = None

    agent.clear_interrupt.side_effect = clear_interrupt
    result = run_codex_app_server_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task-1",
    )

    assert result["interrupted"] is True
    assert result["interrupt_message"] == "new correction"
    agent.clear_interrupt.assert_called_once_with()
    assert agent._interrupt_requested is False


def test_codex_turn_persists_each_message_exactly_once():
    """The user turn (flushed at turn start) must not be duplicated; the
    projected assistant message must land once.  Uses a real SessionDB and the
    real AIAgent._flush_messages_to_session_db to prove no #860/#42039
    duplicate-write regression on the codex path."""
    tmp = tempfile.mkdtemp(prefix="codex_persist_")
    db = None
    try:
        db = SessionDB(Path(tmp) / "state.db")
        sid = "sess-codex-once"
        db.create_session(session_id=sid, source="telegram", model="codex")

        # Real agent bound to this DB/session, minimal construction.
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=db,
            session_id=sid,
        )
        agent._session_db_created = True
        agent._codex_session = MagicMock()
        agent._codex_session.run_turn.return_value = _make_turn()
        agent.tool_progress_callback = None

        # Model the real flow: the inbound user turn is flushed at turn start
        # (turn_context._persist_session) on the SAME `messages` list the codex
        # path later reuses. That flush stamps _DB_PERSISTED_MARKER on the user
        # dict, so the codex-path flush skips it — no duplicate.
        user_msg = {"role": "user", "content": "USER_TURN"}
        messages = [user_msg]
        agent._flush_messages_to_session_db(messages)  # turn-start flush

        result = run_codex_app_server_turn(
            agent,
            user_message="USER_TURN",
            original_user_message="USER_TURN",
            messages=messages,
            effective_task_id="task-1",
        )
        assert result["agent_persisted"] is True

        rows = db.get_messages(sid, include_inactive=True)
        contents = [r["content"] for r in rows]
        # Exactly one user turn, exactly one assistant turn — no duplicates.
        assert contents.count("USER_TURN") == 1, contents
        assert contents.count("CODEX_ASSISTANT") == 1, contents
        assistant_row = next(
            row for row in rows if row["content"] == "CODEX_ASSISTANT"
        )
        assert isinstance(assistant_row["timestamp"], float)
        # session_search can now see the codex conversation.
        hits = {r["session_id"] for r in db.search_messages("CODEX_ASSISTANT")}
        assert sid in hits
    finally:
        import shutil

        if db is not None:
            db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_codex_sidecar_is_durable_only_after_turn_start_accepts_wire_input(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-ack"
        db.create_session(session_id=sid, source="cli", model="codex")
        agent = _make_agent(db, sid)
        agent._persist_user_message_override = None
        agent._session_persist_lock = None
        row = {"role": "user", "content": "clean question"}
        db.append_message(sid, "user", "clean question")
        row["_row_id"] = db.get_messages(sid)[-1]["id"]
        wire = compose_user_api_content("[platform] clean question", "<selected-memory>fact</selected-memory>", "")
        turn = _make_turn()
        turn.input_accepted = False
        agent._codex_session.run_turn.return_value = turn
        run_codex_app_server_turn(agent, user_message="[platform] clean question",
                                  original_user_message="clean question", messages=[row],
                                  effective_task_id="task", ext_prefetch_cache="<selected-memory>fact</selected-memory>")
        assert agent._codex_session.run_turn.call_args.kwargs["user_input"] == wire
        assert row.get("api_content") is None
        assert db.get_messages(sid)[0]["api_content"] is None

        turn.input_accepted = True
        agent._codex_session.run_turn.return_value = turn
        run_codex_app_server_turn(agent, user_message="[platform] clean question",
                                  original_user_message="clean question", messages=[row],
                                  effective_task_id="task", ext_prefetch_cache="<selected-memory>fact</selected-memory>")
        assert row["api_content"] == wire
        assert db.get_messages(sid)[0]["api_content"] == wire
        from agent.codex_runtime_history_seed import render_history_seed
        loaded = db.get_messages_as_conversation(sid)
        seed = render_history_seed(loaded + [{"role": "assistant", "content": "done"},
                                             {"role": "user", "content": "next"}])
        assert "<selected-memory>fact</selected-memory>" in seed
        assert len([r for r in db.get_messages(sid) if r["role"] == "user"]) == 1
    finally:
        db.close()


def test_compacted_codex_row_backfills_sidecar_and_provenance_together(tmp_path):
    from agent.codex_runtime_history_seed import render_history_seed
    from agent.turn_context import _stamp_api_content_sidecar

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-compacted-ack"
        db.create_session(session_id=sid, source="cli", model="codex")
        db.append_message(sid, "user", "clean question")
        agent = _make_agent(db, sid)
        agent._persist_user_message_override = None
        agent._session_persist_lock = None
        agent._last_compaction_in_place = True
        row = {"role": "user", "content": "clean question"}
        wire = "[platform] clean question\n\n<selected-memory>fact</selected-memory>"
        _stamp_api_content_sidecar(agent, [row], 0, "", "", preflight_compressed=True,
                                   wire_content=wire)
        assert db.get_messages(sid)[0]["api_content"] == wire
        loaded = db.get_messages_as_conversation(sid)
        seed = render_history_seed(loaded + [{"role": "assistant", "content": "done"},
                                             {"role": "user", "content": "next"}])
        assert "<selected-memory>fact</selected-memory>" in seed
    finally:
        db.close()


@pytest.mark.parametrize("live,clean", [
    ("[platform] clean question", "clean question"),
    ("literal <memory-context> tag", None),
])
def test_codex_pre_submit_flush_never_claims_unsent_api_content(tmp_path, live, clean):
    """A failed turn/start must not leave either inferred sidecar in the DB."""
    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-pending"
        db.create_session(session_id=sid, source="cli", model="codex")
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        session_db=db, session_id=sid)
        agent.api_mode = "codex_app_server"
        agent._session_db_created = True
        agent._persist_user_message_idx = 0
        agent._persist_user_message_override = clean
        row = {"role": "user", "content": live}
        agent._flush_messages_to_session_db([row], None)
        persisted = db.get_messages(sid)[0]
        assert persisted["content"] == (clean or live)
        assert persisted["api_content"] is None
        agent._codex_session = MagicMock()
        agent._codex_session.run_turn.side_effect = RuntimeError("turn/start rejected")
        result = run_codex_app_server_turn(agent, user_message=live,
                                            original_user_message=clean or live, messages=[row],
                                            effective_task_id="task")
        assert result["completed"] is False
        assert db.get_messages(sid)[0]["api_content"] is None
        assert db.get_messages_as_conversation(sid)[0].get("api_content") is None
    finally:
        db.close()


def test_codex_ack_persists_wire_and_provenance_after_pending_clean_flush(tmp_path):
    from agent.codex_runtime_history_seed import SIDECAR_PROVENANCE_KEY

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-clean-ack"
        db.create_session(session_id=sid, source="cli", model="codex")
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        session_db=db, session_id=sid)
        agent.api_mode = "codex_app_server"
        agent._session_db_created = True
        agent._persist_user_message_idx = 0
        agent._persist_user_message_override = "clean question"
        agent._codex_session = MagicMock()
        row = {"role": "user", "content": "[platform] clean question"}
        agent._flush_messages_to_session_db([row], None)
        assert db.get_messages(sid)[0]["api_content"] is None
        turn = _make_turn()
        turn.input_accepted = True
        agent._codex_session.run_turn.return_value = turn
        result = run_codex_app_server_turn(agent, user_message=row["content"],
                                            original_user_message="clean question", messages=[row],
                                            effective_task_id="task", ext_prefetch_cache="recalled fact")
        wire = agent._codex_session.run_turn.call_args.kwargs["user_input"]
        assert result["completed"] is True
        stored = db.get_messages(sid)[0]
        assert stored["api_content"] == wire
        assert SIDECAR_PROVENANCE_KEY in stored["display_metadata"]
        assert db.get_messages_as_conversation(sid)[0]["api_content"] == wire
    finally:
        db.close()


@pytest.mark.parametrize("wire", [
    "what does a literal <memory-context> tag do?",
    "  spaced question \n",
])
def test_codex_ack_preserves_raw_wire_when_replay_normalizes_visible_text(tmp_path, wire):
    from agent.codex_runtime_history_seed import render_history_seed

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-normalized-ack"
        db.create_session(session_id=sid, source="cli", model="codex")
        agent = AIAgent(api_key="test-key", base_url="https://stub.invalid",
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        session_db=db, session_id=sid)
        agent.api_mode = "codex_app_server"
        agent._session_db_created = True
        agent._persist_user_message_idx = 0
        agent._codex_session = MagicMock()
        row = {"role": "user", "content": wire}
        agent._flush_messages_to_session_db([row], None)
        assert db.get_messages(sid)[0]["api_content"] is None
        assert db.get_messages_as_conversation(sid)[0].get("api_content") is None

        turn = _make_turn()
        turn.input_accepted = True
        agent._codex_session.run_turn.return_value = turn
        result = run_codex_app_server_turn(agent, user_message=wire,
                                            original_user_message=wire, messages=[row],
                                            effective_task_id="task")
        assert result["completed"] is True
        assert agent._codex_session.run_turn.call_args.kwargs["user_input"] == wire
        stored = db.get_messages(sid)[0]
        loaded = db.get_messages_as_conversation(sid)[0]
        assert loaded["content"] != wire
        assert stored["api_content"] == loaded["api_content"] == wire
        seed = render_history_seed([loaded, {"role": "assistant", "content": "ok"},
                                    {"role": "user", "content": "next"}])
        assert "[WIRE INPUT SENT WITH THIS PRIOR TURN" in seed
        assert wire in seed
    finally:
        db.close()


def test_codex_history_seed_rejects_provenance_after_raw_content_collision(tmp_path):
    from agent.codex_runtime_history_seed import render_history_seed

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-raw-collision"
        db.create_session(session_id=sid, source="cli", model="codex")
        raw = "question <memory-context>literal tag"
        row_id = db.append_message(sid, "user", raw)
        agent = _make_agent(db, sid)
        agent._persist_user_message_override = None
        agent._session_persist_lock = None
        row = {"role": "user", "content": raw, "_row_id": row_id}
        from agent.turn_context import _stamp_api_content_sidecar
        _stamp_api_content_sidecar(agent, [row], 0, "", "", preflight_compressed=False,
                                   wire_content=raw)
        loaded = db.get_messages_as_conversation(sid)[0]
        assert loaded["content"] != raw
        assert raw in render_history_seed([loaded, {"role": "assistant", "content": "ok"},
                                           {"role": "user", "content": "next"}])

        # Different raw content maps to the same replay view. A provenance
        # match solely against normalized text would leak the old sidecar.
        db.set_user_message_content(sid, row_id, loaded["content"])
        rewritten = db.get_messages_as_conversation(sid)[0]
        assert rewritten["content"] == loaded["content"]
        seed = render_history_seed([rewritten, {"role": "assistant", "content": "ok"},
                                    {"role": "user", "content": "next"}])
        assert raw not in seed
    finally:
        db.close()


@pytest.mark.parametrize("accepted,keeps_sidecar", [
    ("clean question", False),
    ("  clean question \n", True),
])
def test_codex_clean_retry_clears_stale_sidecar_only_when_equivalent_on_reload(
    tmp_path, accepted, keeps_sidecar,
):
    from agent.codex_runtime_history_seed import SIDECAR_PROVENANCE_KEY, sidecar_provenance
    from agent.turn_context import _stamp_api_content_sidecar

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-normalized-clean-retry"
        db.create_session(session_id=sid, source="cli", model="codex")
        raw = "  clean question \n"
        old_wire = "old wire\n\nold memory"
        row_id = db.append_message(sid, "user", raw, api_content=old_wire,
                                   display_metadata={SIDECAR_PROVENANCE_KEY:
                                                     sidecar_provenance(raw, old_wire)})
        agent = _make_agent(db, sid)
        agent._persist_user_message_override = None
        agent._session_persist_lock = None
        row = {"role": "user", "content": raw, "_row_id": row_id,
               "api_content": old_wire,
               "display_metadata": {SIDECAR_PROVENANCE_KEY: sidecar_provenance(raw, old_wire)}}
        _stamp_api_content_sidecar(agent, [row], 0, "", "", preflight_compressed=False,
                                   wire_content=accepted)
        assert row.get("api_content") == (accepted if keeps_sidecar else None)
        assert (SIDECAR_PROVENANCE_KEY in row.get("display_metadata", {})) == keeps_sidecar
        loaded = db.get_messages_as_conversation(sid)[0]
        assert loaded["content"] == "clean question"
        assert loaded.get("api_content") == (accepted if keeps_sidecar else None)
        assert (SIDECAR_PROVENANCE_KEY in loaded.get("display_metadata", {})) == keeps_sidecar
    finally:
        db.close()


@pytest.mark.parametrize("preflight_compressed", [False, True])
def test_codex_ack_backfills_in_place_compaction_copy_without_row_id(tmp_path, preflight_compressed):
    from agent.codex_runtime_history_seed import SIDECAR_PROVENANCE_KEY

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-compacted-runtime"
        db.create_session(session_id=sid, source="cli", model="codex")
        db.append_message(sid, "user", "clean question")
        agent = _make_agent(db, sid)
        agent._persist_user_message_override = None
        agent._session_persist_lock = None
        agent._last_compaction_in_place = True
        turn = _make_turn()
        turn.input_accepted = True
        agent._codex_session.run_turn.return_value = turn
        row = {"role": "user", "content": "clean question"}  # compacted copy: no _row_id
        run_codex_app_server_turn(agent, user_message="[platform] clean question",
                                  original_user_message="clean question", messages=[row],
                                  effective_task_id="task", preflight_compressed=preflight_compressed)
        stored = db.get_messages(sid)[0]
        if preflight_compressed:
            assert stored["api_content"] == "[platform] clean question"
            assert SIDECAR_PROVENANCE_KEY in stored["display_metadata"]
            assert db.get_messages_as_conversation(sid)[0]["api_content"] == stored["api_content"]
        else:
            # A row-id-less user dict alone must never authorize positional backfill.
            assert stored["api_content"] is None
            assert not (stored["display_metadata"] or {}).get(SIDECAR_PROVENANCE_KEY)
    finally:
        db.close()


@pytest.mark.parametrize("row_id_retained", [True, False])
def test_codex_ack_backfills_actual_in_place_compacted_content_despite_clean_override(tmp_path, row_id_retained):
    from agent.codex_runtime_history_seed import SIDECAR_PROVENANCE_KEY, render_history_seed, sidecar_provenance

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-real-compaction-override"
        db.create_session(session_id=sid, source="cli", model="codex")
        db.append_message(sid, "user", "old turn")
        live = "[platform] clean question"
        clean = "clean question"
        # The real compactor inserts the copied live message, not the normal
        # flush's clean persist override. The insert may stamp its row id.
        copied = {"role": "user", "content": live}
        db.archive_and_compact(sid, [copied])
        assert db.get_messages(sid)[0]["content"] == live
        if not row_id_retained:
            copied.pop("_row_id", None)  # marker-swept handoff can lose the id
        agent = _make_agent(db, sid)
        agent._persist_user_message_override = clean
        agent._session_persist_lock = None
        agent._last_compaction_in_place = True
        turn = _make_turn()
        turn.input_accepted = True
        agent._codex_session.run_turn.return_value = turn
        result = run_codex_app_server_turn(
            agent, user_message=live, original_user_message=clean, messages=[copied],
            effective_task_id="task", preflight_compressed=True,
            ext_prefetch_cache="<selected-memory>fact</selected-memory>",
        )
        wire = agent._codex_session.run_turn.call_args.kwargs["user_input"]
        assert result["completed"] is True
        stored = db.get_messages(sid)[0]
        assert stored["content"] == live
        assert stored["api_content"] == wire
        assert stored["display_metadata"][SIDECAR_PROVENANCE_KEY] == sidecar_provenance(live, wire)
        loaded = db.get_messages_as_conversation(sid)[0]
        seed = render_history_seed([loaded, {"role": "assistant", "content": "ok"},
                                    {"role": "user", "content": "next"}])
        assert "<selected-memory>fact</selected-memory>" in seed
        assert "[CONTEXT SENT WITH THIS PRIOR TURN" in seed
    finally:
        db.close()


@pytest.mark.parametrize("stale_row_id", [True, False])
def test_codex_ack_stale_compacted_row_id_does_not_stamp_newest_row(tmp_path, stale_row_id):
    from agent.codex_runtime_history_seed import SIDECAR_PROVENANCE_KEY
    from agent.turn_context import _stamp_api_content_sidecar

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-stale-compacted-id"
        db.create_session(session_id=sid, source="cli", model="codex")
        old_id = db.append_message(sid, "user", "[platform] clean question")
        db.archive_and_compact(sid, [{"role": "user", "content": (
            "[platform] clean question" if stale_row_id else "another turn")}])
        agent = _make_agent(db, sid)
        agent._persist_user_message_override = "clean question"
        agent._session_persist_lock = None
        agent._last_compaction_in_place = True
        row: dict[str, Any] = {"role": "user", "content": "[platform] clean question"}
        if stale_row_id:
            row["_row_id"] = old_id
        _stamp_api_content_sidecar(agent, [row], 0, "", "", preflight_compressed=True,
                                   wire_content="[platform] clean question\n\nrecalled fact")
        stored = db.get_messages(sid)[0]
        assert stored["api_content"] is None
        assert SIDECAR_PROVENANCE_KEY not in (stored["display_metadata"] or {})
        assert row.get("api_content") is None
        assert SIDECAR_PROVENANCE_KEY not in (row.get("display_metadata") or {})
    finally:
        db.close()


def test_codex_conversation_compaction_ack_backfills_row_idless_copy(tmp_path, monkeypatch):
    """Carry a preflight compaction result through the real turn loop and Codex runtime."""
    from agent.codex_runtime_history_seed import SIDECAR_PROVENANCE_KEY
    from agent.turn_context_compaction import CompactionOutcome
    from agent.context_compressor import _DB_PERSISTED_MARKER

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-compaction-loop"
        db.create_session(session_id=sid, source="cli", model="codex")
        agent = AIAgent(api_key="test-key", base_url="https://stub.invalid", provider="openai",
                        api_mode="codex_app_server", quiet_mode=True, skip_context_files=True,
                        skip_memory=True, session_db=db, session_id=sid)
        agent._session_db_created = True
        agent._codex_session = MagicMock()
        turn = _make_turn()
        turn.input_accepted = True
        agent._codex_session.run_turn.return_value = turn
        agent._codex_session.ensure_started.return_value = "thread-1"

        def compact_in_place(_agent, *, messages, current_turn_user_idx, active_system_prompt,
                             conversation_history, **_kwargs):
            # Model the compactor's actual in-place write of copied live
            # content; archive_and_compact stamps the new durable row id.
            _agent._last_compaction_in_place = True
            copied = [{"role": "user", "content": "[platform] clean question",
                       _DB_PERSISTED_MARKER: True}]
            db.archive_and_compact(sid, copied)
            return CompactionOutcome(copied, active_system_prompt, conversation_history, 0,
                                     compressed=True)

        monkeypatch.setattr("agent.turn_context_compaction.run_turn_start_compaction", compact_in_place)
        result = agent.run_conversation("[platform] clean question",
                                        persist_user_message="clean question")
        assert result["completed"] is True
        stored = db.get_messages(sid)[0]
        assert stored["content"] == "[platform] clean question"
        assert stored["api_content"] is None
        assert agent._codex_session.run_turn.call_args.kwargs["user_input"] == "[platform] clean question"
        assert len([row for row in db.get_messages(sid) if row["role"] == "user"]) == 1
        assert SIDECAR_PROVENANCE_KEY not in (stored["display_metadata"] or {})
        assert db.get_messages_as_conversation(sid)[0].get("api_content") is None
    finally:
        db.close()


def test_codex_ack_clean_retry_clears_prior_attempt_sidecar_and_provenance(tmp_path):
    from agent.codex_runtime_history_seed import SIDECAR_PROVENANCE_KEY, sidecar_provenance

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-clean-retry"
        db.create_session(session_id=sid, source="cli", model="codex")
        old_wire = "clean question\n\nold memory"
        row_id = db.append_message(sid, "user", "clean question", api_content=old_wire,
                                   display_metadata={SIDECAR_PROVENANCE_KEY:
                                                     sidecar_provenance("clean question", old_wire)})
        agent = _make_agent(db, sid)
        agent._persist_user_message_override = None
        agent._session_persist_lock = None
        turn = _make_turn()
        turn.input_accepted = True
        agent._codex_session.run_turn.return_value = turn
        row = {"role": "user", "content": "clean question", "_row_id": row_id,
               "api_content": old_wire,
               "display_metadata": {SIDECAR_PROVENANCE_KEY: sidecar_provenance("clean question", old_wire)}}
        run_codex_app_server_turn(agent, user_message="clean question",
                                  original_user_message="clean question", messages=[row],
                                  effective_task_id="task")
        assert row.get("api_content") is None
        assert SIDECAR_PROVENANCE_KEY not in row.get("display_metadata", {})
        loaded = db.get_messages_as_conversation(sid)[0]
        assert loaded.get("api_content") is None
        assert SIDECAR_PROVENANCE_KEY not in (loaded.get("display_metadata") or {})
    finally:
        db.close()


def test_codex_ack_merges_provenance_with_reaction_added_after_flush(tmp_path):
    from agent.codex_runtime_history_seed import SIDECAR_PROVENANCE_KEY

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "codex-ack-reaction"
        db.create_session(session_id=sid, source="cli", model="codex")
        row_id = db.append_message(sid, "user", "clean question")
        agent = _make_agent(db, sid)
        agent._persist_user_message_override = None
        agent._session_persist_lock = None
        row = {"role": "user", "content": "clean question", "_row_id": row_id,
               "display_metadata": {"original": True}}
        db.set_message_reaction(sid, row_id, "👍")
        turn = _make_turn()
        turn.input_accepted = True
        agent._codex_session.run_turn.return_value = turn
        run_codex_app_server_turn(agent, user_message="[platform] clean question",
                                  original_user_message="clean question", messages=[row],
                                  effective_task_id="task")
        stored = db.get_messages(sid)[0]
        assert stored["api_content"] == "[platform] clean question"
        assert SIDECAR_PROVENANCE_KEY in stored["display_metadata"]
        assert stored["display_metadata"][db.REACTIONS_METADATA_KEY][0]["emoji"] == "👍"
    finally:
        db.close()


class TestGatewayPersistedResolution:
    """The gateway default must preserve standard-runtime skip-db behaviour."""

    @staticmethod
    def _resolve_persistence_block(agent_result, session_db_present):
        # gateway/run.py persistence block:
        #   agent_persisted = agent_result.get("agent_persisted", self._session_db is not None)
        return agent_result.get("agent_persisted", session_db_present)

    @staticmethod
    def _resolve_passthrough(result_holder0):
        # gateway/run.py result_holder passthrough:
        #   result_holder[0].get("agent_persisted", True) if result_holder[0] else True
        return result_holder0.get("agent_persisted", True) if result_holder0 else True

    def test_codex_result_keeps_gateway_skip(self):
        # Codex now self-persists → gateway must SKIP (agent_persisted True).
        codex = {"agent_persisted": True}
        assert self._resolve_persistence_block(codex, True) is True
        assert self._resolve_persistence_block(codex, False) is True
        assert self._resolve_passthrough(codex) is True

    def test_standard_runtime_preserves_skip_db(self):
        # Standard runtime omits the key → old behaviour: skip iff DB present.
        standard = {"final_response": "ok"}
        assert self._resolve_persistence_block(standard, True) is True
        assert self._resolve_persistence_block(standard, False) is False
        assert self._resolve_passthrough(standard) is True

    def test_missing_result_holder_defaults_persisted(self):
        assert self._resolve_passthrough(None) is True
