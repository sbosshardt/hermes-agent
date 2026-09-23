"""Lock audit for every call on the shared writer connection (#99349).

``SessionDB._conn`` is opened with ``check_same_thread=False`` and shared
across threads (``AsyncSessionDB`` offloads every method via
``asyncio.to_thread``), so every *call* on it must hold ``self._lock``.
A lock-free ``self._conn.execute(...)`` — even a pure SELECT — can run
concurrently with ``close()`` deallocating the connection's pysqlite
statement cache, which segfaults the interpreter (observed in the field:
``_PyDict_GetItem_KnownHash`` via ``bounded_lru_cache_wrapper`` on one
thread while ``pysqlite_connection_close`` tears the cache down on
another). "Read-only" is not an exemption: the race is on the connection
object, not the database file.

Reads that must not contend on the writer lock go through
``SessionDB._read_ctx()`` instead — it borrows a pooled read connection
(exclusively checked out for the block) and its non-WAL fallback is the
writer connection *under* ``self._lock``.

This is an AST audit in the spirit of
``tests/gateway/test_async_session_db.py``: it fails on the next
``self._conn.<method>(...)`` call site added outside ``with self._lock:``.
"""

import ast
from pathlib import Path

from tests.hermes_state._writer_lock_audit import verified_writer_with_ids

# Functions allowed to touch self._conn without the lock: construction-time
# code that runs before the instance is ever shared with another thread.
_ALLOWED_UNLOCKED_FNS = frozenset({
    "__init__",
    "_connect_and_init",
    "_connect_and_init_with_lock_patience",
})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _nearest_enclosing_fn(tree: ast.AST) -> dict:
    """Map id(node) -> name of the nearest enclosing function ("<module>"
    at module level). Nested defs override their parents."""
    enclosing: dict = {}

    def visit(node: ast.AST, current: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            current = node.name
        elif isinstance(node, ast.Lambda):
            current = "<lambda>"
        enclosing[id(node)] = current
        for child in ast.iter_child_nodes(node):
            visit(child, current)

    visit(tree, "<module>")
    return enclosing


def _unlocked_conn_calls(tree: ast.AST):
    """Return (lineno, enclosing_fn, use) for unlocked writer calls or
    direct connection handoffs inside lambdas."""
    locked_ids = set()

    def body_nodes(node):
        # A function/class defined under the lock can run after it is released.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        yield node
        for child in ast.iter_child_nodes(node):
            yield from body_nodes(child)

    verified_withs = verified_writer_with_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                ctx = item.context_expr
                if (
                    id(node) in verified_withs
                    or (isinstance(ctx, ast.Attribute)
                        and ctx.attr == "_lock"
                        and isinstance(ctx.value, ast.Name)
                        and ctx.value.id == "self")
                ):
                    locked_ids.update(id(child) for statement in node.body
                                      for child in body_nodes(statement))

    enclosing = _nearest_enclosing_fn(tree)

    offending = []
    def is_conn_attr(node):
        return (isinstance(node, ast.Attribute)
                and node.attr == "_conn"
                and isinstance(node.value, ast.Name)
                and node.value.id == "self")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_direct_call = isinstance(func, ast.Attribute) and is_conn_attr(func.value)
        # Direct connection handoffs in deferred lambdas are also unsafe;
        # the sibling sweep covers handoffs outside lambda bodies.
        is_lambda_handoff = enclosing.get(id(node)) == "<lambda>" and (
            any(is_conn_attr(arg) for arg in node.args)
            or any(is_conn_attr(keyword.value) for keyword in node.keywords)
        )
        if not (is_direct_call or is_lambda_handoff):
            continue
        if id(node) in locked_ids:
            continue
        fn = enclosing.get(id(node), "<module>")
        if fn in _ALLOWED_UNLOCKED_FNS:
            continue
        use = (f"self._conn.{func.attr}(...)" if isinstance(func, ast.Attribute) and is_direct_call
               else "self._conn passed to a deferred call")
        offending.append((node.lineno, fn, use))
    return offending


def test_every_conn_call_outside_construction_holds_the_lock():
    src = (_repo_root() / "hermes_state.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    offending = _unlocked_conn_calls(tree)
    assert not offending, (
        "self._conn.<method>() called without `with self._lock:` — this "
        "races SessionDB.close() inside pysqlite's statement cache and "
        "segfaults the process (#99349). Use `with self._read_ctx() as "
        "conn:` for reads, or take self._lock. Sites: "
        + ", ".join(
            f"line {lineno} in {fn}(): {use}"
            for lineno, fn, use in offending
        )
    )
