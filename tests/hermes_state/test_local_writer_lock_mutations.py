"""Current-source mutants must fail both lexical audits at the intended site.

Mutants are parsed, never imported/executed, and written only below tmp_path.
No historical conditional-with anchor, runtime patch, or removed test is used.
"""

import ast
import copy
from pathlib import Path
from typing import cast

import pytest

from tests.hermes_state._local_writer_lock_audit import (
    EXPECTED_CONTEXT, same_shape, source_trees, unique_class, unique_method,
    verified_writer_with_ids,
)
from tests.hermes_state.test_local_state_writer_lexical_audit import (
    Audit, AuditViolation, Site, _repo_root, assert_clean, scan_sources,
)


# Baseline lives in test_local_state_writer_lexical_audit.py, once per scanner.
KINDS = (
    "ordinary_unlocked_call", "ordinary_unlocked_handoff",
    "bounded_lookalike_other_method", "bounded_lookalike_nested_method",
    "deferred_def_call", "deferred_async_call", "deferred_def_alias_handoff",
    "deferred_lambda_call", "deferred_lambda_handoff", "deferred_lambda_alias_call",
    "deferred_lambda_alias_handoff", "deferred_lambda_reference",
    "deferred_class_call", "nested_construction_name", "ordinary_deferred_def",
    "ordinary_deferred_lambda", "mixed_immediate_and_deferred_handoff",
    "ordinary_context_removed", "changed_bounded_context", "changed_bounded_arm",
    "missing_finally", "broken_finally_release", "wrong_finally_mutex",
    "missing_acquisition", "wrong_acquisition_mutex", "ignored_acquisition_result",
    "duplicate_helper", "rebound_helper", "replacement_helper_class",
    "instance_helper_replacement", "module_helper_replacement",
    "duplicate_bounded_method", "rebound_bounded_method", "duplicate_sessiondb_class",
)

DEFERRED_PROBES = {
    "deferred_def_call": ("def audit_deferred():\n    self._conn.execute('AUDIT_DEFERRED')", "audit_deferred", "self._conn.execute('AUDIT_DEFERRED')"),
    "deferred_async_call": ("async def audit_deferred():\n    self._conn.execute('AUDIT_DEFERRED')", "audit_deferred", "self._conn.execute('AUDIT_DEFERRED')"),
    "deferred_def_alias_handoff": ("def audit_deferred():\n    return fn(conn)", "audit_deferred", "fn(conn)"),
    "deferred_lambda_call": ("callback = lambda: self._conn.execute('AUDIT_DEFERRED')", "<lambda>", "self._conn.execute('AUDIT_DEFERRED')"),
    "deferred_lambda_handoff": ("callback = lambda: fn(self._conn)", "<lambda>", "fn(self._conn)"),
    "deferred_lambda_alias_call": ("callback = lambda: conn.execute('AUDIT_DEFERRED')", "<lambda>", "conn.execute('AUDIT_DEFERRED')"),
    "deferred_lambda_alias_handoff": ("callback = lambda: fn(conn)", "<lambda>", "fn(conn)"),
    "deferred_lambda_reference": ("callback = lambda: self._conn", "<lambda>", "self._conn"),
    "deferred_class_call": ("class AuditDeferred:\n    self._conn.execute('AUDIT_DEFERRED')", "AuditDeferred", "self._conn.execute('AUDIT_DEFERRED')"),
    "nested_construction_name": ("def _connect_and_init():\n    self._conn.execute('AUDIT_DEFERRED')", "_connect_and_init", "self._conn.execute('AUDIT_DEFERRED')"),
    "ordinary_deferred_def": ("def audit_deferred():\n    self._conn.execute('AUDIT_DEFERRED')", "audit_deferred", "self._conn.execute('AUDIT_DEFERRED')"),
    "ordinary_deferred_lambda": ("callback = lambda: fn(self._conn)", "<lambda>", "fn(self._conn)"),
    "mixed_immediate_and_deferred_handoff": ("fn(self._conn, lambda: self._conn)", "<lambda>", "self._conn"),
}


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    method = unique_method(cls, name)
    assert method is not None, f"current-source method identity changed: {name}"
    return method


def _bounded(writer: ast.FunctionDef) -> ast.With:
    matches = [node for node in writer.body if isinstance(node, ast.With)
               and len(node.items) == 1 and same_shape(node.items[0].context_expr, EXPECTED_CONTEXT)]
    assert len(matches) == 1, "current-source bounded context changed"
    return matches[0]


def _normal_lock(writer: ast.FunctionDef) -> ast.With:
    expression = ast.parse("self._lock", mode="eval").body
    matches = [node for node in ast.walk(writer) if isinstance(node, ast.With)
               and any(same_shape(item.context_expr, expression) for item in node.items)
               and any(isinstance(child, ast.Call) and ast.unparse(child) == "self._conn.execute('BEGIN IMMEDIATE')"
                       for child in ast.walk(node))]
    assert len(matches) == 1, "current-source normal transaction mutex changed"
    return matches[0]


def _helper_parts(helper: ast.FunctionDef) -> tuple[ast.If, ast.Try]:
    admission = [node for node in helper.body if isinstance(node, ast.If)]
    release = [node for node in helper.body if isinstance(node, ast.Try)]
    assert len(admission) == len(release) == 1, "current-source helper contract changed"
    return admission[0], release[0]


def _mutate_context_or_helper(kind: str, tree: ast.Module, cls: ast.ClassDef,
                              writer: ast.FunctionDef, helper: ast.FunctionDef) -> None:
    bounded = _bounded(writer)
    admission, release = _helper_parts(helper)
    if kind == "changed_bounded_context":
        bounded.items[0].context_expr = ast.parse("nullcontext()", mode="eval").body
    elif kind == "changed_bounded_arm":
        bounded.items[0].context_expr = ast.IfExp(
            test=ast.parse("lock_timeout_s is None", mode="eval").body,
            body=ast.parse("nullcontext()", mode="eval").body,
            orelse=copy.deepcopy(EXPECTED_CONTEXT),
        )
    elif kind == "missing_finally":
        index = helper.body.index(release)
        helper.body[index:index + 1] = [*release.body, *release.finalbody]
    elif kind == "broken_finally_release":
        release.finalbody = [ast.Pass()]
    elif kind == "wrong_finally_mutex":
        release.finalbody = ast.parse("self._read_conns_lock.release()").body
    elif kind == "missing_acquisition":
        helper.body.remove(admission)
    elif kind == "wrong_acquisition_mutex":
        admission.test = ast.parse("not self._read_conns_lock.acquire(timeout=max(0.0, timeout_s))", mode="eval").body
    elif kind == "ignored_acquisition_result":
        helper.body[helper.body.index(admission)] = ast.parse("self._lock.acquire(timeout=max(0.0, timeout_s))").body[0]
    elif kind == "duplicate_helper":
        cls.body.append(copy.deepcopy(helper))
    elif kind == "rebound_helper":
        cls.body.extend(ast.parse("_write_lock = lambda *args: nullcontext()").body)
    elif kind == "replacement_helper_class":
        cls.body.extend(ast.parse("class _write_lock:\n    pass").body)
    elif kind == "instance_helper_replacement":
        _method(cls, "__init__").body.append(ast.parse("self._write_lock = lambda *args: nullcontext()").body[0])
    elif kind == "module_helper_replacement":
        tree.body.extend(ast.parse("SessionDB._write_lock = lambda *args: nullcontext()").body)
    elif kind == "duplicate_bounded_method":
        cls.body.append(copy.deepcopy(writer))
    elif kind == "rebound_bounded_method":
        cls.body.extend(ast.parse("_execute_best_effort_write = lambda *args: None").body)
    elif kind == "duplicate_sessiondb_class":
        tree.body.extend(ast.parse("class SessionDB:\n    pass").body)
    else:
        raise ValueError(kind)


def _source_variant(source: str, kind: str) -> tuple[str, tuple[tuple[str, str], ...], bool]:
    tree = ast.parse(source)
    cls = unique_class(tree, "SessionDB")
    assert cls is not None
    writer = _method(cls, "_execute_best_effort_write")
    helper = _method(cls, "_write_lock")
    normal = _method(cls, "_execute_write")
    bounded = _bounded(writer)
    # This binds all variants to the actual safe candidate, including its leading
    # helper docstring. No fabricated baseline and no old conditional anchor.
    assert verified_writer_with_ids(tree) == {id(bounded)}
    _normal_lock(normal)
    target_scope = "SessionDB._execute_best_effort_write"
    trust_remains = True
    if kind in {"ordinary_unlocked_call", "ordinary_unlocked_handoff"}:
        expression = ("self._conn.execute('AUDIT_UNLOCKED')" if kind.endswith("call") else "fn(self._conn)")
        cls.body.extend(ast.parse(f"def audit_probe(self, fn):\n    {expression}").body)
        targets = (("SessionDB.audit_probe", expression),)
    elif kind in {"bounded_lookalike_other_method", "bounded_lookalike_nested_method"}:
        probe = cast(ast.FunctionDef, ast.parse('''
def audit_probe(self, fn, admission, deadline):
    with self._write_lock(min(admission, max(0.0, deadline - time.monotonic()))):
        self._conn.execute('AUDIT_LOOKALIKE')
        fn(self._conn)
''').body[0])
        if kind == "bounded_lookalike_other_method":
            cls.body.append(probe)
            scope = "SessionDB.audit_probe"
        else:
            writer.body.append(probe)
            scope = target_scope + ".audit_probe"
        targets = ((scope, "self._conn.execute('AUDIT_LOOKALIKE')"), (scope, "fn(self._conn)"))
    elif kind in DEFERRED_PROBES:
        snippet, suffix, expression = DEFERRED_PROBES[kind]
        host = _normal_lock(normal) if kind.startswith("ordinary_deferred") else bounded
        owner = "SessionDB._execute_write" if kind.startswith("ordinary_deferred") else target_scope
        # Alias capture occurs after the actual conn = self._conn acquisition.
        acquisitions = [index for index, node in enumerate(host.body)
                        if isinstance(node, ast.Assign) and ast.unparse(node) == "conn = self._conn"]
        index = acquisitions[0] + 1 if acquisitions else 0
        host.body[index:index] = ast.parse(snippet).body
        targets = ((owner + "." + suffix, expression),)
    elif kind == "ordinary_context_removed":
        _normal_lock(normal).items[0].context_expr = ast.parse("nullcontext()", mode="eval").body
        targets = (("SessionDB._execute_write", "self._conn.execute('BEGIN IMMEDIATE')"),
                   ("SessionDB._execute_write", "fn(self._conn)"),
                   ("SessionDB._execute_write", "self._conn.commit()"),
                   ("SessionDB._execute_write", "self._conn.rollback()"))
    else:
        _mutate_context_or_helper(kind, tree, cls, writer, helper)
        trust_remains = False
        # BOTH scanners must report actual native alias use/handoffs, not just
        # the helper's syntactic mismatch or an unrelated construction exception.
        targets = tuple((target_scope, expression) for expression in (
            "self._nonblocking_sqlite_writer(conn)", "conn.execute('BEGIN IMMEDIATE')",
            "fn(conn)", "conn.commit()", "conn.rollback()",
        ))
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n", targets, trust_remains


def _expected_sites(tree: ast.Module, targets: tuple[tuple[str, str], ...]) -> set[tuple[int, str, str]]:
    """Independent target locator: no scanner metadata or lock reasoning is reused."""
    wanted = set(targets)
    found: set[tuple[int, str, str]] = set()

    def visit(node: ast.AST, path: tuple[str, ...]) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            path += (node.name,)
        elif isinstance(node, ast.Lambda):
            path += ("<lambda>",)
        expression = ast.unparse(node)
        scope = ".".join(path)
        if isinstance(node, ast.expr) and (scope, expression) in wanted:
            found.add((getattr(node, "lineno"), scope, expression))
        for child in ast.iter_child_nodes(node):
            visit(child, path)

    visit(tree, ())
    assert {(scope, expression) for _, scope, expression in found} == wanted, "mutation target was not created"
    return found


def _site_keys(sites: list[Site]) -> set[tuple[int, str, str]]:
    return {(site.line, site.scope, site.expression) for site in sites if site.module == "hermes_state.py"}


@pytest.mark.parametrize("audit", ["calls", "uses"])
@pytest.mark.parametrize("kind", KINDS)
def test_current_source_mutant_is_refused_at_its_writer_boundary(
    audit: Audit, kind: str, tmp_path: Path,
) -> None:
    root = _repo_root()
    source = (root / "hermes_state.py").read_text(encoding="utf-8")
    mutant, targets, trust_remains = _source_variant(source, kind)
    path = tmp_path / "hermes_state.py"
    path.write_text(mutant, encoding="utf-8")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert bool(verified_writer_with_ids(tree)) is trust_remains
    expected = _expected_sites(tree, targets)
    # Unmodified actual defining siblings remain in the scan. Discovery uses the
    # real source, so duplicate/rebound mutant identities cannot cause a fixture
    # assertion to masquerade as lexical refusal.
    trees = source_trees(root)
    trees["hermes_state.py"] = tree
    sites = scan_sources(trees, audit)
    assert expected <= _site_keys(sites), f"{audit}/{kind}: intended unlocked sites missing; got {sites!r}"
    assert all(site.module == "hermes_state.py" for site in sites), "unrelated sibling failure"
    with pytest.raises(AuditViolation) as refusal:
        assert_clean(trees, audit)
    assert refusal.value.sites == sites
    assert expected <= _site_keys(refusal.value.sites)
