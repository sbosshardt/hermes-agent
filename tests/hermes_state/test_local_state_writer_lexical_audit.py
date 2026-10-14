"""Independent writer CALL and USE/handoff sweeps, ported without runtime imports.

This source audit is explicitly requested for the isolated family6 portfolio.
It supplements, and does not certify or replace, native owner-thread tests.
"""

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from tests.hermes_state._local_writer_lock_audit import (
    Facts, is_writer_attribute, lexical_facts, source_trees,
)

Audit = Literal["calls", "uses"]


@dataclass(frozen=True)
class Site:
    module: str
    line: int
    scope: str
    kind: str
    expression: str


class AuditViolation(AssertionError):
    def __init__(self, audit: Audit, sites: list[Site]) -> None:
        self.sites = sites
        super().__init__(f"unlocked writer {audit}: " + "; ".join(
            f"{site.module}:{site.line} {site.scope} [{site.kind}] {site.expression}"
            for site in sites
        ))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _writer_ref(node: ast.AST, facts: Facts) -> bool:
    return is_writer_attribute(node) or id(node) in facts.aliases


def _unowned(node: ast.AST, facts: Facts) -> bool:
    scope = facts.scopes[id(node)]
    return id(node) not in facts.locked and scope.definition_id not in facts.allowed


def _site(module: str, node: ast.AST, kind: str, facts: Facts) -> Site:
    return Site(module, getattr(node, "lineno"), facts.scopes[id(node)].name, kind, ast.unparse(node))


def _deferred_references(tree: ast.Module, facts: Facts, covered: set[int]) -> list[ast.AST]:
    # Also catch a deferred bare return/capture, not only an execute() call.
    return [node for node in ast.walk(tree)
            if (isinstance(node, (ast.Attribute, ast.Name)) and isinstance(node.ctx, ast.Load)
                and _writer_ref(node, facts) and facts.scopes[id(node)].deferred
                and id(node) not in covered and _unowned(node, facts))]


def unlocked_writer_calls(tree: ast.Module, module: str = "hermes_state.py") -> list[Site]:
    """Enumerate direct writer/obvious bounded-alias calls and call handoffs.

    Handoffs are checked here as well so a broken bounded helper cannot disappear
    behind fn(conn) or the connection-param busy-handler boundary.
    """
    facts = lexical_facts(tree, module)
    offending: list[Site] = []
    covered: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        direct = isinstance(node.func, ast.Attribute) and _writer_ref(node.func.value, facts)
        handoff = any(_writer_ref(arg, facts) for arg in [*node.args, *(kw.value for kw in node.keywords)])
        if direct or handoff:
            if direct and isinstance(node.func, ast.Attribute):
                covered.add(id(node.func.value))
            covered.update(id(arg) for arg in [*node.args, *(kw.value for kw in node.keywords)]
                           if _writer_ref(arg, facts))
            if _unowned(node, facts):
                offending.append(_site(module, node, "call" if direct else "handoff", facts))
    offending.extend(_site(module, node, "deferred-reference", facts)
                     for node in _deferred_references(tree, facts, covered))
    return sorted(offending, key=lambda site: (site.line, site.kind, site.expression))


def unlocked_writer_uses(tree: ast.Module, module: str = "hermes_state.py") -> list[Site]:
    """Sibling USE sweep: calls, handoffs, bounded alias acquisition, deferred refs.

    Mere None/identity guards and attribute stores are not SQLite use. General
    aliases in other methods, container aliases and parameter-based helpers are
    outside this deliberately narrow lexical check.
    """
    facts = lexical_facts(tree, module)
    offending: list[Site] = []
    covered: set[int] = set()

    class Sweep(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            direct = isinstance(node.func, ast.Attribute) and _writer_ref(node.func.value, facts)
            handed = any(_writer_ref(arg, facts) for arg in node.args) or any(
                _writer_ref(keyword.value, facts) for keyword in node.keywords
            )
            if direct or handed:
                if direct and isinstance(node.func, ast.Attribute):
                    covered.add(id(node.func.value))
                covered.update(id(arg) for arg in [*node.args, *(kw.value for kw in node.keywords)]
                               if _writer_ref(arg, facts))
                if _unowned(node, facts):
                    offending.append(_site(module, node, "call" if direct else "handoff", facts))
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            if (facts.scopes[id(node)].definition_id in facts.bounded_methods
                    and is_writer_attribute(node.value) and _unowned(node, facts)):
                offending.append(_site(module, node, "alias-acquisition", facts))
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if (facts.scopes[id(node)].definition_id in facts.bounded_methods
                    and node.value is not None and is_writer_attribute(node.value) and _unowned(node, facts)):
                offending.append(_site(module, node, "alias-acquisition", facts))
            self.generic_visit(node)

    Sweep().visit(tree)
    offending.extend(_site(module, node, "deferred-reference", facts)
                     for node in _deferred_references(tree, facts, covered))
    return sorted(offending, key=lambda site: (site.line, site.kind, site.expression))


def scan_sources(trees: dict[str, ast.Module], audit: Audit) -> list[Site]:
    scanner = unlocked_writer_calls if audit == "calls" else unlocked_writer_uses
    return [site for module, tree in sorted(trees.items()) for site in scanner(tree, module)]


def assert_clean(trees: dict[str, ast.Module], audit: Audit) -> None:
    sites = scan_sources(trees, audit)
    if sites:
        raise AuditViolation(audit, sites)


@pytest.mark.parametrize("audit", ["calls", "uses"])
def test_current_sessiondb_writer_source_is_lexically_owned(audit: Audit) -> None:
    assert_clean(source_trees(_repo_root()), audit)
