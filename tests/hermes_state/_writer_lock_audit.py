"""Narrow AST accommodation for SessionDB's bounded writer mutex.

Only the exact _execute_write conditional is trusted, and only while the
in-class _write_lock context manager retains its acquire/yield/finally-release
contract. This is a lexical source audit, not proof against Python rebinding,
connection aliases, or deferred callbacks.
"""

import ast
from typing import cast


_EXPECTED_WITH_NODE = cast(ast.With, ast.parse("""\
with (self._lock if lock_timeout_s is None else self._write_lock(
    min(lock_timeout_s, max(0.0, deadline - time.monotonic()))
)):
    pass
""").body[0])
_EXPECTED_WITH = _EXPECTED_WITH_NODE.items[0].context_expr

_EXPECTED_HELPER_NODE = cast(ast.FunctionDef, ast.parse("""\
@contextmanager
def _write_lock(self, timeout_s: float):
    if not self._lock.acquire(timeout=max(0.0, timeout_s)):
        raise TimeoutError("state.db local write lock unavailable for best-effort telemetry")
    try:
        yield
    finally:
        self._lock.release()
""").body[0])


def _same_shape(left, right):
    if isinstance(left, list):
        return isinstance(right, list) and len(left) == len(right) and all(
            _same_shape(a, b) for a, b in zip(left, right)
        )
    if isinstance(right, list):
        return False
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def _method(cls, name):
    matches = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name]
    return matches[0] if len(matches) == 1 else None


def verified_writer_with_ids(tree):
    """IDs of the only conditional-with whose body may use the writer conn.

    No module-wide trust of `_write_lock`: same text in another method, a
    changed conditional arm, or a changed lock helper cannot qualify.
    """
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SessionDB"]
    if len(classes) != 1:
        return set()
    cls = classes[0]
    helper = _method(cls, "_write_lock")
    writer = _method(cls, "_execute_write")
    if helper is None or writer is None:
        return set()
    body = helper.body[1:] if (helper.body and isinstance(helper.body[0], ast.Expr)
                               and isinstance(helper.body[0].value, ast.Constant)
                               and isinstance(helper.body[0].value.value, str)) else helper.body
    if (not _same_shape(helper.decorator_list, _EXPECTED_HELPER_NODE.decorator_list)
            or not _same_shape(helper.args, _EXPECTED_HELPER_NODE.args)
            or not _same_shape(body, _EXPECTED_HELPER_NODE.body)):
        return set()
    # Scope stays in the method itself: a nested function has independent
    # execution and must not inherit this method-specific exception.
    def method_nodes(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        yield node
        for child in ast.iter_child_nodes(node):
            yield from method_nodes(child)

    return {
        id(node) for statement in writer.body for node in method_nodes(statement)
        if isinstance(node, ast.With) and len(node.items) == 1
        and node.items[0].optional_vars is None
        and _same_shape(node.items[0].context_expr, _EXPECTED_WITH)
    }
