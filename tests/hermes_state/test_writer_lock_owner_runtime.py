"""Real temp-SQLite writer transactions exercise owner-thread mutex admission."""

import threading

import pytest

from hermes_state import SessionDB


class OwnerLock:
    def __init__(self, lock):
        self.lock = lock
        self.owner = None

    def acquire(self, *args, **kwargs):
        acquired = self.lock.acquire(*args, **kwargs)
        if acquired:
            self.owner = threading.get_ident()
        return acquired

    def release(self):
        assert self.owner == threading.get_ident(), "non-owner released writer mutex"
        self.owner = None
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class CheckedConnection:
    def __init__(self, conn, owner_lock):
        self.conn = conn
        self.owner_lock = owner_lock
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def _record(self, name):
        assert self.owner_lock.owner == threading.get_ident(), name
        self.calls.append(name)

    def execute(self, *args, **kwargs):
        self._record("execute")
        return self.conn.execute(*args, **kwargs)

    def commit(self):
        self._record("commit")
        return self.conn.commit()

    def rollback(self):
        self._record("rollback")
        return self.conn.rollback()


@pytest.fixture
def guarded_db(tmp_path):
    db = SessionDB(db_path=tmp_path / "writer.db")
    original_lock, original_conn = db._lock, db._conn
    guard = OwnerLock(original_lock)
    conn = CheckedConnection(original_conn, guard)
    db._lock, db._conn = guard, conn
    yield db, guard, conn
    db._lock, db._conn = original_lock, original_conn
    db.close()


@pytest.mark.parametrize("bounded", [False, True])
def test_execute_callback_and_commit_run_on_current_lock_owner(guarded_db, bounded):
    db, guard, conn = guarded_db
    seen = []

    def write(writer):
        assert writer is conn
        assert guard.owner == threading.get_ident()
        seen.append("callback")
        writer.execute("CREATE TABLE IF NOT EXISTS audit_probe (value TEXT)")
        writer.execute("INSERT INTO audit_probe VALUES ('committed')")
        return "ok"

    kwargs = {"lock_timeout_s": 0.2} if bounded else {}
    assert db._execute_write(write, **kwargs) == "ok"
    assert seen == ["callback"]
    assert "commit" in conn.calls and "rollback" not in conn.calls
    assert guard.owner is None
    with db._lock:
        assert conn.execute("SELECT value FROM audit_probe").fetchone()[0] == "committed"


@pytest.mark.parametrize("bounded", [False, True])
def test_callback_failure_rolls_back_under_current_lock_owner(guarded_db, bounded):
    db, guard, conn = guarded_db
    with db._lock:
        conn.execute("CREATE TABLE audit_probe (value TEXT)")
        conn.commit()
    conn.calls.clear()

    def broken(writer):
        assert guard.owner == threading.get_ident()
        writer.execute("INSERT INTO audit_probe VALUES ('rolled back')")
        raise ValueError("callback failed")

    kwargs = {"lock_timeout_s": 0.2} if bounded else {}
    with pytest.raises(ValueError, match="callback failed"):
        db._execute_write(broken, **kwargs)
    assert "rollback" in conn.calls and "commit" not in conn.calls
    assert guard.owner is None
    with db._lock:
        assert conn.execute("SELECT count(*) FROM audit_probe").fetchone()[0] == 0


def test_bounded_lock_contention_times_out_before_any_writer_connection_use(guarded_db):
    db, guard, conn = guarded_db
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with db._lock:
            entered.set()
            assert release.wait(timeout=5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        assert guard.owner == thread.ident != threading.get_ident()
        before = list(conn.calls)
        called = []
        with pytest.raises(TimeoutError, match="local write lock unavailable"):
            db._execute_write(lambda writer: called.append(writer), lock_timeout_s=0.01, patience_s=0.03)
        assert called == []
        assert conn.calls == before
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert guard.owner is None
