"""Real temporary SQLite current-thread writer ownership for ordinary and telemetry paths."""
import sqlite3
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
        assert self.owner == threading.get_ident(), 'non-owner released writer mutex'
        self.owner = None
        self.lock.release()

    def __enter__(self):
        assert self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class CheckedConnection:
    def __init__(self, conn, owner_lock):
        self.conn = conn
        self.owner_lock = owner_lock
        self.calls = []

    def _record(self, name):
        assert self.owner_lock.owner == threading.get_ident(), name
        self.calls.append(name)

    def __getattr__(self, name):
        self._record(name)
        return getattr(self.conn, name)

    def execute(self, *args, **kwargs):
        self._record('execute')
        return self.conn.execute(*args, **kwargs)

    def commit(self):
        self._record('commit')
        return self.conn.commit()

    def rollback(self):
        self._record('rollback')
        return self.conn.rollback()


@pytest.fixture
def guarded_db(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('audit-owner', 'cli')
    original_lock, original_conn = db._lock, db._conn
    guard = OwnerLock(original_lock)
    conn = CheckedConnection(original_conn, guard)
    setattr(db, '_lock', guard)
    setattr(db, '_conn', conn)
    try:
        yield db, guard, conn
    finally:
        setattr(db, '_lock', original_lock)
        setattr(db, '_conn', original_conn)
        db.close()


def test_ordinary_callback_execute_commit_are_owned(guarded_db):
    db, guard, conn = guarded_db
    seen = []
    def write(writer):
        assert writer is conn and guard.owner == threading.get_ident()
        seen.append('callback')
        writer.execute('CREATE TABLE audit_probe (value TEXT)')
        writer.execute("INSERT INTO audit_probe VALUES ('committed')")
        return 'ok'
    assert db._execute_write(write) == 'ok'
    assert seen == ['callback']
    assert 'commit' in conn.calls and 'rollback' not in conn.calls
    assert guard.owner is None
    with db._lock:
        assert conn.execute('SELECT value FROM audit_probe').fetchone()[0] == 'committed'


def test_ordinary_failure_rolls_back_under_owner(guarded_db):
    db, guard, conn = guarded_db
    with db._lock:
        conn.execute('CREATE TABLE audit_probe (value TEXT)')
        conn.commit()
    conn.calls.clear()
    def broken(writer):
        assert writer is conn and guard.owner == threading.get_ident()
        writer.execute("INSERT INTO audit_probe VALUES ('rolled back')")
        raise ValueError('callback failed')
    with pytest.raises(ValueError, match='callback failed'):
        db._execute_write(broken)
    assert 'rollback' in conn.calls and 'commit' not in conn.calls
    assert guard.owner is None
    with db._lock:
        assert conn.execute('SELECT count(*) FROM audit_probe').fetchone()[0] == 0


def test_telemetry_execute_and_commit_are_owned(guarded_db):
    db, guard, conn = guarded_db
    conn.calls.clear()
    db.update_auto_recall_metrics('audit-owner', attempts=1, latency_ms=7)
    assert 'execute' in conn.calls and 'commit' in conn.calls and 'rollback' not in conn.calls
    assert guard.owner is None
    assert db.get_session('audit-owner')['auto_recall_attempt_count'] == 1


def test_telemetry_failure_rolls_back_under_owner(guarded_db):
    db, guard, conn = guarded_db
    with db._lock:
        conn.execute("CREATE TRIGGER audit_failure BEFORE UPDATE OF auto_recall_attempt_count "
                     "ON sessions BEGIN SELECT RAISE(FAIL, 'audit rollback'); END")
        conn.commit()
    conn.calls.clear()
    with pytest.raises(sqlite3.IntegrityError, match='audit rollback'):
        db.update_auto_recall_metrics('audit-owner', attempts=1)
    assert 'rollback' in conn.calls and 'commit' not in conn.calls
    assert guard.owner is None
    assert db.get_session('audit-owner')['auto_recall_attempt_count'] == 0


def test_telemetry_refuses_contention_before_connection_access(guarded_db):
    db, guard, conn = guarded_db
    entered, release = threading.Event(), threading.Event()
    errors = []
    def holder():
        try:
            with db._lock:
                entered.set()
                assert release.wait(timeout=5)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        assert guard.owner == thread.ident != threading.get_ident()
        before = list(conn.calls)
        with pytest.raises(TimeoutError, match='optional session telemetry lock busy'):
            db.update_auto_recall_metrics('audit-owner', attempts=1)
        assert conn.calls == before
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    assert guard.owner is None
    assert db.get_session('audit-owner')['auto_recall_attempt_count'] == 0
