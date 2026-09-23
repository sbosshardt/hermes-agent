"""Mutation tests for the single, source-verified writer-lock audit exception.

These are lexical AST checks, not a proof against dynamic rebinding or aliases.
"""

import inspect
from pathlib import Path

import pytest

from tests.hermes_state import test_hermes_state_conn_lock_audit as call_audit
from tests.hermes_state import test_writer_conn_thread_safety as usage_audit


SOURCE = Path(call_audit.__file__).resolve().parents[2] / "hermes_state.py"
CONDITIONAL = "with (self._lock if lock_timeout_s is None else self._write_lock("


def _source_variant(kind):
    source = SOURCE.read_text(encoding="utf-8")
    assert source.count(CONDITIONAL) == 1
    if kind == "ordinary_unlocked":
        source = source.replace(
            "    def _execute_write(\n",
            "    def audit_probe(self):\n"
            "        self._conn.execute('SELECT 1')\n\n"
            "    def _execute_write(\n",
            1,
        )
    elif kind == "conditional_in_other_method":
        source = source.replace(
            "    def _execute_write(\n",
            "    def audit_probe(self, lock_timeout_s):\n"
            "        with (self._lock if lock_timeout_s is None else self._write_lock(\n"
            "            min(lock_timeout_s, max(0.0, deadline - time.monotonic()))\n"
            "        )):\n"
            "            self._conn.execute('SELECT 1')\n\n"
            "    def _execute_write(\n",
            1,
        )
    elif kind == "nested_conditional":
        source = source.replace(
            "        if patience_s is None:\n",
            "        def audit_probe(lock_timeout_s):\n"
            "            with (self._lock if lock_timeout_s is None else self._write_lock(\n"
            "                min(lock_timeout_s, max(0.0, deadline - time.monotonic()))\n"
            "            )):\n"
            "                self._conn.execute('SELECT 1')\n"
            "        if patience_s is None:\n",
            1,
        )
    elif kind == "deferred_call_inside_conditional":
        before = "                )):\n                    if best_effort:\n"
        after = ("                )):\n"
                 "                    def audit_probe():\n"
                 "                        self._conn.execute('SELECT 1')\n"
                 "                    if best_effort:\n")
        assert before in source
        source = source.replace(before, after, 1)
    elif kind == "deferred_lambda_inside_conditional":
        before = "                )):\n                    if best_effort:\n"
        after = ("                )):\n"
                 "                    callback = lambda: fn(self._conn)\n"
                 "                    if best_effort:\n")
        assert before in source
        source = source.replace(before, after, 1)
    elif kind == "changed_conditional_arm":
        source = source.replace(CONDITIONAL, "with (nullcontext() if lock_timeout_s is None else self._write_lock(", 1)
    elif kind == "broken_helper":
        before = "        finally:\n            self._lock.release()\n\n    @contextmanager\n    def _nonblocking_sqlite_writer"
        after = "        finally:\n            pass\n\n    @contextmanager\n    def _nonblocking_sqlite_writer"
        assert before in source
        source = source.replace(before, after, 1)
    elif kind != "real":
        raise ValueError(kind)
    return source


@pytest.mark.parametrize("audit", ["calls", "uses"])
@pytest.mark.parametrize("kind", [
    "real", "ordinary_unlocked", "conditional_in_other_method",
    "nested_conditional", "deferred_call_inside_conditional",
    "deferred_lambda_inside_conditional", "changed_conditional_arm", "broken_helper",
])
def test_writer_lock_audit_only_trusts_verified_execute_write(
    audit, kind, tmp_path, monkeypatch,
):
    source = _source_variant(kind)
    if audit == "calls":
        (tmp_path / "hermes_state.py").write_text(source, encoding="utf-8")
        monkeypatch.setattr(call_audit, "_repo_root", lambda: tmp_path)
        run_audit = call_audit.test_every_conn_call_outside_construction_holds_the_lock
    else:
        monkeypatch.setattr(inspect, "getsource", lambda _: source)
        run_audit = usage_audit.TestConcurrentReadersDoNotRaceTheWriter().test_unlocked_writer_conn_use_is_enumerated
    if kind == "real":
        run_audit()
    else:
        with pytest.raises(AssertionError):
            run_audit()
