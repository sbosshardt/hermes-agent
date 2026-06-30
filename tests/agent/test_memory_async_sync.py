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


def test_sync_all_does_not_block_on_slow_provider():
    """The crux of the fix: a slow provider must NOT stall the caller.

    sync_all() dispatches through the sync executor; queue_prefetch_all()
    dispatches through a separate prefetch executor. Both return immediately.
    """
    mgr = MemoryManager()
    mgr.add_provider(_SlowProvider(delay=2.0))

    t0 = time.time()
    mgr.sync_all("hi", "hey", session_id="s1")
    mgr.queue_prefetch_all("hi", session_id="s1")
    elapsed = time.time() - t0

    # Both dispatch off-thread; must return ~instantly.
    assert elapsed < 0.5, f"turn-completion path blocked {elapsed:.2f}s"


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


def test_prefetch_executor_starts_while_sync_executor_is_blocked():
    """A slow durable sync must not starve next-turn prefetch dispatch."""
    sync_started = threading.Event()
    release_sync = threading.Event()
    prefetch_started = threading.Event()

    class _BlockingProvider(_SlowProvider):
        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
            sync_started.set()
            assert release_sync.wait(timeout=2)

        def queue_prefetch(self, query, *, session_id=""):
            prefetch_started.set()

    mgr = MemoryManager()
    mgr.add_provider(_BlockingProvider(delay=0))
    mgr.sync_all("hi", "hey", session_id="s1")
    assert sync_started.wait(timeout=1)

    mgr.queue_prefetch_all("next turn", session_id="s1")

    assert prefetch_started.wait(timeout=1)
    release_sync.set()
    assert mgr.flush_pending(timeout=2) is True


def test_shutdown_timeout_reports_abandoned_queued_prefetch(monkeypatch, caplog):
    """Shutdown reports queued prefetch loss from the separate executor."""
    import agent.memory_manager as memory_manager_module

    started = threading.Event()
    release = threading.Event()
    calls = []

    class _WedgedPrefetchProvider(_SlowProvider):
        def queue_prefetch(self, query, *, session_id=""):
            if query == "active":
                started.set()
                release.wait(timeout=2)
            calls.append(query)

    monkeypatch.setattr(memory_manager_module, "_SYNC_DRAIN_TIMEOUT_S", 0.1)
    mgr = MemoryManager()
    mgr.add_provider(_WedgedPrefetchProvider(delay=0))
    mgr.queue_prefetch_all("active")
    assert started.wait(timeout=1)
    mgr.queue_prefetch_all("queued")

    with caplog.at_level(logging.WARNING, logger="agent.memory_manager"):
        mgr.shutdown_all()

    state = mgr.shutdown_drain_state
    assert state["status"] == "timed_out"
    assert state["abandoned_prefetches"] == 1
    assert state["active_tasks"] == 1
    assert "queued" not in calls
    assert "1 queued prefetch" in caplog.text
    release.set()










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
