"""Native row/ACK refusal controls. Real runtime and temporary DB, recording inference only."""
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from agent.codex_runtime import run_codex_app_server_turn
from agent.codex_runtime_history_seed import SIDECAR_PROVENANCE_KEY, sidecar_provenance, render_history_seed
from agent.turn_context import compose_user_api_content
from hermes_state import SessionDB

# Import during collection so the unchanged native kanban fixture can guard it.
from hermes_cli import kanban_db as _isolation_kanban_db
from hermes_cli import kanban_db_connect as _isolation_kanban_connect


@pytest.fixture(autouse=True)
def _codex_ack_recovery(request, tmp_path, codex_isolation_proof):
    """Only bootstrap recovery custody changes; row/ACK/runtime helpers are real."""
    from hermes_cli import _early_recovery as recovery
    from tests.agent._codex_isolation_plugin import recovery_scope, persist
    previous = recovery.restore_interrupted_pull
    with recovery_scope(tmp_path / "empty-bootstrap-recovery") as adapter:
        yield adapter
    assert recovery.restore_interrupted_pull is previous
    codex_isolation_proof.setdefault("ack_recovery_scopes", []).append({
        "nodeid": request.node.nodeid, "root": str(adapter["root"]),
        "events": list(adapter["events"]), "restored": True,
    })
    persist(codex_isolation_proof)


def _peer_agent(db, sid, turn):
    agent = MagicMock()
    agent._codex_session = MagicMock()
    agent._codex_session._history_seed_pending = False
    agent._codex_session._history_seed = ""
    agent._codex_session.ensure_started.return_value = "fresh-thread"
    agent._codex_session.run_turn.return_value = turn
    agent._codex_session_prompt = None
    agent._persist_user_message_override = None
    agent._session_persist_lock = None
    agent._last_compaction_in_place = False
    agent.tool_progress_callback = None
    # Required stable post-turn scalar contract, also set by the deployed peer.
    # Do not let MagicMock synthesize numeric counters or an accounting model.
    agent._iters_since_skill = 0
    agent._skill_nudge_interval = 0
    agent.model = "codex"
    agent.provider = "openai-codex"
    agent.valid_tool_names = set()
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = sid
    return agent


def _turn(wire):
    return SimpleNamespace(input_accepted=True, submitted_user_text=wire, turn_id="literal-ack",
                           interrupted=False, error=None, thread_id="fresh-thread",
                           projected_messages=[{"role": "assistant", "content": "done"}],
                           tool_iterations=0, final_text="done", should_retire=False)


def _row(db, sid, **kwargs):
    rid = db.append_message(sid, "user", "visible question", **kwargs)
    return {"role": "user", "content": "visible question", "_row_id": rid}


@pytest.mark.parametrize("field,value", [
    ("input_accepted", False), ("input_accepted", None), ("input_accepted", 1),
    ("input_accepted", "true"), ("turn_id", None), ("turn_id", ""),
    ("turn_id", "   "), ("turn_id", 7), ("submitted_user_text", None),
    ("submitted_user_text", "unrelated wire"), ("submitted_user_text", "visible question"),
    ("submitted_user_text", ["not a string"]),
])
def test_unverifiable_ack_cannot_grant_live_or_durable_sidecar(tmp_path, field, value):
    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "ack-refusal"
        db.create_session(session_id=sid, source="cli", model="codex")
        row = _row(db, sid)
        wire = compose_user_api_content(row["content"], "selected private fact", "selected plugin")
        turn = _turn(wire)
        setattr(turn, field, value)
        agent = _peer_agent(db, sid, turn)
        result = run_codex_app_server_turn(agent, user_message=row["content"],
                                           original_user_message=row["content"], messages=[row],
                                           effective_task_id="test", current_turn_user_idx=0,
                                           ext_prefetch_cache="selected private fact",
                                           plugin_user_context="selected plugin")
        assert result["completed"] is True  # transport completion is not accepted-input proof
        assert agent._codex_session.run_turn.call_args.kwargs["user_input"] == wire
        assert row.get("api_content") is None
        assert row.get("_codex_input_accepted") is not True
        stored = db.get_messages(sid)[0]
        assert stored["api_content"] is None
        assert SIDECAR_PROVENANCE_KEY not in (stored["display_metadata"] or {})
    finally:
        db.close()


@pytest.mark.parametrize("submitted_shape", ["full", "suffix", "foreign-prefix"])
def test_seed_snapshot_after_thread_start_stores_only_exact_acknowledged_suffix(tmp_path, submitted_shape):
    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "seed-snapshot"
        db.create_session(session_id=sid, source="cli", model="codex")
        row = _row(db, sid)
        wire = compose_user_api_content(row["content"], "current fact", "")
        seed = "known historical seed from thread/start"
        full = seed + "\n\n[CURRENT USER TURN]\n" + wire
        submitted = {"full": full, "suffix": wire, "foreign-prefix": "other seed\n\n[CURRENT USER TURN]\n" + wire}[submitted_shape]
        turn = _turn(submitted)
        agent = _peer_agent(db, sid, turn)

        def thread_start(**_kwargs):
            agent._codex_session._history_seed_pending = True
            agent._codex_session._history_seed = seed
            return "fresh-thread"

        agent._codex_session.ensure_started.side_effect = thread_start
        run_codex_app_server_turn(agent, user_message=row["content"],
                                  original_user_message=row["content"], messages=[row],
                                  effective_task_id="test", ext_prefetch_cache="current fact")
        stored = db.get_messages(sid)[0]
        if submitted_shape == "full":
            assert row["api_content"] == stored["api_content"] == wire
            assert seed not in stored["api_content"]
            assert stored["display_metadata"][SIDECAR_PROVENANCE_KEY] == sidecar_provenance(row["content"], wire)
            history = db.get_messages_as_conversation(sid) + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "next"}]
            rendered = render_history_seed(history)
            assert "current fact" in rendered and seed not in rendered
        else:
            assert row.get("api_content") is None and stored["api_content"] is None
            assert row.get("_codex_input_accepted") is not True
    finally:
        db.close()


@pytest.mark.parametrize("guard", ["wrong-session", "wrong-role", "inactive", "changed-content", "submit-owner", "writer-failure", "invalid-row-id"])
def test_ack_guard_failure_never_falls_back_to_latest_equal_row(tmp_path, monkeypatch, guard):
    db = SessionDB(tmp_path / "state.db")
    try:
        sid, other = "guarded-current", "guarded-other"
        for s in (sid, other):
            db.create_session(session_id=s, source="cli", model="codex")
        row = _row(db, sid)
        if guard == "wrong-session":
            row["_row_id"] = db.append_message(other, "user", row["content"])
        elif guard == "wrong-role":
            row["_row_id"] = db.append_message(sid, "assistant", row["content"])
        elif guard == "inactive":
            db.archive_and_compact(sid, [{"role": "user", "content": row["content"]}])
        elif guard == "changed-content":
            db.set_user_message_content(sid, row["_row_id"], "different durable content")
        elif guard == "submit-owner":
            row["_submit_row_session_id"] = other
        elif guard == "invalid-row-id":
            row["_row_id"] = True
        elif guard == "writer-failure":
            def writer_failure(*_args, **_kwargs):
                raise OSError("isolated writer failure")
            monkeypatch.setattr(db, "set_message_api_content", writer_failure)
        # A newest equal row makes an unauthorized positional fallback observable.
        db.append_message(sid, "user", row["content"])
        before = db.get_messages(sid, include_inactive=True)
        wire = compose_user_api_content(row["content"], "not-authorized-for-replay", "")
        agent = _peer_agent(db, sid, _turn(wire))
        agent._last_compaction_in_place = True
        row["api_content"] = "unproven inherited wire"
        row["display_metadata"] = {SIDECAR_PROVENANCE_KEY: "unproven"}
        run_codex_app_server_turn(agent, user_message=row["content"],
                                  original_user_message=row["content"], messages=[row],
                                  effective_task_id="test", ext_prefetch_cache="not-authorized-for-replay",
                                  preflight_compressed=True)
        assert row.get("api_content") is None
        assert row.get("_codex_input_accepted") is not True
        assert SIDECAR_PROVENANCE_KEY not in row.get("display_metadata", {})
        assert db.get_messages(sid, include_inactive=True) == before
        assert all(r["api_content"] is None for r in db.get_messages(other))
    finally:
        db.close()


def test_current_context_is_normalized_and_multimodal_native_images_kept(tmp_path):
    # Actual current-input helper shares native composition with the real runtime.
    from agent.codex_runtime import _codex_current_input
    turn = _turn("question")
    agent = _peer_agent(None, "unused", turn)
    original = [{"type": "text", "text": "question"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]
    selected = {"type": "text", "text": "selected context already staged"}
    staged = original + [selected]
    result, idx, current_string = _codex_current_input(agent, original, [{"role": "user", "content": staged}], 0, {}, ["invalid optional"])
    assert idx == 0 and current_string is False
    assert result == staged and result.count(selected) == 1
    result, _, current_string = _codex_current_input(agent, "question", [{"role": "user", "content": "question"}], 0, {}, ["invalid optional"])
    assert result == "question" and current_string is True


def test_isolation_actual_operator_capture_and_dynamic_state_root(codex_isolation_proof):
    from pathlib import Path
    from hermes_state_guard import _real_platform_state_root, _is_production_state_db
    s = codex_isolation_proof
    actual = Path("/home/hermes/.hermes")
    assert actual in s["native"]._REAL_HERMES_ROOT_CANDIDATES
    assert s["native"]._REAL_KANBAN_ROOT == actual
    assert s["native"]._NATIVE_HERMES_PARENT == Path("/home/hermes")
    assert _real_platform_state_root() == actual
    assert _is_production_state_db(actual / "state.db", actual)
    assert _is_production_state_db(actual / "profiles" / "default" / "state.db", actual)
    assert s["stdlib_selector_restored"] is True
    assert s["home_restored_before_tests"] == "/home/hermes"


def _assert_native_refusal_site(caught, name, relative_source):
    from tests.agent._codex_isolation_plugin import CORE
    assert any(entry.frame.code.raw.co_name == name
               and entry.frame.code.raw.co_filename == str(CORE / relative_source)
               for entry in caught.traceback), "refusal came from a different guard"


def test_isolation_refusal_traceback_identity_needs_no_metadata(request, monkeypatch, codex_isolation_proof):
    import os
    from tests.home_io_guard import HomeIOGuard
    from tests.agent._codex_isolation_plugin import recording_bottom
    calls = []
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "stat", recording_bottom(calls, "stat"))
        # This actual native refusal function only raises; it performs no I/O.
        with pytest.raises(AssertionError, match="TEST BUG: file I/O against the REAL hermes home") as caught:
            HomeIOGuard.refuse("inert-refusal-control-only")
        _assert_native_refusal_site(caught, "refuse", "tests/home_io_guard.py")
        assert calls == []


@pytest.mark.parametrize("operation", ["read", "write", "metadata", "sqlite"])
def test_isolation_real_operator_io_refused_before_io(operation, request, monkeypatch,
                                                    codex_isolation_proof):
    import builtins
    import io
    import os
    import sqlite3
    import re
    from tests.agent._codex_isolation_plugin import (
        recording_bottom, native_fixture_scope, inert_metadata_scope, refusal_receipt,
    )
    sentinel = "/home/hermes/.hermes/03-codex-isolation-exception-denied-probe"
    calls = []
    bindings = [(module, name, getattr(module, name)) for module, name in (
        (builtins, "open"), (io, "open"), (os, "stat"), (os, "lstat"),
        (os, "readlink"), (sqlite3, "connect"),
    )]
    # Bottoms are installed FIRST. The actual native fixture captures them;
    # no real operator I/O remains downstream even if its check fails open.
    with inert_metadata_scope([sentinel]), monkeypatch.context() as bottom:
        for module, name, _previous in bindings:
            bottom.setattr(module, name, recording_bottom(calls, name))
        with native_fixture_scope(codex_isolation_proof["native"]._forbid_real_hermes_home_io,
                                  request=request):
            with pytest.raises(AssertionError, match=re.escape(
                    "TEST BUG: file I/O against the REAL hermes home: " + sentinel)) as caught:
                if operation in ("read", "write"):
                    builtins.open(sentinel, "rb" if operation == "read" else "xb")
                elif operation == "metadata":
                    os.stat(sentinel)
                else:
                    sqlite3.connect(sentinel)
            _assert_native_refusal_site(caught, "refuse", "tests/home_io_guard.py")
            assert calls == []
    assert all(getattr(module, name) is previous for module, name, previous in bindings)
    refusal_receipt(codex_isolation_proof, request.node.nodeid, operation,
                    "HomeIOGuard.check/refuse", calls)


def test_isolation_native_kanban_and_sessiondb_denials(request, tmp_path, monkeypatch,
                                                    codex_isolation_proof):
    from pathlib import Path
    import hermes_state
    from tests.agent._codex_isolation_plugin import (
        OPERATOR_ROOT, recording_bottom, native_fixture_scope,
        inert_metadata_scope, refusal_receipt,
    )
    kanban = Path("/home/hermes/.hermes/03-codex-isolation-exception-kanban.db")
    state_db = Path("/home/hermes/.hermes/state.db")
    calls = []
    previous = _isolation_kanban_connect.connect
    with inert_metadata_scope([kanban, state_db, OPERATOR_ROOT]):
        with monkeypatch.context() as bottom:
            bottom.setattr(_isolation_kanban_connect, "connect", recording_bottom(calls, "kanban.connect"))
            with native_fixture_scope(codex_isolation_proof["native"]._kanban_write_guard,
                                      _hermetic_environment=None):
                with pytest.raises(RuntimeError, match="kanban_write_guard: kanban DB path resolved to") as caught:
                    _isolation_kanban_connect.connect(kanban)
                _assert_native_refusal_site(caught, "_guarded_connect", "tests/conftest.py")
                assert calls == []
        assert _isolation_kanban_connect.connect is previous
        # The real SessionDB choke point is called directly: a missed denial
        # cannot reach a constructor, mkdir, SQLite, or any production I/O.
        with pytest.raises(RuntimeError, match="live-system guard: test attempted to open production state.db at") as caught:
            hermes_state._ensure_test_isolation(state_db)
        _assert_native_refusal_site(caught, "_ensure_test_isolation", "hermes_state.py")
        assert hermes_state._ensure_test_isolation(tmp_path / "allowed-state.db") is None
    refusal_receipt(codex_isolation_proof, request.node.nodeid, "kanban",
                    "_kanban_write_guard._guarded_connect", calls)
    refusal_receipt(codex_isolation_proof, request.node.nodeid, "sessiondb",
                    "hermes_state._ensure_test_isolation", calls)


def test_isolation_native_lifecycle_denials(request, monkeypatch, codex_isolation_proof):
    import os
    import signal
    import subprocess
    from tests.agent._codex_isolation_plugin import (
        CORE, recording_bottom, native_fixture_scope, refusal_receipt,
    )
    calls = []
    previous = (os.kill, subprocess.run, subprocess.Popen)
    with monkeypatch.context() as bottom:
        bottom.setattr(os, "kill", recording_bottom(calls, "os.kill"))
        bottom.setattr(subprocess, "run", recording_bottom(calls, "subprocess.run"))
        # No Popen/class replacement: native subclass/type boundaries remain.
        with native_fixture_scope(codex_isolation_proof["native"]._live_system_guard,
                                  request=request):
            with pytest.raises(RuntimeError, match="would mutate the live hermes-gateway systemd unit") as caught:
                subprocess.run(["systemctl", "restart", "hermes-gateway.service"], check=True)
            _assert_native_refusal_site(caught, "_check_subprocess_cmd", "tests/_fixtures/live_system_guard.py")
            with pytest.raises(RuntimeError, match=r"blocked os\.kill\(-1, .*PID is outside the test process subtree") as caught:
                os.kill(-1, signal.SIGTERM)
            _assert_native_refusal_site(caught, "_guarded_kill", "tests/_fixtures/live_system_guard.py")
            with pytest.raises(RuntimeError, match="`git reset` would mutate the protected checkout") as caught:
                subprocess.run(["git", "-C", str(CORE), "reset", "--hard"], check=True)
            _assert_native_refusal_site(caught, "_check_subprocess_cmd", "tests/_fixtures/live_system_guard.py")
            assert calls == []
    assert (os.kill, subprocess.run, subprocess.Popen) == previous
    for control, site in (
        ("systemctl", "_live_system_guard._check_subprocess_cmd/systemctl"),
        ("kill", "_live_system_guard._guarded_kill"),
        ("git", "_live_system_guard._check_subprocess_cmd/blocked_git_mutation"),
    ):
        refusal_receipt(codex_isolation_proof, request.node.nodeid, control, site, calls)


def test_isolation_actual_scratch_and_escape_denials(tmp_path, tmp_path_factory, codex_isolation_proof):
    import tempfile
    from pathlib import Path
    from hermes_constants import get_hermes_home, get_default_hermes_root
    from hermes_cli.profiles import get_profile_dir
    from tests.agent._codex_isolation_plugin import owned_path
    sandbox = codex_isolation_proof["sandbox_path"]
    for value in (tmp_path, tmp_path_factory.getbasetemp(), Path(tempfile.gettempdir()),
                  get_hermes_home(), get_default_hermes_root(), get_profile_dir("default")):
        assert owned_path(value, sandbox).is_relative_to(sandbox)
    with tempfile.NamedTemporaryFile() as probe:
        assert owned_path(probe.name, sandbox).is_relative_to(sandbox)
    with pytest.raises(AssertionError, match="scratch escape"):
        owned_path(Path("/var/tmp/03-codex-isolation-exception-never-created"), sandbox)
    link = tmp_path / "escape-link"
    link.symlink_to(sandbox.parent, target_is_directory=True)
    with pytest.raises(AssertionError, match="scratch symlink escape"):
        owned_path(link / "never-created", sandbox)


def test_isolation_real_recovery_empty_fixture_and_fail_closed(_codex_ack_recovery, monkeypatch):
    from hermes_cli import _early_recovery as recovery
    from tests.agent._codex_isolation_plugin import CORE
    adapter = _codex_ack_recovery
    real_git_dir = recovery._git_dir
    observed = []

    def record_git_dir(root):
        assert root == adapter["root"]
        observed.append(root)
        return real_git_dir(root)

    with monkeypatch.context() as scoped:
        scoped.setattr(recovery, "_git_dir", record_git_dir)
        assert recovery.restore_interrupted_pull(CORE) is False
    assert observed == [adapter["root"]]
    assert adapter["events"][-1]["actual_root"] == str(adapter["root"])
    assert list((adapter["root"] / ".git").iterdir()) == []
    with pytest.raises(AssertionError, match="unrelated root"):
        recovery.restore_interrupted_pull(CORE.parent / "unrelated-never-probed")
    with monkeypatch.context() as scoped:
        def arbitrary_failure(_root):
            raise AssertionError("real recovery failure must surface")
        scoped.setattr(recovery, "_git_dir", arbitrary_failure)
        with pytest.raises(AssertionError, match="real recovery failure must surface"):
            recovery.restore_interrupted_pull(CORE)


def test_isolation_recovery_scoped_binding_restored(tmp_path):
    from hermes_cli import _early_recovery as recovery
    from tests.agent._codex_isolation_plugin import CORE, recovery_scope
    before = recovery.restore_interrupted_pull
    with recovery_scope(tmp_path / "nested-empty-recovery") as adapter:
        assert recovery.restore_interrupted_pull is adapter["binding"]
        assert recovery.restore_interrupted_pull(CORE) is False
    assert recovery.restore_interrupted_pull is before

