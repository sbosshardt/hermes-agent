"""Independent lexical CALL and USE sweeps over real current writer owners.

Source-only exception to upstream's behavior-test preference: this bounded audit
cannot prove dynamic aliasing, arbitrary callbacks or whole-program ownership.
"""
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from tests.hermes_state._local_writer_lock_audit import (
    Facts, lexical_facts, source_trees, writer_ref,
)

Audit = Literal['calls', 'uses']

@dataclass(frozen=True)
class Site:
    module: str
    line: int
    scope: str
    kind: str
    expression: str

class AuditViolation(AssertionError):
    def __init__(self, audit: Audit, sites: list[Site]):
        self.sites = sites
        super().__init__(f'unlocked writer {audit}: ' + '; '.join(
            f'{site.module}:{site.line} {site.scope} [{site.kind}] {site.expression}' for site in sites
        ))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ref(node: ast.AST, facts: Facts, module: str) -> bool:
    return writer_ref(node, module, facts.aliases)


def _unowned(node: ast.AST, facts: Facts) -> bool:
    scope = facts.scopes[id(node)]
    return id(node) not in facts.locked and (scope.deferred or scope.definition_id not in facts.allowed)


def _site(module: str, node: ast.AST, kind: str, facts: Facts) -> Site:
    return Site(module, getattr(node, 'lineno'), facts.scopes[id(node)].name, kind, ast.unparse(node))


def _scan(tree: ast.Module, module: str, audit: Audit) -> list[Site]:
    facts = lexical_facts(tree, module)
    sites = []
    covered = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            direct = isinstance(func, ast.Attribute) and _ref(func.value, facts, module)
            passed = [*node.args, *(kw.value for kw in node.keywords)]
            handed = any(_ref(arg, facts, module) for arg in passed)
            if direct or handed:
                if direct and isinstance(func, ast.Attribute):
                    covered.add(id(func.value))
                covered.update(id(arg) for arg in passed if _ref(arg, facts, module))
                if _unowned(node, facts):
                    sites.append(_site(module, node, 'call' if direct else 'handoff', facts))
        if audit == 'uses' and module == 'hermes_state_telemetry.py' and isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is not None and writer_ref(value, module, set()) and facts.scopes[id(node)].definition_id in facts.owner_ids and _unowned(node, facts):
                sites.append(_site(module, node, 'alias-acquisition', facts))
    # Deferred bare capture/return is use even without a call. A covered immediate
    # call can contain an independent deferred lambda; that inner ref remains visible.
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)
                and _ref(node, facts, module) and facts.scopes[id(node)].deferred
                and id(node) not in covered and _unowned(node, facts)):
            sites.append(_site(module, node, 'deferred-reference', facts))
    return sorted(sites, key=lambda site: (site.line, site.kind, site.expression))


def unlocked_writer_calls(tree: ast.Module, module: str = 'hermes_state.py') -> list[Site]:
    return _scan(tree, module, 'calls')


def unlocked_writer_uses(tree: ast.Module, module: str = 'hermes_state.py') -> list[Site]:
    return _scan(tree, module, 'uses')


def scan_sources(trees: dict[str, ast.Module], audit: Audit) -> list[Site]:
    scanner = unlocked_writer_calls if audit == 'calls' else unlocked_writer_uses
    return [site for module, tree in sorted(trees.items()) for site in scanner(tree, module)]


def assert_clean(trees: dict[str, ast.Module], audit: Audit) -> None:
    sites = scan_sources(trees, audit)
    if sites:
        raise AuditViolation(audit, sites)


@pytest.mark.parametrize('audit', ['calls', 'uses'])
def test_current_sessiondb_writer_source_is_lexically_owned(audit: Audit) -> None:
    assert_clean(source_trees(_repo_root()), audit)
