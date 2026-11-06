"""Parsed-only current-source mutants: both sweeps must identify their intended site.

No mutant is imported or executed; no production store, host signal or service is touched.
"""
import ast
import copy
from pathlib import Path
from typing import cast

import pytest

from tests.hermes_state._local_writer_lock_audit import (
    shape, source_trees, unique_class, unique_function, unique_method, verified_telemetry_with_id,
)
from tests.hermes_state.test_local_state_writer_lexical_audit import (
    Audit, AuditViolation, Site, _repo_root, assert_clean, scan_sources,
)

KINDS = (
    'ordinary_unlocked_call', 'ordinary_unlocked_handoff', 'ordinary_context_removed',
    'ordinary_context_evaluated_before_lock', 'ordinary_deferred_def',
    'ordinary_deferred_async', 'ordinary_deferred_lambda', 'ordinary_deferred_class',
    'telemetry_unlocked_call', 'telemetry_unlocked_handoff', 'telemetry_alias_before_lock',
    'telemetry_context_removed', 'telemetry_context_evaluated_before_lock',
    'telemetry_lookalike_other_function',
    'telemetry_helper_wrong_mutex', 'telemetry_helper_missing_acquire',
    'telemetry_helper_ignored_acquire', 'telemetry_helper_missing_release',
    'telemetry_helper_wrong_release', 'telemetry_helper_duplicate',
    'telemetry_helper_rebound', 'telemetry_deferred_def', 'telemetry_deferred_lambda',
    'telemetry_deferred_class',
)


def _normal_lock(method: ast.FunctionDef) -> ast.With:
    matches = [n for n in ast.walk(method) if isinstance(n, ast.With)
               and any(shape(item.context_expr, ast.parse('self._lock', mode='eval').body) for item in n.items)
               and any(isinstance(c, ast.Call) and ast.unparse(c) == "self._conn.execute('BEGIN IMMEDIATE')" for c in ast.walk(n))]
    assert len(matches) == 1, 'native transaction mutex changed'
    return matches[0]


def _telemetry_lock(owner: ast.FunctionDef) -> ast.With:
    matches = [n for n in owner.body if isinstance(n, ast.With)
               and len(n.items) == 1 and shape(n.items[0].context_expr, ast.parse('_bounded_telemetry_lock(db)', mode='eval').body)]
    assert len(matches) == 1, 'native telemetry context changed'
    return matches[0]


def _variant(module: str, kind: str, source: str) -> tuple[str, tuple[str, str]]:
    tree = ast.parse(source)
    telemetry = module == 'hermes_state_telemetry.py'
    cls: ast.ClassDef | None = None
    helper: ast.FunctionDef | None = None
    if telemetry:
        owner = unique_function(tree.body, 'update_auto_recall_metrics')
        helper = unique_function(tree.body, '_bounded_telemetry_lock')
        assert owner is not None and helper is not None
        lock = _telemetry_lock(owner)
        assert verified_telemetry_with_id(tree) == {id(lock)}
        scope = 'update_auto_recall_metrics'
        sql = "conn.execute('PRAGMA busy_timeout')"
    else:
        cls = unique_class(tree, 'SessionDB')
        assert cls is not None
        owner = unique_method(cls, '_execute_write')
        assert owner is not None
        lock = _normal_lock(owner)
        scope = 'SessionDB._execute_write'
        sql = "self._conn.execute('BEGIN IMMEDIATE')"
    if kind.endswith('unlocked_call') or kind.endswith('unlocked_handoff'):
        if telemetry:
            expr = "db._conn.execute('AUDIT_UNLOCKED')" if kind.endswith('call') else 'fn(db._conn)'
            owner.body.insert(owner.body.index(lock), ast.parse(expr).body[0])
        else:
            expr = "self._conn.execute('AUDIT_UNLOCKED')" if kind.endswith('call') else 'fn(self._conn)'
            assert cls is not None
            cls.body.extend(ast.parse(f'def audit_probe(self, fn):\n    {expr}').body)
            scope = 'SessionDB.audit_probe'
    elif kind == 'telemetry_lookalike_other_function':
        tree.body.extend(ast.parse('''
def audit_probe(db):
    with _bounded_telemetry_lock(db):
        db._conn.execute('AUDIT_LOOKALIKE')
''').body)
        scope, expr = 'audit_probe', "db._conn.execute('AUDIT_LOOKALIKE')"
    elif kind == 'telemetry_alias_before_lock':
        # Alias acquired before entry; the independent USE sweep also flags acquisition.
        owner.body.insert(owner.body.index(lock), ast.parse("conn.execute('AUDIT_PRELOCK')").body[0])
        expr = "conn.execute('AUDIT_PRELOCK')"
    elif kind.endswith('context_removed'):
        lock.items[0].context_expr = ast.parse('nullcontext()', mode='eval').body
        expr = sql
    elif kind.endswith('context_evaluated_before_lock'):
        # Python evaluates the first manager before entering the second.
        pre = 'db._conn.execute(\'AUDIT_CONTEXT\')' if telemetry else 'self._conn.execute(\'AUDIT_CONTEXT\')'
        lock.items.insert(0, ast.withitem(context_expr=ast.parse(pre, mode='eval').body, optional_vars=None))
        expr = pre
    elif kind.startswith('telemetry_helper_'):
        assert helper is not None
        admission = cast(ast.If, helper.body[1])
        settlement = cast(ast.Try, helper.body[2])
        if kind == 'telemetry_helper_wrong_mutex':
            admission.test = ast.parse('not db._read_conns_lock.acquire(timeout=_LOCAL_LOCK_TIMEOUT_S)', mode='eval').body
        elif kind == 'telemetry_helper_missing_acquire':
            helper.body[1:2] = []
        elif kind == 'telemetry_helper_ignored_acquire':
            helper.body[1:2] = ast.parse('db._lock.acquire(timeout=_LOCAL_LOCK_TIMEOUT_S)').body
        elif kind == 'telemetry_helper_missing_release':
            settlement.finalbody = [ast.Pass()]
        elif kind == 'telemetry_helper_wrong_release':
            settlement.finalbody = ast.parse('db._read_conns_lock.release()').body
        elif kind == 'telemetry_helper_duplicate':
            tree.body.append(copy.deepcopy(helper))
        elif kind == 'telemetry_helper_rebound':
            tree.body.extend(ast.parse('_bounded_telemetry_lock = lambda db: nullcontext()').body)
        expr = sql
    else:
        deferred = {
            'ordinary_deferred_def': ("def audit_deferred():\n    self._conn.execute('AUDIT_DEFERRED')", 'audit_deferred', "self._conn.execute('AUDIT_DEFERRED')"),
            'ordinary_deferred_async': ("async def audit_deferred():\n    self._conn.execute('AUDIT_DEFERRED')", 'audit_deferred', "self._conn.execute('AUDIT_DEFERRED')"),
            'ordinary_deferred_lambda': ("callback = lambda: self._conn.execute('AUDIT_DEFERRED')", '<lambda>', "self._conn.execute('AUDIT_DEFERRED')"),
            'ordinary_deferred_class': ("class AuditDeferred:\n    self._conn.execute('AUDIT_DEFERRED')", 'AuditDeferred', "self._conn.execute('AUDIT_DEFERRED')"),
            'telemetry_deferred_def': ("def audit_deferred():\n    conn.execute('AUDIT_DEFERRED')", 'audit_deferred', "conn.execute('AUDIT_DEFERRED')"),
            'telemetry_deferred_lambda': ("callback = lambda: conn.execute('AUDIT_DEFERRED')", '<lambda>', "conn.execute('AUDIT_DEFERRED')"),
            'telemetry_deferred_class': ("class AuditDeferred:\n    conn.execute('AUDIT_DEFERRED')", 'AuditDeferred', "conn.execute('AUDIT_DEFERRED')"),
        }
        snippet, suffix, expr = deferred[kind]
        lock.body.append(ast.parse(snippet).body[0])
        scope += '.' + suffix
    return ast.unparse(ast.fix_missing_locations(tree)), (scope, expr)


def _target(tree: ast.Module, scope: str, expression: str) -> tuple[int, str, str]:
    found = []
    def visit(node: ast.AST, path: tuple[str, ...]):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            path += (node.name,)
        elif isinstance(node, ast.Lambda):
            path += ('<lambda>',)
        if isinstance(node, ast.expr) and '.'.join(path) == scope and ast.unparse(node) == expression:
            found.append((node.lineno, scope, expression))
        for child in ast.iter_child_nodes(node):
            visit(child, path)
    visit(tree, ())
    assert len(found) == 1, f'expected exactly one independent target: {scope} {expression}'
    return found[0]


def _keys(sites: list[Site], module: str) -> set[tuple[int, str, str]]:
    return {(site.line, site.scope, site.expression) for site in sites if site.module == module}


@pytest.mark.parametrize('audit', ['calls', 'uses'])
@pytest.mark.parametrize('kind', KINDS)
def test_mutant_refused_at_intended_writer_site(audit: Audit, kind: str, tmp_path: Path):
    module = 'hermes_state_telemetry.py' if kind.startswith('telemetry_') else 'hermes_state.py'
    root = _repo_root()
    mutant, (scope, expression) = _variant(module, kind, (root / module).read_text(encoding='utf-8'))
    path = tmp_path / module
    path.write_text(mutant, encoding='utf-8')
    tree = ast.parse(path.read_text(encoding='utf-8'))
    expected = _target(tree, scope, expression)
    trees = source_trees(root)
    trees[module] = tree
    sites = scan_sources(trees, audit)
    assert expected in _keys(sites, module), f'{audit}/{kind}: missing intended site: {sites!r}'
    assert all(site.module == module for site in sites), 'unrelated source violation'
    with pytest.raises(AuditViolation) as refusal:
        assert_clean(trees, audit)
    assert expected in _keys(refusal.value.sites, module)
