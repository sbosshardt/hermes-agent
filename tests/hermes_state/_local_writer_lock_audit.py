"""Narrow source-only ownership audit for SessionDB and its optional telemetry writer.

No runtime import or I/O beyond reading the current defining source files. This
is a lexical tripwire, not interprocedural proof or arbitrary Python alias analysis.
"""
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import cast


def shape(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def parsed_expr(source: str) -> ast.expr:
    return ast.parse(source, mode="eval").body


def bindings(body: list[ast.stmt], name: str) -> list[ast.AST]:
    found = []
    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                found.append(node)
            return
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append(node)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.extend(node for alias in node.names if (alias.asname or alias.name.split('.')[0]) == name)
        for child in ast.iter_child_nodes(node):
            visit(child)
    for item in body:
        visit(item)
    return found


def unique_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    matches = bindings(tree.body, name)
    return matches[0] if len(matches) == 1 and isinstance(matches[0], ast.ClassDef) and matches[0] in tree.body else None


def unique_function(body: list[ast.stmt], name: str) -> ast.FunctionDef | None:
    matches = bindings(body, name)
    return matches[0] if len(matches) == 1 and isinstance(matches[0], ast.FunctionDef) and matches[0] in body else None


def unique_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | None:
    return unique_function(cls.body, name)


def operational(fn: ast.FunctionDef) -> list[ast.stmt]:
    body = fn.body
    return body[1:] if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str) else body


# Exact acquiring contextmanager, not a helper name or arbitrary lookalike.
_EXPECTED_HELPER = cast(ast.FunctionDef, ast.parse('''
@contextmanager
def _bounded_telemetry_lock(db):
    if not db._lock.acquire(timeout=_LOCAL_LOCK_TIMEOUT_S):
        raise TimeoutError("optional session telemetry lock busy")
    try:
        yield
    finally:
        db._lock.release()
''').body[0])
_EXPECTED_CONTEXT = parsed_expr('_bounded_telemetry_lock(db)')
_TELEMETRY_NAME = 'hermes_state_telemetry.py'
_TELEMETRY_OWNER = 'update_auto_recall_metrics'


def verified_telemetry_with_id(tree: ast.Module) -> set[int]:
    helper = unique_function(tree.body, '_bounded_telemetry_lock')
    owner = unique_function(tree.body, _TELEMETRY_OWNER)
    if helper is None or owner is None or len(helper.decorator_list) != 1 or not (
        shape(helper.decorator_list[0], _EXPECTED_HELPER.decorator_list[0])
    ):
        return set()
    if not shape(helper.args, _EXPECTED_HELPER.args) or not all(
        shape(a, b) for a, b in zip(operational(helper), _EXPECTED_HELPER.body)
    ) or len(operational(helper)) != len(_EXPECTED_HELPER.body):
        return set()
    # Syntactic rebinding invalidates this narrowly recognized owner.
    if any(isinstance(n, ast.Attribute) and n.attr == '_bounded_telemetry_lock' and isinstance(n.ctx, (ast.Store, ast.Del)) for n in ast.walk(tree)):
        return set()
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == '_bounded_telemetry_lock']
    matches = [n for n in owner.body if isinstance(n, ast.With) and len(n.items) == 1 and n.items[0].optional_vars is None and shape(n.items[0].context_expr, _EXPECTED_CONTEXT)]
    return {id(matches[0])} if len(calls) == len(matches) == 1 and calls[0] is matches[0].items[0].context_expr else set()


# Direct pre-publication constructor chain and explicitly lock-owned internal boundaries.
# No name-wide, module-wide or deferred-scope exemption.
CONSTRUCTION = frozenset({
    ('hermes_state.py', 'SessionDB', '__init__'),
    ('hermes_state.py', 'SessionDB', '_open_writer'),
    ('hermes_state.py', 'SessionDB', '_connect_and_init'),
    ('hermes_state.py', 'SessionDB', '_connect_and_init_with_lock_patience'),
    ('hermes_state_schema.py', 'SessionSchemaMixin', '_init_schema'),
})
LOCK_OWNED_BOUNDARIES = frozenset({
    ('hermes_state.py', 'SessionDB', '_settle_lost_generation_locked'),
    ('hermes_state.py', 'SessionDB', '_disable_close_time_checkpoint'),
    ('hermes_state_maintenance.py', 'SessionMaintenanceMixin', '_try_checkpoint'),
    ('hermes_state_search.py', 'SessionSearchMixin', '_fts_table_exists'),
    ('hermes_state_schema.py', 'SessionSchemaMixin', '_recover_stale_fts_locked'),
})


def writer_ref(node: ast.AST, module: str, aliases: set[int]) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == '_conn'
            and isinstance(node.value, ast.Name)
            and node.value.id == ('db' if module == _TELEMETRY_NAME else 'self')) or id(node) in aliases


@dataclass(frozen=True)
class Scope:
    name: str
    definition_id: int | None
    deferred: bool


@dataclass(frozen=True)
class Facts:
    scopes: dict[int, Scope]
    locked: set[int]
    allowed: set[int]
    aliases: set[int]
    owner_ids: set[int]


def lexical_facts(tree: ast.Module, module: str) -> Facts:
    allowed = set()
    for filename, classname, methodname in CONSTRUCTION | LOCK_OWNED_BOUNDARIES:
        if filename == module:
            cls = unique_class(tree, classname)
            method = unique_method(cls, methodname) if cls else None
            if method is not None:
                allowed.add(id(method))
    verified = verified_telemetry_with_id(tree) if module == _TELEMETRY_NAME else set()
    owner = unique_function(tree.body, _TELEMETRY_OWNER) if module == _TELEMETRY_NAME else None
    owner_ids = {id(owner)} if owner is not None else set()
    aliases: set[int] = set()
    if owner is not None:
        # The only intentionally recognized alias is the owner's `conn = db._conn`.
        names = {target.id for node in ast.walk(owner)
                 if isinstance(node, ast.Assign) and writer_ref(node.value, module, set())
                 for target in node.targets if isinstance(target, ast.Name)}
        aliases = {id(node) for node in ast.walk(owner) if isinstance(node, ast.Name)
                   and isinstance(node.ctx, ast.Load) and node.id in names}
    scopes: dict[int, Scope] = {}
    locked: set[int] = set()

    def visit(node: ast.AST, path: tuple[str, ...] = (), fn: int | None = None,
              functions: int = 0, classes: int = 0, deferred: bool = False,
              held: bool = False) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            path += ('<lambda>' if isinstance(node, ast.Lambda) else node.name,)
            deferred = deferred or functions > 0 or isinstance(node, ast.Lambda)
            functions += 1
            fn = id(node)
            held = False  # a new callable runs later, never inherits a lexical mutex
        elif isinstance(node, ast.ClassDef):
            path += (node.name,)
            deferred = deferred or functions > 0 or classes > 0
            classes += 1
            fn = None
            held = False
        scopes[id(node)] = Scope('.'.join(path) or '<module>', fn, deferred)
        if held:
            locked.add(id(node))
        if isinstance(node, (ast.With, ast.AsyncWith)):
            acquired = held
            for item in node.items:
                # Context expressions execute BEFORE their own __enter__.
                visit(item.context_expr, path, fn, functions, classes, deferred, acquired)
                if item.optional_vars is not None:
                    visit(item.optional_vars, path, fn, functions, classes, deferred, acquired)
                if (shape(item.context_expr, parsed_expr('self._lock'))
                        or id(node) in verified and shape(item.context_expr, _EXPECTED_CONTEXT)):
                    acquired = True
            for statement in node.body:
                visit(statement, path, fn, functions, classes, deferred, acquired)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, path, fn, functions, classes, deferred, held)

    visit(tree)
    return Facts(scopes, locked, allowed, aliases, owner_ids)


def source_trees(root: Path) -> dict[str, ast.Module]:
    facade = ast.parse((root / 'hermes_state.py').read_text(encoding='utf-8'))
    cls = unique_class(facade, 'SessionDB')
    assert cls is not None, 'ambiguous SessionDB identity'
    imports = {alias.asname or alias.name: (node.module, alias.name)
               for node in facade.body if isinstance(node, ast.ImportFrom) for alias in node.names}
    trees = {'hermes_state.py': facade}
    for base in cls.bases:
        assert isinstance(base, ast.Name) and base.id in imports, 'unresolved SessionDB base'
        module, classname = imports[base.id]
        assert module and module.startswith('hermes_state_') and '.' not in module
        filename = module + '.py'
        if filename not in trees:
            trees[filename] = ast.parse((root / filename).read_text(encoding='utf-8'))
        assert unique_class(trees[filename], classname) is not None, f'ambiguous base {base.id}'
    trees[_TELEMETRY_NAME] = ast.parse((root / _TELEMETRY_NAME).read_text(encoding='utf-8'))
    return trees
