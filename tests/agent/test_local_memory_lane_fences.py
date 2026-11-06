"""Retained C04 contracts on the native MemoryManager: independent read preparation,
FIFO session ownership, fail-closed read fences, and bounded lifecycle teardown.

Only fake providers and bounded events are used; the parent runs RED before runtime edits.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import agent.memory_manager as memory_manager_module
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from agent.turn_context import _memory_turn_start_and_prefetch
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override


class _Provider(MemoryProvider):
    @property
    def name(self):
        return "lane-fence-fake"

    def get_tool_schemas(self):
        return []

    def is_available(self):
        return True

    def initialize(self, session_id="", **kwargs):
        pass

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None,
                  turn_author=None):
        pass

    def queue_prefetch(self, query, *, session_id=""):
        pass

    def prefetch(self, query, *, session_id="") -> str:
        return ""


@pytest.fixture
def manager():
    managers = []

    def make(provider):
        mgr = MemoryManager()
        mgr.add_provider(provider)
        managers.append(mgr)
        return mgr

    yield make
    for mgr in managers:
        mgr.shutdown_all()


@contextmanager
def _home(home):
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _thread(fn):
    """Surface assertions raised on test-owned threads to the parent test."""
    errors = []
    finished = threading.Event()

    def run():
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, finished, errors


def _join(thread, errors):
    thread.join(3)
    assert not thread.is_alive(), "test-owned thread did not finish"
    assert not errors, errors


def test_prefetch_starts_while_write_provider_is_blocked(manager):
    writing, release, prefetched = (threading.Event() for _ in range(3))

    class Provider(_Provider):
        def sync_turn(self, user_content, assistant_content, **kwargs):
            writing.set()
            assert release.wait(5)

        def queue_prefetch(self, query, *, session_id=""):
            prefetched.set()

    mgr = manager(Provider())
    try:
        mgr.sync_all("old turn", "answer", session_id="old")
        assert writing.wait(2)
        mgr.queue_prefetch_all("next query", session_id="old")
        assert prefetched.wait(2), "prefetch must not sit behind a blocked write"
    finally:
        release.set()
    assert mgr.flush_pending(timeout=5)


def test_boundary_waits_for_old_prefetch_before_end_and_switch(manager):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Provider(_Provider):
        def queue_prefetch(self, query, *, session_id=""):
            entered.set()
            assert release.wait(5)
            calls.append(("prefetch", session_id))

        def on_session_end(self, messages):
            calls.append(("end", messages[0]["content"]))

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append(("switch", new_session_id))

    mgr = manager(Provider())
    try:
        mgr.queue_prefetch_all("old query", session_id="old")
        assert entered.wait(2)
        mgr.commit_session_boundary_async([{"role": "user", "content": "old transcript"}], new_session_id="new")
        assert not mgr.flush_pending(timeout=0.1)
        assert calls == []
    finally:
        release.set()
    assert mgr.flush_pending(timeout=5)
    assert calls == [("prefetch", "old"), ("end", "old transcript"), ("switch", "new")]


def test_new_prefetch_waits_until_queued_boundary_rebinds(manager):
    ending, release, prefetched = (threading.Event() for _ in range(3))
    calls = []

    class Provider(_Provider):
        def on_session_end(self, messages):
            ending.set()
            assert release.wait(5)
            calls.append("end")

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append("switch")

        def queue_prefetch(self, query, *, session_id=""):
            calls.append(("prefetch", session_id))
            prefetched.set()

    mgr = manager(Provider())
    try:
        mgr.commit_session_boundary_async([], new_session_id="new")
        assert ending.wait(2)
        mgr.queue_prefetch_all("new query", session_id="new")
        assert not prefetched.wait(0.1)
    finally:
        release.set()
    assert mgr.flush_pending(timeout=5)
    assert calls == ["end", "switch", ("prefetch", "new")]


def test_direct_prefetch_cannot_read_old_binding_during_pending_boundary(manager):
    ending, release, returned = (threading.Event() for _ in range(3))
    reads, results = [], []

    class Provider(_Provider):
        bound = "old"

        def on_session_end(self, messages):
            ending.set()
            assert release.wait(5)

        def on_session_switch(self, new_session_id, **kwargs):
            self.bound = new_session_id

        def prefetch(self, query, *, session_id=""):
            reads.append(self.bound)
            return self.bound

    mgr = manager(Provider())

    def read():
        results.append(mgr.prefetch_all("what did we decide about deployment?", session_id="new"))
        returned.set()

    reader = None
    errors = []
    try:
        mgr.commit_session_boundary_async([], new_session_id="new")
        assert ending.wait(2)
        reader, finished, errors = _thread(read)
        assert not returned.wait(0.1)
        assert reads == []
    finally:
        release.set()
        if reader is not None:
            _join(reader, errors)
    assert mgr.flush_pending(timeout=5)
    assert reads == results == ["new"]


def test_turn_start_and_direct_prefetch_follow_queued_switch(manager):
    writing, release, returned = (threading.Event() for _ in range(3))
    calls, result = [], []

    class Provider(_Provider):
        bound = "old"

        def sync_turn(self, user_content, assistant_content, **kwargs):
            writing.set()
            assert release.wait(5)

        def on_session_switch(self, new_session_id, **kwargs):
            self.bound = new_session_id
            calls.append("switch")

        def on_turn_start(self, turn_number, message, **kwargs):
            calls.append(("turn_start", self.bound))

        def prefetch(self, query, *, session_id=""):
            calls.append(("prefetch", self.bound))
            return self.bound

    mgr = manager(Provider())
    agent = SimpleNamespace(_memory_manager=mgr, session_id="new", _user_turn_count=1,
                            _emit_status=lambda text: None)
    thread = None
    errors = []
    try:
        mgr.sync_all("old", "answer", session_id="old")
        assert writing.wait(2)
        mgr.commit_session_boundary_async([], new_session_id="new")

        def turn():
            result.append(_memory_turn_start_and_prefetch(agent, "What did we decide about deployment?"))
            returned.set()

        thread, finished, errors = _thread(turn)
        assert not returned.wait(0.1)
        assert calls == []
    finally:
        release.set()
        if thread is not None:
            _join(thread, errors)
    assert mgr.flush_pending(timeout=5)
    assert result == ["new"]
    assert calls == ["switch", ("turn_start", "new"), ("prefetch", "new")]


def test_failed_boundary_does_not_expose_stale_memory_to_new_turn(manager):
    """A failed rebind cannot license a new-session tick or recall against old provider state."""
    calls = []

    class Provider(_Provider):
        def on_session_switch(self, new_session_id, **kwargs):
            raise RuntimeError("rebind failed")

        def on_turn_start(self, turn_number, message, **kwargs):
            calls.append("tick")

        def prefetch(self, query, *, session_id=""):
            calls.append("read")
            return "old session data"

    mgr = manager(Provider())
    mgr.commit_session_boundary_async([], new_session_id="new")
    assert mgr.flush_pending(timeout=5)
    agent = SimpleNamespace(_memory_manager=mgr, session_id="new", _user_turn_count=1,
                            _emit_status=lambda text: None)
    assert _memory_turn_start_and_prefetch(agent, "What did we decide about deployment?") == ""
    assert mgr.prefetch_all("What did we decide about deployment?", session_id="new") == ""
    assert calls == []


def test_queued_prefetch_after_failed_boundary_never_reads_old_session(manager):
    ending, release = threading.Event(), threading.Event()
    reads = []

    class Provider(_Provider):
        def on_session_end(self, messages):
            ending.set()
            assert release.wait(5)

        def on_session_switch(self, new_session_id, **kwargs):
            raise RuntimeError("cannot bind new session")

        def queue_prefetch(self, query, *, session_id=""):
            reads.append(session_id)

    mgr = manager(Provider())
    try:
        mgr.commit_session_boundary_async([], new_session_id="new")
        assert ending.wait(2)
        mgr.queue_prefetch_all("new query", session_id="new")
        assert reads == []
    finally:
        release.set()
    assert mgr.flush_pending(timeout=5)
    assert reads == []


def test_pending_boundary_wait_expires_without_releasing_stale_read(manager):
    ending, release = threading.Event(), threading.Event()
    reads = []

    class Provider(_Provider):
        def on_session_end(self, messages):
            ending.set()
            assert release.wait(5)

        def on_session_switch(self, new_session_id, **kwargs):
            pass

        def prefetch(self, query, *, session_id="") -> str:
            reads.append(session_id)
            return "stale"

    mgr = manager(Provider())
    try:
        mgr.commit_session_boundary_async([], new_session_id="new")
        assert ending.wait(2)
        assert mgr.wait_for_session_boundary(timeout=0.1) is False
        assert reads == []
    finally:
        release.set()
    assert mgr.wait_for_session_boundary(timeout=3) is True
    assert mgr.flush_pending(timeout=5)


def test_boundary_wait_deadline_includes_lock_admission(manager):
    """The public fence has a single deadline, even if submission lock is held."""
    mgr = manager(_Provider())
    returned = threading.Event()
    outcomes = []
    reader = None
    errors = []
    mgr._sync_executor_lock.acquire()
    try:
        def fence():
            outcomes.append(mgr.wait_for_session_boundary(timeout=0.1))
            returned.set()

        reader, finished, errors = _thread(fence)
        assert returned.wait(2), "a lock-admission wait must obey its deadline"
        assert outcomes == [False]
    finally:
        mgr._sync_executor_lock.release()
        if reader is not None:
            _join(reader, errors)


def test_constructor_fallback_preserves_old_prefetch_before_boundary(manager, monkeypatch):
    """A failed write-lane constructor must not overtake active read preparation."""
    import tools.daemon_pool as daemon_pool

    entered, release, attempted = (threading.Event() for _ in range(3))
    calls = []

    class Provider(_Provider):
        def queue_prefetch(self, query, *, session_id=""):
            entered.set()
            assert release.wait(5)
            calls.append("old prefetch")

        def on_session_end(self, messages):
            calls.append("end")

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append("switch")

    mgr = manager(Provider())
    mgr.queue_prefetch_all("old", session_id="old")
    assert entered.wait(2)
    original = daemon_pool.DaemonThreadPoolExecutor

    def create(*args, **kwargs):
        if kwargs.get("thread_name_prefix") == "mem-sync":
            attempted.set()
            raise RuntimeError("no write worker")
        return original(*args, **kwargs)

    monkeypatch.setattr(daemon_pool, "DaemonThreadPoolExecutor", create)
    boundary = None
    errors = []
    try:
        boundary, finished, errors = _thread(lambda: mgr.commit_session_boundary_async([], new_session_id="new"))
        assert attempted.wait(2)
        assert calls == []
    finally:
        release.set()
        if boundary is not None:
            _join(boundary, errors)
    assert mgr.flush_pending(timeout=5)
    assert calls == ["old prefetch", "end", "switch"]


def test_prefetch_constructor_fallback_still_fences_later_switch(manager, monkeypatch):
    import tools.daemon_pool as daemon_pool

    entered, release, attempted = (threading.Event() for _ in range(3))
    calls = []

    class Provider(_Provider):
        def queue_prefetch(self, query, *, session_id=""):
            entered.set()
            assert release.wait(5)
            calls.append("old prefetch")

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append("switch")

    mgr = manager(Provider())
    original = daemon_pool.DaemonThreadPoolExecutor

    def create(*args, **kwargs):
        if kwargs.get("thread_name_prefix") == "mem-prefetch":
            attempted.set()
            raise RuntimeError("no prefetch worker")
        return original(*args, **kwargs)

    monkeypatch.setattr(daemon_pool, "DaemonThreadPoolExecutor", create)
    prefetch = None
    errors = []
    try:
        prefetch, finished, errors = _thread(lambda: mgr.queue_prefetch_all("old", session_id="old"))
        assert attempted.wait(2)
        assert entered.wait(2)
        mgr.commit_session_boundary_async([], new_session_id="new")
        assert calls == []
    finally:
        release.set()
        if prefetch is not None:
            _join(prefetch, errors)
    assert mgr.flush_pending(timeout=5)
    assert calls == ["old prefetch", "switch"]


def test_submission_fallback_keeps_new_prefetch_behind_old_sync_and_switch(manager, monkeypatch):
    writing, release, rejected = (threading.Event() for _ in range(3))
    calls = []

    class Provider(_Provider):
        def sync_turn(self, user_content, assistant_content, **kwargs):
            writing.set()
            assert release.wait(5)
            calls.append("sync")

        def on_session_end(self, messages):
            calls.append("end")

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append("switch")

        def queue_prefetch(self, query, *, session_id=""):
            calls.append("new prefetch")

    mgr = manager(Provider())
    mgr.sync_all("old", "answer", session_id="old")
    assert writing.wait(2)
    executor = mgr._sync_executor
    assert executor is not None
    original_submit = executor.submit

    def reject_once(fn):
        rejected.set()
        monkeypatch.setattr(executor, "submit", original_submit)
        raise RuntimeError("submission rejected")

    monkeypatch.setattr(executor, "submit", reject_once)
    boundary = None
    errors = []
    try:
        boundary, finished, errors = _thread(lambda: mgr.commit_session_boundary_async([], new_session_id="new"))
        assert rejected.wait(2)
        mgr.queue_prefetch_all("new query", session_id="new")
        assert calls == []
    finally:
        release.set()
        if boundary is not None:
            _join(boundary, errors)
    assert mgr.flush_pending(timeout=5)
    assert calls == ["sync", "end", "switch", "new prefetch"]


def test_shutdown_drains_both_lanes_and_boundary_in_session_order(manager):
    writing, release, old_prefetched = (threading.Event() for _ in range(3))
    calls = []

    class Provider(_Provider):
        def sync_turn(self, user_content, assistant_content, **kwargs):
            if user_content == "first":
                writing.set()
                assert release.wait(5)
            calls.append(("sync", user_content))

        def queue_prefetch(self, query, *, session_id=""):
            calls.append(("prefetch", session_id))
            if session_id == "old":
                old_prefetched.set()

        def on_session_end(self, messages):
            calls.append(("end", messages[0]["content"]))

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append(("switch", new_session_id))

    mgr = manager(Provider())
    try:
        mgr.sync_all("first", "reply", session_id="old")
        assert writing.wait(2)
        mgr.queue_prefetch_all("old query", session_id="old")
        assert old_prefetched.wait(2)
        mgr.sync_all("second", "reply", session_id="old")
        mgr.commit_session_boundary_async([{"role": "user", "content": "old transcript"}], new_session_id="new")
        mgr.queue_prefetch_all("new query", session_id="new")
    finally:
        release.set()
    mgr.shutdown_all()
    assert calls == [("prefetch", "old"), ("sync", "first"), ("sync", "second"),
                     ("end", "old transcript"), ("switch", "new"), ("prefetch", "new")]
    assert mgr.shutdown_drain_state["status"] == "drained"
    assert mgr.shutdown_drain_state["abandoned_writes"] == 0
    assert mgr.shutdown_drain_state["abandoned_prefetches"] == 0


def test_shutdown_uses_one_bounded_budget_for_both_lanes_and_cancels_old_effects(manager, monkeypatch, caplog):
    writing, reading, release, write_done, read_done = (threading.Event() for _ in range(5))
    calls = []

    class Provider(_Provider):
        def sync_turn(self, user_content, assistant_content, **kwargs):
            if user_content == "active write":
                writing.set()
                assert release.wait(5)
                write_done.set()
            calls.append(user_content)

        def queue_prefetch(self, query, *, session_id=""):
            if query == "active read":
                reading.set()
                assert release.wait(5)
                read_done.set()
            calls.append(query)

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append("late switch")

    monkeypatch.setattr(memory_manager_module, "_SYNC_DRAIN_TIMEOUT_S", 0.15)
    mgr = manager(Provider())
    try:
        mgr.sync_all("active write", "reply", session_id="old")
        assert writing.wait(2)
        mgr.queue_prefetch_all("active read", session_id="old")
        assert reading.wait(2), "prefetch worker must start independently of write worker"
        mgr.sync_all("queued write", "reply", session_id="old")
        mgr.queue_prefetch_all("queued read", session_id="old")
        mgr.commit_session_boundary_async([], new_session_id="new")
        with caplog.at_level(logging.WARNING, logger="agent.memory_manager"):
            started = time.monotonic()
            mgr.shutdown_all()
            elapsed = time.monotonic() - started
        state = mgr.shutdown_drain_state
        assert elapsed < 2, "shutdown must not spend a fresh drain budget per lane"
        assert state["status"] == "timed_out"
        assert state["abandoned_writes"] >= 2  # queued sync + old-session boundary
        assert state["abandoned_prefetches"] == 1
        assert state["active_tasks"] == 2
        assert "abandoning" in caplog.text
    finally:
        release.set()
    assert write_done.wait(3) and read_done.wait(3)
    assert "queued write" not in calls and "queued read" not in calls and "late switch" not in calls


def test_split_lanes_retain_bound_profile_and_refuse_foreign_hooks(tmp_path):
    """Both worker contexts keep the initialized home; a foreign caller cannot enqueue hooks."""
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    events = []

    class Provider(_Provider):
        def sync_turn(self, user_content, assistant_content, **kwargs):
            events.append(("write", str(get_hermes_home())))

        def queue_prefetch(self, query, *, session_id=""):
            events.append(("prefetch", str(get_hermes_home())))

        def on_session_switch(self, new_session_id, **kwargs):
            events.append(("switch", str(get_hermes_home())))

    with _home(alpha):
        mgr = MemoryManager()
        mgr.add_provider(Provider())
        mgr.initialize_all(session_id="old", hermes_home=str(alpha))
        try:
            mgr.sync_all("own turn", "answer", session_id="old")
            mgr.queue_prefetch_all("own query", session_id="old")
            mgr.commit_session_boundary_async([], new_session_id="new")
            assert mgr.flush_pending(timeout=5)
            assert sorted(events[:2]) == sorted([("write", str(alpha)), ("prefetch", str(alpha))])
            assert events[2:] == [("switch", str(alpha))]
            with _home(beta):
                mgr.sync_all("foreign turn", "answer", session_id="foreign")
                mgr.queue_prefetch_all("foreign query", session_id="foreign")
                mgr.commit_session_boundary_async([], new_session_id="foreign")
            assert mgr.flush_pending(timeout=5)
            assert len(events) == 3
        finally:
            mgr.shutdown_all()
