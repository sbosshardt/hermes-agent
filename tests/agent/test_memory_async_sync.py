"""Regression guard: end-of-turn memory sync must not block the turn.

Before this fix, ``MemoryManager.sync_all`` / ``queue_prefetch_all`` looped
``provider.sync_turn`` / ``provider.queue_prefetch`` INLINE on the
turn-completion path. A provider making a blocking network/daemon call (a
misconfigured Hindsight daemon was observed blocking ~298s before failing)
held ``run_conversation`` open long after the user saw their response, so
every interface (CLI, TUI, gateway) kept the agent marked "running" for
minutes and any follow-up message triggered an aggressive interrupt that
dropped the message.

The fix dispatches provider work to a single-worker background executor.
``sync_all`` / ``queue_prefetch_all`` return immediately; the work completes
(or fails, logged) in the background. ``flush_pending`` provides a barrier
for session boundaries and deterministic tests. ``shutdown_all`` drains the
executor with a bounded timeout so a wedged provider can't hang teardown.
"""
import logging
import threading
import time

import pytest

from agent.memory_provider import MemoryProvider
from agent.memory_manager import MemoryManager
from types import SimpleNamespace


class _SlowProvider(MemoryProvider):
    """Provider whose sync/prefetch block, simulating a slow backend."""

    _name = "slow"

    def __init__(self, delay: float = 1.0):
        self._delay = delay
        self.sync_done = False
        self.prefetch_done = False

    @property
    def name(self) -> str:
        return self._name

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def is_available(self) -> bool:
        return True

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query, *, session_id: str = "") -> str:
        return ""

    def queue_prefetch(self, query, *, session_id: str = "") -> None:
        time.sleep(self._delay)
        self.prefetch_done = True

    def sync_turn(self, user_content, assistant_content, *, session_id: str = "", messages=None) -> None:
        time.sleep(self._delay)
        self.sync_done = True

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs) -> str:
        return ""




def test_background_work_still_completes():
    """Dispatching off-thread must not silently drop the write."""
    mgr = MemoryManager()
    p = _SlowProvider(delay=0.1)
    mgr.add_provider(p)

    mgr.sync_all("hi", "hey", session_id="s1")
    mgr.queue_prefetch_all("hi", session_id="s1")

    assert mgr.flush_pending(timeout=10) is True
    assert p.sync_done is True
    assert p.prefetch_done is True










def test_prefetch_starts_while_sync_is_blocked():
    """Independent read preparation must not queue behind a wedged retain."""
    sync_started = threading.Event()
    release_sync = threading.Event()
    prefetch_started = threading.Event()

    class BlockingProvider(_SlowProvider):
        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
            sync_started.set()
            assert release_sync.wait(timeout=5)

        def queue_prefetch(self, query, *, session_id=""):
            prefetch_started.set()

    mgr = MemoryManager()
    mgr.add_provider(BlockingProvider(delay=0))
    try:
        mgr.sync_all("hi", "hey", session_id="s1")
        assert sync_started.wait(timeout=2)
        mgr.queue_prefetch_all("next", session_id="s1")
        assert prefetch_started.wait(timeout=2)
    finally:
        release_sync.set()
        assert mgr.flush_pending(timeout=5)
        mgr.shutdown_all()


def test_session_boundary_waits_for_prior_prefetch_before_switch():
    """Preserve the release's FIFO session ownership after splitting workers."""
    prefetch_started = threading.Event()
    release_prefetch = threading.Event()
    boundary_done = threading.Event()
    calls = []

    class BoundaryProvider(_SlowProvider):
        def queue_prefetch(self, query, *, session_id=""):
            prefetch_started.set()
            assert release_prefetch.wait(timeout=5)
            calls.append("prefetch")

        def on_session_end(self, messages):
            calls.append("end")

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append("switch")
            boundary_done.set()

    mgr = MemoryManager()
    mgr.add_provider(BoundaryProvider(delay=0))
    try:
        mgr.queue_prefetch_all("old", session_id="old")
        assert prefetch_started.wait(timeout=2)
        mgr.commit_session_boundary_async([{"role": "user", "content": "old"}], new_session_id="new")
        assert not mgr.flush_pending(timeout=0.1)
        assert not boundary_done.is_set()
    finally:
        release_prefetch.set()
        assert mgr.flush_pending(timeout=5)
        mgr.shutdown_all()
    assert calls == ["prefetch", "end", "switch"]


def test_new_session_prefetch_waits_for_boundary_rebind():
    boundary_started = threading.Event()
    release_boundary = threading.Event()
    new_prefetch_started = threading.Event()
    calls = []

    class BoundaryProvider(_SlowProvider):
        def on_session_end(self, messages):
            boundary_started.set()
            assert release_boundary.wait(timeout=5)
            calls.append("end")

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append("switch")

        def queue_prefetch(self, query, *, session_id=""):
            calls.append("prefetch")
            new_prefetch_started.set()

    mgr = MemoryManager()
    mgr.add_provider(BoundaryProvider(delay=0))
    try:
        mgr.commit_session_boundary_async([{"role": "user", "content": "old"}], new_session_id="new")
        assert boundary_started.wait(timeout=2)
        mgr.queue_prefetch_all("new query", session_id="new")
        assert not mgr.flush_pending(timeout=0.1)
        assert not new_prefetch_started.is_set()
    finally:
        release_boundary.set()
        assert mgr.flush_pending(timeout=5)
        mgr.shutdown_all()
    assert calls == ["end", "switch", "prefetch"]


def test_public_prefetch_waits_for_pending_session_rebind():
    """A direct read must not consume provider state before the queued switch."""
    started, release, returned = (threading.Event() for _ in range(3))
    reads, results = [], []

    class Provider(_SlowProvider):
        def __init__(self):
            super().__init__(delay=0)
            self.bound_session = "old"

        def on_session_end(self, messages):
            started.set()
            assert release.wait(5)

        def on_session_switch(self, new_session_id, **kwargs):
            self.bound_session = new_session_id

        def prefetch(self, query, *, session_id=""):
            reads.append(self.bound_session)
            return self.bound_session

    mgr = MemoryManager()
    mgr.add_provider(Provider())

    def read():
        try:
            results.append(mgr.prefetch_all("remember the current project", session_id="new"))
        finally:
            returned.set()

    reader = threading.Thread(target=read)
    try:
        mgr.commit_session_boundary_async([], new_session_id="new")
        assert started.wait(2)
        reader.start()
        assert not returned.wait(0.1)
        assert reads == []
        release.set()
        reader.join(3)
        assert not reader.is_alive()
        assert reads == ["new"] and results == ["new"]
    finally:
        release.set()
        if reader.ident is not None:
            reader.join(3)
        assert mgr.flush_pending(timeout=3)
        mgr.shutdown_all()


def test_turn_start_waits_for_queued_switch_before_provider_tick():
    from types import SimpleNamespace
    from agent.turn_context import _memory_turn_start_and_prefetch

    started, release, returned = (threading.Event() for _ in range(3))
    calls, result = [], []

    class Provider(_SlowProvider):
        current = "old"

        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
            started.set()
            assert release.wait(5)

        def on_session_switch(self, new_session_id, **kwargs):
            self.current = new_session_id
            calls.append("switch")

        def on_turn_start(self, turn_number, message, **kwargs):
            calls.append(("start", self.current))

        def prefetch(self, query, *, session_id=""):
            calls.append(("prefetch", self.current))
            return self.current

    mgr = MemoryManager()
    mgr.add_provider(Provider(delay=0))
    agent = SimpleNamespace(_memory_manager=mgr, session_id="new", _user_turn_count=1,
                            _emit_status=lambda text: None)

    def run_turn():
        try:
            result.append(_memory_turn_start_and_prefetch(agent, "What did we decide about deployment?"))
        finally:
            returned.set()

    turn = threading.Thread(target=run_turn)
    try:
        mgr.sync_all("old", "answer", session_id="old")
        assert started.wait(2)
        mgr.commit_session_boundary_async([], new_session_id="new")
        turn.start()
        assert not returned.wait(0.1)
        assert calls == []
        release.set()
        assert returned.wait(3)
        assert result == ["new"]
        assert calls == ["switch", ("start", "new"), ("prefetch", "new")]
    finally:
        release.set()
        if turn.ident is not None:
            turn.join(3)
        assert mgr.flush_pending(timeout=3)
        mgr.shutdown_all()


def test_session_read_fence_bounds_submission_lock_admission(monkeypatch):
    import agent.memory_manager as memory_manager_module

    monkeypatch.setattr(memory_manager_module, "_SESSION_BOUNDARY_WAIT_TIMEOUT_S", 0.02)
    mgr = MemoryManager()
    returned, result = threading.Event(), []

    def read_fence():
        try:
            result.append(mgr.wait_for_session_boundary())
        finally:
            returned.set()

    reader = threading.Thread(target=read_fence)
    mgr._sync_executor_lock.acquire()
    try:
        reader.start()
        assert returned.wait(0.2)
        assert result == [False]
    finally:
        mgr._sync_executor_lock.release()
        reader.join(3)
        mgr.shutdown_all()


def test_shutdown_drains_queued_writes_and_boundary_in_fifo_order():
    """Shutdown must not cancel durable work merely because it is still queued."""
    started = threading.Event()
    release = threading.Event()
    calls = []

    class _BlockingProvider(_SlowProvider):
        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
            if user_content == "turn-0":
                started.set()
                assert release.wait(timeout=2)
            calls.append(("sync", user_content))

        def on_session_end(self, messages):
            calls.append(("end", messages[0]["content"]))

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append(("switch", new_session_id))

    mgr = MemoryManager()
    mgr.add_provider(_BlockingProvider(delay=0))
    mgr.sync_all("turn-0", "response")
    assert started.wait(timeout=1)
    mgr.sync_all("turn-1", "response")
    mgr.commit_session_boundary_async(
        [{"role": "user", "content": "old-session"}],
        new_session_id="new-session",
    )

    threading.Timer(0.05, release.set).start()
    mgr.shutdown_all()

    assert calls == [
        ("sync", "turn-0"),
        ("sync", "turn-1"),
        ("end", "old-session"),
        ("switch", "new-session"),
    ]
    assert mgr.shutdown_drain_state["status"] == "drained"
    assert mgr.shutdown_drain_state["abandoned_writes"] == 0


def test_shutdown_timeout_abandons_queued_write_with_state_and_log(monkeypatch, caplog):
    """A wedged active write bounds shutdown and reports queued data loss."""
    import agent.memory_manager as memory_manager_module

    started = threading.Event()
    release = threading.Event()
    calls = []

    class _WedgedProvider(_SlowProvider):
        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
            if user_content == "active":
                started.set()
                release.wait(timeout=2)
            calls.append(user_content)

    monkeypatch.setattr(memory_manager_module, "_SYNC_DRAIN_TIMEOUT_S", 0.1)
    mgr = MemoryManager()
    mgr.add_provider(_WedgedProvider(delay=0))
    mgr.sync_all("active", "response")
    assert started.wait(timeout=1)
    mgr.sync_all("queued", "response")

    with caplog.at_level(logging.WARNING, logger="agent.memory_manager"):
        t0 = time.monotonic()
        mgr.shutdown_all()
        elapsed = time.monotonic() - t0

    state = mgr.shutdown_drain_state
    assert elapsed < 0.5
    assert state["status"] == "timed_out"
    assert state["abandoned_writes"] == 1
    assert "queued" not in calls
    assert "abandoning 1 queued memory write" in caplog.text
    release.set()

# Transferred accepted fallback/timeout/shutdown behavior regressions.
def test_inline_boundary_waits_for_old_prefetch_when_executor_creation_fails(monkeypatch):
    import tools.daemon_pool as daemon_pool

    started, release, switched = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class Provider(_SlowProvider):
        def queue_prefetch(self, query, *, session_id=""):
            started.set()
            assert release.wait(5)
            calls.append("old prefetch")

        def on_session_end(self, messages):
            calls.append("end")

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append("switch")
            switched.set()

    mgr = MemoryManager()
    mgr.add_provider(Provider(delay=0))
    mgr.queue_prefetch_all("old", session_id="old")
    assert started.wait(2)
    attempted = threading.Event()

    def reject(*args, **kwargs):
        attempted.set()
        raise RuntimeError("no worker")

    monkeypatch.setattr(daemon_pool, "DaemonThreadPoolExecutor", reject)
    boundary = threading.Thread(target=lambda: mgr.commit_session_boundary_async([], new_session_id="new"))
    try:
        boundary.start()
        assert attempted.wait(2)
        assert not switched.is_set()
        release.set()
        boundary.join(3)
        assert not boundary.is_alive()
        assert calls == ["old prefetch", "end", "switch"]
    finally:
        release.set()
        boundary.join(3)
        assert mgr.flush_pending(timeout=3)

def test_inline_boundary_submission_failure_fences_new_prefetch_behind_sync(monkeypatch):
    started, release, switched, prefetched = (threading.Event() for _ in range(4))
    calls = []

    class Provider(_SlowProvider):
        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
            started.set()
            assert release.wait(5)
            calls.append("sync")

        def on_session_end(self, messages):
            calls.append("end")

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append("switch")
            switched.set()

        def queue_prefetch(self, query, *, session_id=""):
            calls.append("new prefetch")
            prefetched.set()

    mgr = MemoryManager()
    mgr.add_provider(Provider(delay=0))
    mgr.sync_all("old", "answer", session_id="old")
    assert started.wait(2)
    executor = mgr._sync_executor
    assert executor is not None
    original_submit = executor.submit
    attempted = threading.Event()

    def reject(fn):
        attempted.set()
        raise RuntimeError("rejected")

    monkeypatch.setattr(executor, "submit", reject)
    boundary = threading.Thread(target=lambda: mgr.commit_session_boundary_async([], new_session_id="new"))
    try:
        boundary.start()
        assert attempted.wait(2)
        with mgr._sync_executor_lock:
            pass  # the rejected boundary has published its fallback fence
        mgr.queue_prefetch_all("new query", session_id="new")
        assert not switched.is_set()
        assert not prefetched.is_set()
        release.set()
        boundary.join(3)
        assert not boundary.is_alive()
        assert prefetched.wait(3)
        assert calls == ["sync", "end", "switch", "new prefetch"]
    finally:
        release.set()
        boundary.join(3)
        monkeypatch.setattr(executor, "submit", original_submit)
        assert mgr.flush_pending(timeout=3)

def test_inline_old_prefetch_fences_queued_switch_when_executor_creation_fails(monkeypatch):
    import tools.daemon_pool as daemon_pool

    entered, release = threading.Event(), threading.Event()
    calls = []

    class Provider(_SlowProvider):
        def queue_prefetch(self, query, *, session_id=""):
            entered.set()
            assert release.wait(5)
            calls.append("prefetch")

        def on_session_switch(self, new_session_id, **kwargs):
            calls.append("switch")

    mgr = MemoryManager()
    mgr.add_provider(Provider(delay=0))
    original = daemon_pool.DaemonThreadPoolExecutor

    def create(*args, **kwargs):
        if kwargs.get("thread_name_prefix") == "mem-prefetch":
            raise RuntimeError("no prefetch worker")
        return original(*args, **kwargs)

    monkeypatch.setattr(daemon_pool, "DaemonThreadPoolExecutor", create)
    prefetch = threading.Thread(target=lambda: mgr.queue_prefetch_all("old", session_id="old"))
    try:
        prefetch.start()
        assert entered.wait(2)
        mgr.commit_session_boundary_async([], new_session_id="new")
        assert calls == []
        release.set()
        prefetch.join(3)
        assert mgr.flush_pending(timeout=3)
        assert calls == ["prefetch", "switch"]
    finally:
        release.set()
        prefetch.join(3)

def test_turn_start_skips_stale_memory_when_boundary_wait_expires(monkeypatch):
    import agent.memory_manager as memory_manager_module
    from agent.turn_context import _memory_turn_start_and_prefetch

    started, release = threading.Event(), threading.Event()
    calls = []

    class Provider(_SlowProvider):
        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
            started.set()
            assert release.wait(5)

        def on_turn_start(self, turn_number, message, **kwargs):
            calls.append("tick")

        def prefetch(self, query, *, session_id=""):
            calls.append("prefetch")
            return "old cache"

    mgr = MemoryManager()
    mgr.add_provider(Provider(delay=0))
    mgr.sync_all("old", "answer", session_id="old")
    assert started.wait(2)
    mgr.commit_session_boundary_async([], new_session_id="new")
    monkeypatch.setattr(memory_manager_module, "_SESSION_BOUNDARY_WAIT_TIMEOUT_S", 0)
    agent = SimpleNamespace(_memory_manager=mgr, session_id="new", _user_turn_count=1)
    try:
        assert _memory_turn_start_and_prefetch(agent, "What did we decide about deployment?") == ""
        assert calls == []
        assert mgr.prefetch_all("What did we decide about deployment?", session_id="new") == ""
        assert calls == []
    finally:
        release.set()
        assert mgr.flush_pending(timeout=3)

def test_shutdown_accounts_for_queued_prefetch_on_separate_worker(monkeypatch, caplog):
    import agent.memory_manager as memory_manager_module

    started = threading.Event()
    release = threading.Event()
    calls = []

    class _WedgedProvider(_SlowProvider):
        def queue_prefetch(self, query, *, session_id=""):
            if query == "active":
                started.set()
                release.wait(timeout=5)
            calls.append(query)

    monkeypatch.setattr(memory_manager_module, "_SYNC_DRAIN_TIMEOUT_S", 0.1)
    mgr = MemoryManager()
    mgr.add_provider(_WedgedProvider(delay=0))
    try:
        mgr.queue_prefetch_all("active")
        assert started.wait(timeout=2)
        mgr.queue_prefetch_all("queued")
        with caplog.at_level(logging.WARNING, logger="agent.memory_manager"):
            mgr.shutdown_all()
        state = mgr.shutdown_drain_state
        assert state["status"] == "timed_out"
        assert state["abandoned_prefetches"] == 1
        assert state["active_tasks"] == 1
        assert "queued" not in calls
        assert "1 queued prefetch" in caplog.text
    finally:
        release.set()
