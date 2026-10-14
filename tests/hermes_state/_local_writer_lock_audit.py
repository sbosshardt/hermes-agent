"""Source-only lexical tripwires for the current, split SessionDB implementation.

The bounded exception belongs to one direct SessionDB method, not to the name
``_write_lock``. Ordinary mutex bodies exclude deferred execution scopes. Only
obvious writer aliases in _execute_best_effort_write are tracked; this is not
Python dataflow, rebinding, monkeypatch, or interprocedural ownership proof.
"""

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, cast


_EXPECTED_HELPER = cast(ast.FunctionDef, ast.parse('''
@contextmanager
def _write_lock(self, timeout_s: float) -> Iterator[None]:
    if not self._lock.acquire(timeout=max(0.0, timeout_s)):
        raise TimeoutError("state.db local write lock unavailable for best-effort telemetry")
    try:
        yield
    finally:
        self._lock.release()
''').body[0])
_EXPECTED_WRITER = cast(ast.FunctionDef, ast.parse('''
def _execute_best_effort_write(
    self, fn: Callable[[sqlite3.Connection], T], patience_s: Optional[float],
    lock_timeout_s: Optional[float],
) -> T:
    pass
''').body[0])
EXPECTED_CONTEXT = cast(ast.With, ast.parse('''
with self._write_lock(min(admission, max(0.0, deadline - time.monotonic()))):
    pass
''').body[0]).items[0].context_expr
_DEFERRED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

# Exact defining identities, never prefixes or nearest-function-name exemptions.
# Evidence: hermes_state.__init__ -> _open_writer -> _connect_and_init_with_lock_patience
# -> _connect_and_init -> SessionSchemaMixin._init_schema, before publication.
CONSTRUCTION = frozenset({
    ("hermes_state.py", "SessionDB", "__init__"),
    ("hermes_state.py", "SessionDB", "_open_writer"),
    ("hermes_state.py", "SessionDB", "_connect_and_init"),
    ("hermes_state.py", "SessionDB", "_connect_and_init_with_lock_patience"),
    ("hermes_state_schema.py", "SessionSchemaMixin", "_init_schema"),
})
# Known native lock-owned boundaries, not a claim that arbitrary callers are safe.
# close's mutex -> settlement; vacuum's mutex -> checkpoint;
# optimize/rebuild/merge mutex -> _present_fts_tables -> _fts_table_exists;
# schema startup or retry_deferred_fts_recovery's mutex -> _recover_stale_fts
# -> _recover_stale_fts_locked. Parameter-based transaction helpers are NOT exempted
# by name; they simply contain no direct self._conn reference in this audit.
LOCK_OWNED_BOUNDARIES = frozenset({
    ("hermes_state.py", "SessionDB", "_settle_lost_generation_locked"),
    ("hermes_state_maintenance.py", "SessionMaintenanceMixin", "_try_checkpoint"),
    ("hermes_state_search.py", "SessionSearchMixin", "_fts_table_exists"),
    ("hermes_state_schema.py", "SessionSchemaMixin", "_recover_stale_fts_locked"),
})


def same_shape(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def immediate_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Execution-local descendants: no inherited lock through a deferred scope."""
    if isinstance(node, _DEFERRED):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from immediate_nodes(child)


def _bindings(body: list[ast.stmt], name: str) -> list[ast.AST]:
    """Syntactic bindings at this execution scope, including conditional ones."""
    found: list[ast.AST] = []

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
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    found.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in body:
        visit(statement)
    return found


def unique_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    bindings = _bindings(tree.body, name)
    if len(bindings) == 1 and isinstance(bindings[0], ast.ClassDef) and bindings[0] in tree.body:
        return bindings[0]
    return None


def unique_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | None:
    bindings = _bindings(cls.body, name)
    if len(bindings) == 1 and isinstance(bindings[0], ast.FunctionDef) and bindings[0] in cls.body:
        return bindings[0]
    return None


def _operational_body(fn: ast.FunctionDef) -> list[ast.stmt]:
    body = fn.body
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        return body[1:]
    return body


def verified_writer_with_ids(tree: ast.Module) -> set[int]:
    """Fail closed on ambiguous identities or a changed acquire/yield/release contract."""
    cls = unique_class(tree, "SessionDB")
    if cls is None or cls.decorator_list:
        return set()
    helper = unique_method(cls, "_write_lock")
    writer = unique_method(cls, "_execute_best_effort_write")
    if helper is None or writer is None:
        return set()
    # Detect explicit syntactic replacement of the helper/method, including
    # instance replacement in __init__. Arbitrary setattr/monkeypatch is out of scope.
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del))
                and node.attr in {"_write_lock", "_execute_best_effort_write"}
                and isinstance(node.value, ast.Name) and node.value.id in {"self", "SessionDB"}):
            return set()
    if (not same_shape(helper.args, _EXPECTED_HELPER.args)
            or helper.returns is None or not same_shape(helper.returns, cast(ast.AST, _EXPECTED_HELPER.returns))
            or len(helper.decorator_list) != 1
            or not same_shape(helper.decorator_list[0], _EXPECTED_HELPER.decorator_list[0])):
        return set()
    body = _operational_body(helper)
    if len(body) != len(_EXPECTED_HELPER.body) or not all(
        same_shape(a, b) for a, b in zip(body, _EXPECTED_HELPER.body)
    ):
        return set()
    if (writer.decorator_list or not same_shape(writer.args, _EXPECTED_WRITER.args)
            or writer.returns is None or not same_shape(writer.returns, cast(ast.AST, _EXPECTED_WRITER.returns))):
        return set()
    bounded = [node for node in writer.body if isinstance(node, ast.With)
               and len(node.items) == 1 and node.items[0].optional_vars is None
               and same_shape(node.items[0].context_expr, EXPECTED_CONTEXT)]
    return {id(bounded[0])} if len(bounded) == 1 else set()


def is_writer_attribute(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "_conn"
            and isinstance(node.value, ast.Name) and node.value.id == "self")


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
    bounded_methods: set[int]


def lexical_facts(tree: ast.Module, module: str) -> Facts:
    scopes: dict[int, Scope] = {}

    def visit(node: ast.AST, path: tuple[str, ...], fn: int | None,
              functions: int, classes: int, deferred: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            path += ("<lambda>" if isinstance(node, ast.Lambda) else node.name,)
            deferred = deferred or functions > 0 or isinstance(node, ast.Lambda)
            functions += 1
            fn = id(node)
        elif isinstance(node, ast.ClassDef):
            path += (node.name,)
            deferred = deferred or functions > 0 or classes > 0
            classes += 1
            fn = None
        scopes[id(node)] = Scope(".".join(path) or "<module>", fn, deferred)
        for child in ast.iter_child_nodes(node):
            visit(child, path, fn, functions, classes, deferred)

    visit(tree, (), None, 0, 0, False)
    verified = verified_writer_with_ids(tree) if module == "hermes_state.py" else set()
    locked: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        ordinary = any(isinstance(item.context_expr, ast.Attribute)
                       and item.context_expr.attr == "_lock"
                       and isinstance(item.context_expr.value, ast.Name)
                       and item.context_expr.value.id == "self" for item in node.items)
        if ordinary or id(node) in verified:
            locked.update(id(child) for statement in node.body for child in immediate_nodes(statement))
    allowed: set[int] = set()
    for filename, classname, methodname in CONSTRUCTION | LOCK_OWNED_BOUNDARIES:
        if module != filename:
            continue
        cls = unique_class(tree, classname)
        method = unique_method(cls, methodname) if cls is not None else None
        if method is not None:
            allowed.add(id(method))
    # Independent of verifier success: otherwise breaking the helper could hide
    # its alias calls from the CALL sweep. Conservative about nested closures.
    aliases: set[int] = set()
    bounded_methods: set[int] = set()
    if module == "hermes_state.py":
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef) or cls.name != "SessionDB":
                continue
            for method in cls.body:
                if not isinstance(method, ast.FunctionDef) or method.name != "_execute_best_effort_write":
                    continue
                bounded_methods.add(id(method))
                names: set[str] = set()
                for statement in method.body:
                    for node in immediate_nodes(statement):
                        if isinstance(node, ast.Assign) and is_writer_attribute(node.value):
                            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
                        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                            if is_writer_attribute(node.value):
                                names.add(node.target.id)
                aliases.update(id(node) for node in ast.walk(method)
                               if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names)
    return Facts(scopes, locked, allowed, aliases, bounded_methods)


def source_trees(root: Path) -> dict[str, ast.Module]:
    """Read the facade and the actual defining modules of its direct mixin bases.

    No runtime import, connection creation, private state, or recursive package scan.
    The source layout must resolve every base, rather than silently omitting a split.
    """
    facade = ast.parse((root / "hermes_state.py").read_text(encoding="utf-8"))
    cls = unique_class(facade, "SessionDB")
    assert cls is not None, "SessionDB defining identity is ambiguous"
    imports = {alias.asname or alias.name: (node.module, alias.name)
               for node in facade.body if isinstance(node, ast.ImportFrom)
               for alias in node.names}
    trees = {"hermes_state.py": facade}
    for base in cls.bases:
        assert isinstance(base, ast.Name) and base.id in imports, "unresolved SessionDB mixin base"
        module, classname = imports[base.id]
        assert module is not None and module.startswith("hermes_state_") and "." not in module
        filename = module + ".py"
        if filename not in trees:
            trees[filename] = ast.parse((root / filename).read_text(encoding="utf-8"))
        assert unique_class(trees[filename], classname) is not None, f"ambiguous base {base.id}"
    return trees
