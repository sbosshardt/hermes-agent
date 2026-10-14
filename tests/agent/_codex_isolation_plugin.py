"""Opt-in adapter for this staged selection; never a production pytest default.

HOME changes only during configuration/collection, AFTER native root capture.
It is restored before native per-test fixtures, keeping the dynamic state-DB
root resolver pointed at the operator. Native guards and their lists are intact.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

CORE = Path('/home/hermes/.hermes/cache/scratch/hermes-v0216-candidate-99962632')
OPERATOR_HOME = Path('/home/hermes')
OPERATOR_ROOT = OPERATOR_HOME / '.hermes'
PREFIX = '03-codex-isolation-exception-'
DENIED_MARKS = {'allow_real_home_io', 'live_system_guard_bypass',
                'spawns_gateway_lookalike', 'real_audio_playback'}


def owned_path(value, sandbox):
    """Validate lexical AND resolved custody before a caller performs I/O."""
    candidate = Path(value)
    root = Path(sandbox)
    if not candidate.is_absolute() or not candidate.is_relative_to(root):
        raise AssertionError('isolation scratch escape: ' + str(value))
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise AssertionError('isolation scratch symlink escape: ' + str(value))
    return resolved


class InertFallbackReached(BaseException):
    """An inert bottom was reached, never an acceptable native refusal."""


def recording_bottom(calls, name):
    def record(*args, **kwargs):
        calls.append(name)
        raise InertFallbackReached(name)
    return record


@contextmanager
def native_fixture_scope(fixture, **arguments):
    """Install the actual fixture AFTER bottom stubs; undo its guards first."""
    import inspect
    function = getattr(fixture, '__wrapped__', None)
    if function is None:
        function = getattr(fixture, '_fixture_function', None)
    if not callable(function):
        raise AssertionError('actual native fixture implementation unavailable')
    with pytest.MonkeyPatch.context() as patch:
        result = function(monkeypatch=patch, **arguments)
        if inspect.isgenerator(result):
            next(result)
            try:
                yield
            finally:
                try:
                    next(result)
                except StopIteration:
                    pass
                else:
                    raise AssertionError('native fixture yielded more than once')
                result.close()
        else:
            yield


@contextmanager
def inert_metadata_scope(paths):
    """Literal synthetic paths only: no operator stat/readlink during resolution.

    These are scoped bottom metadata primitives, NOT guard/root replacements.
    All unrelated resolution retains the existing native guards and originals.
    """
    targets = frozenset(Path(value) for value in paths)
    previous_resolve = Path.resolve
    previous_realpath = os.path.realpath

    def resolve(path, *args, **kwargs):
        if path in targets:
            return path
        return previous_resolve(path, *args, **kwargs)

    def realpath(value, *args, **kwargs):
        if Path(os.fsdecode(value)) in targets:
            return os.fspath(value)
        return previous_realpath(value, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, 'resolve', resolve)
        patch.setattr(os.path, 'realpath', realpath)
        yield
    if Path.resolve is not previous_resolve or os.path.realpath is not previous_realpath:
        raise AssertionError('inert metadata bindings not restored')


def refusal_receipt(s, nodeid, control, site, calls):
    if calls:
        raise AssertionError('native refusal reached an inert downstream primitive')
    s.setdefault('native_refusals', []).append({
        'nodeid': nodeid, 'control': control, 'actual_native_site': site,
        'downstream_calls': list(calls), 'scopes_restored': True,
    })
    persist(s)


@contextmanager
def recovery_scope(fixture_root):
    """Rebind one bootstrap boundary; run the real helper on an empty owned root."""
    from hermes_cli import _early_recovery as recovery

    fixture_root = Path(fixture_root)
    if not fixture_root.is_relative_to(CORE) or not fixture_root.resolve().is_relative_to(CORE.resolve()):
        raise AssertionError('recovery fixture outside staged scratch checkout')
    fixture_root.mkdir(mode=0o700)
    (fixture_root / '.git').mkdir(mode=0o700)
    # No HEAD/index/ref/marker or copied source: exercise the real no-marker path.
    previous = recovery.restore_interrupted_pull
    native = getattr(previous, '_codex_native_recovery', previous)
    events = []

    def isolated(project_root=None, *, after_failure=False):
        supplied = CORE if project_root is None else Path(project_root)
        # Do not resolve/probe an unrelated root before refusing it.
        if supplied != CORE:
            raise AssertionError('isolation recovery unrelated root: ' + str(supplied))
        if list((fixture_root / '.git').iterdir()) or (fixture_root / recovery.ZIP_SWAP_JOURNAL).exists():
            raise AssertionError('empty recovery fixture acquired unexpected state')
        result = native(fixture_root, after_failure=after_failure)
        if result is not False:
            raise AssertionError('empty recovery fixture unexpectedly restored a tree')
        events.append({'supplied_root': str(supplied), 'actual_root': str(fixture_root),
                       'marker': str(fixture_root / '.git' / recovery.INTERRUPTED_PULL_MARKER),
                       'result': result})
        return result

    isolated._codex_native_recovery = native
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(recovery, 'restore_interrupted_pull', isolated)
        yield {'native': native, 'previous': previous, 'binding': isolated,
               'root': fixture_root, 'events': events}
    if recovery.restore_interrupted_pull is not previous:
        raise AssertionError('recovery binding not restored')


@contextmanager
def payload_scope(envelope):
    """Only canonical payload-envelope discovery, through the unchanged helper."""
    from pm import environments as pm_env
    envelope = Path(envelope)
    owned_path(envelope, CORE)
    envelope.mkdir(mode=0o700)
    actual_root = envelope / 'repo'
    actual_root.mkdir(mode=0o700)
    previous = pm_env._payload_manifest
    native = getattr(previous, '_codex_native_payload', previous)
    events = []

    def isolated(root):
        if Path(root) != CORE:
            raise AssertionError('isolation payload unrelated root: ' + str(root))
        if list(actual_root.iterdir()) or (envelope / 'manifest.json').exists():
            raise AssertionError('empty payload envelope acquired unexpected state')
        result = native(actual_root)
        if result is not None:
            raise AssertionError('empty payload envelope unexpectedly selected a payload')
        events.append({'supplied_root': str(root), 'actual_root': str(actual_root),
                       'manifest': str(envelope / 'manifest.json'), 'result': result})
        return result

    isolated._codex_native_payload = native
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pm_env, '_payload_manifest', isolated)
        yield {'native': native, 'previous': previous, 'binding': isolated,
               'root': actual_root, 'events': events}
    if pm_env._payload_manifest is not previous:
        raise AssertionError('payload binding not restored')


# Only fields mutated by this adapter or its native conftest/bootstrap custody.
ENVIRONMENT_FIELDS = ('HOME', 'TMPDIR', 'TMP', 'TEMP', 'HERMES_HOME',
                      'HERMES_TEST_SANDBOX_HOME', 'HERMES_TEST_ISOLATION',
                      'HERMES_DISABLE_LAZY_INSTALLS', 'HERMES_GATEWAY_LOCK_DIR',
                      'HERMES_SCRATCH_DIR', 'PYTHONPYCACHEPREFIX')


def note_failure(s, exc):
    s.setdefault('primary_failure', {'type': type(exc).__name__, 'message': str(exc)})


class CleanupOwner:
    """Registered before mutation; one idempotent owner for all exit paths."""
    def __init__(self, s):
        self.s = s
        self.environment = {name: os.environ.get(name) for name in ENVIRONMENT_FIELDS}
        self.selector = tempfile._get_default_tempdir
        self.tempdir = tempfile.tempdir
        self.pycache_prefix = sys.pycache_prefix
        self.finished = False
        self.native_bindings = []

    def __call__(self):
        global _ACTIVE_CONFIG
        if self.finished:
            return
        self.finished = True
        s = self.s
        errors = []
        checks = {}

        def attempt(label, action):
            try:
                action()
            except BaseException as exc:
                errors.append({'stage': label, 'type': type(exc).__name__, 'message': str(exc)})

        # Context exit, patch undo and receipt failure must not skip other resources.
        for key in ('payload_context', 'recovery_context'):
            context = s.get(key)
            if context is not None:
                attempt(key, lambda context=context: context.__exit__(None, None, None))
        for module, name, original in self.native_bindings:
            attempt('restore_helper:' + name, lambda module=module, name=name, original=original:
                    setattr(module, name, original))
            checks['helper:' + name] = getattr(module, name) is original
        guard = s.get('collection_guard')
        guard_bindings = list(guard._setattr) if guard is not None else []
        if guard is not None:
            attempt('collection_guard', guard.undo)
        for index, (obj, name, original) in enumerate(guard_bindings):
            # Each real guard binding is restored independently even after an undo error.
            attempt('guard:' + str(index), lambda obj=obj, name=name, original=original:
                    setattr(obj, name, original))
            checks['guard:' + str(index) + ':' + name] = getattr(obj, name) is original
        env_patch = s.get('env_patch')
        if env_patch is not None:
            attempt('env_patch', env_patch.undo)
        for name, original in self.environment.items():
            def restore(name=name, original=original):
                if original is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = original
            attempt('environment:' + name, restore)
            checks['environment:' + name] = os.environ.get(name) == original
        attempt('temp_selector', lambda: setattr(tempfile, '_get_default_tempdir', self.selector))
        attempt('tempdir', lambda: setattr(tempfile, 'tempdir', self.tempdir))
        attempt('pycache_prefix', lambda: setattr(sys, 'pycache_prefix', self.pycache_prefix))
        checks['stdlib_selector'] = tempfile._get_default_tempdir is self.selector
        checks['tempdir'] = tempfile.tempdir == self.tempdir
        checks['pycache_prefix'] = sys.pycache_prefix == self.pycache_prefix
        if s.get('native') is not None:
            attempt('native_custody', lambda: assert_native(s))
        _ACTIVE_CONFIG = None
        checks['active_config'] = _ACTIVE_CONFIG is None
        s['restoration_checks'] = checks
        s['cleanup_errors'] = errors
        s['cleanup_finished'] = True
        s['recovery_binding_restored'] = all(value for key, value in checks.items() if key == 'helper:restore_interrupted_pull')
        s['payload_binding_restored'] = all(value for key, value in checks.items() if key == 'helper:_payload_manifest')
        s['process_environment_restored'] = all(value for key, value in checks.items() if key.startswith('environment:'))
        s['stdlib_selector_restored'] = checks['stdlib_selector']
        s['complete'] = bool(s.get('sessionfinish_reached') and s.get('pytest_exit') == 0
                             and all(checks.values()) and not errors and not s.get('primary_failure'))
        attempt('final_proof', lambda: persist(s))
        if errors:
            s['complete'] = False
            # Keep the primary startup/test failure; retain every cleanup error as secondary.
            if 'primary_failure' not in s:
                raise RuntimeError('isolation cleanup failed: ' + repr(errors))


def pytest_addoption(parser):
    group = parser.getgroup('staged-codex-isolation')
    group.addoption('--codex-isolation-sandbox', required=False)


def state(config):
    result = getattr(config, '_codex_isolation_state', None)
    if result is None:
        raise AssertionError('opt-in staged isolation plugin was not initialized')
    return result


def persist(s):
    # Exclusively created proof; never serialize owner objects or environment values.
    pending = s['evidence_path'].with_suffix('.pending')
    payload = {key: value for key, value in s.items() if key not in {
        'native', 'capture_values', 'env_patch', 'collection_guard', 'recovery_context',
        'recovery_adapter', 'payload_context', 'payload_adapter', 'cleanup_owner',
        'evidence_path', 'sandbox_path', 'source_bindings'}}
    payload['recovery_events'] = list(s.get('recovery_adapter', {}).get('events', []))
    payload['payload_events'] = list(s.get('payload_adapter', {}).get('events', []))
    payload['evidence_path'] = str(s['evidence_path'])
    pending.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    pending.replace(s['evidence_path'])


def assert_native(s):
    native = s['native']
    for name, expected in s['capture_values'].items():
        current = getattr(native, name)
        actual = tuple(current) if isinstance(current, list) else current
        if actual != expected:
            raise AssertionError('native captured root/protection changed: ' + name)
    for module, name, original in s['source_bindings']:
        if getattr(module, name) is not original:
            raise AssertionError('native guard binding changed: ' + name)
    if OPERATOR_ROOT not in native._REAL_HERMES_ROOT_CANDIDATES:
        raise AssertionError('native guard lost actual operator root')


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    if Path.cwd() != CORE or Path(__file__).resolve() != CORE / 'tests/agent/_codex_isolation_plugin.py':
        raise pytest.UsageError('isolation plugin is restricted to the canonical staged checkout')
    if os.environ.get('HOME') != str(OPERATOR_HOME) or os.environ.get('HERMES_HOME'):
        raise pytest.UsageError('native captures require actual operator HOME and unset HERMES_HOME')
    raw = getattr(early_config.known_args_namespace, 'codex_isolation_sandbox', None)
    if not raw:
        raise pytest.UsageError('explicit owned isolation sandbox is required')
    sandbox = Path(raw)
    parent = CORE / '.pytest_cache'
    if sandbox.parent != parent or not sandbox.name.startswith(PREFIX) or sandbox.resolve() != sandbox:
        raise pytest.UsageError('sandbox must be a unique direct child of staged .pytest_cache')
    if not sandbox.is_dir():
        raise pytest.UsageError('launcher must create the sandbox first')
    evidence = sandbox / (PREFIX + 'actual-temp.json')
    with evidence.open('x', encoding='utf-8') as handle:
        handle.write('{}\n')
    s = {'sandbox': str(sandbox), 'sandbox_path': sandbox, 'evidence_path': evidence,
         'pid': os.getpid(), 'pgid': os.getpgrp(), 'interpreter': sys.executable,
         'nice': os.getpriority(os.PRIO_PROCESS, 0), 'tests': [], 'reports': [],
         'collected': [], 'complete': False, 'early_option_from_known_namespace': True}
    owner = CleanupOwner(s)
    s['cleanup_owner'] = owner
    early_config._codex_isolation_state = s
    early_config.add_cleanup(owner)  # Before any selector/env/helper/guard mutation.
    try:
        temp = sandbox / 'tempfile'
        temp.mkdir(mode=0o700)
        # Native root capture executes with actual HOME; only stdlib temp selection moves.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(tempfile, '_get_default_tempdir', lambda: str(temp))
            tempfile.tempdir = None
            result = yield
        if tempfile._get_default_tempdir is not owner.selector:
            raise AssertionError('stdlib temp selector binding not restored')
        candidates = [plugin for plugin in early_config.pluginmanager.get_plugins()
                      if getattr(plugin, '__file__', None)
                      and Path(plugin.__file__).resolve() == CORE / 'tests/conftest.py']
        if len(candidates) != 1:
            raise pytest.UsageError('exactly one actual registered native tests/conftest.py required')
        native = candidates[0]
        import tests.home_io_guard as home_guard
        import tests._fixtures.live_system_guard as lifecycle
        import hermes_state_guard as db_guard
        captures = {name: (tuple(getattr(native, name)) if isinstance(getattr(native, name), list)
                           else getattr(native, name)) for name in (
                            '_REAL_HERMES_ROOT_CANDIDATES', '_REAL_KANBAN_ROOT', '_NATIVE_HERMES_PARENT',
                            '_REAL_INSTALLED_GUI_APPS', '_PRE_SANDBOX_HERMES_HOME', '_PRE_SANDBOX_KANBAN_OVERRIDE')}
        if (native._REAL_KANBAN_ROOT != OPERATOR_ROOT or native._NATIVE_HERMES_PARENT != OPERATOR_HOME
                or native._PRE_SANDBOX_HERMES_HOME or native._PRE_SANDBOX_KANBAN_OVERRIDE
                or OPERATOR_ROOT not in native._REAL_HERMES_ROOT_CANDIDATES):
            raise pytest.UsageError('native operator capture differs from approved default-profile scope')
        s.update({'native': native, 'capture_values': captures,
                  'operator_home': str(OPERATOR_HOME),
                  'operator_roots': [str(p) for p in native._REAL_HERMES_ROOT_CANDIDATES],
                  'kanban_root': str(native._REAL_KANBAN_ROOT),
                  'initial_native_home': native.HERMES_HOME_AT_CONFTEST_IMPORT,
                  'initial_tempfile': tempfile.gettempdir(), 'stdlib_selector_restored': True,
                  'source_bindings': [(home_guard.HomeIOGuard, 'check', home_guard.HomeIOGuard.check),
                                      (home_guard.HomeIOGuard, 'install', home_guard.HomeIOGuard.install),
                                      (db_guard, '_real_platform_state_root', db_guard._real_platform_state_root),
                                      (lifecycle, '_LIVE_GUARD_PROTECTED_GIT_ROOTS', lifecycle._LIVE_GUARD_PROTECTED_GIT_ROOTS),
                                      (home_guard, '_INTERPRETER_PREFIXES', home_guard._INTERPRETER_PREFIXES),
                                      (home_guard, '_INTERPRETER_PREFIX_STRS', home_guard._INTERPRETER_PREFIX_STRS)]})
        from hermes_cli import _early_recovery as recovery
        from pm import environments as pm_env
        owner.native_bindings.extend([(recovery, 'restore_interrupted_pull', recovery.restore_interrupted_pull),
                                      (pm_env, '_payload_manifest', pm_env._payload_manifest)])
        s['recovery_context'] = recovery_scope(sandbox / 'collection-recovery')
        s['recovery_adapter'] = s['recovery_context'].__enter__()
        s['payload_context'] = payload_scope(sandbox / 'payload-envelope')
        s['payload_adapter'] = s['payload_context'].__enter__()
        s['env_patch'] = pytest.MonkeyPatch()
        s['collection_guard'] = pytest.MonkeyPatch()
        assert_native(s)
        owned_path(native.HERMES_HOME_AT_CONFTEST_IMPORT, sandbox)
        owned_path(tempfile.gettempdir(), sandbox)
        fake_home = sandbox / 'process-home'
        fake_home.mkdir(mode=0o700)
        s['env_patch'].setenv('HOME', str(fake_home))
        for name in ('TMPDIR', 'TMP', 'TEMP'):
            s['env_patch'].setenv(name, str(temp))
        tempfile.tempdir = str(temp)
        home_guard.HomeIOGuard(lambda: native._REAL_HERMES_ROOT_CANDIDATES,
                               lambda: native._REAL_INSTALLED_GUI_APPS).install(s['collection_guard'])
        persist(s)
        return result
    except BaseException as exc:
        note_failure(s, exc)
        raise


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection(session):
    s = state(session.config)
    try:
        return (yield)
    except BaseException as exc:
        note_failure(s, exc)
        raise
    finally:
        # Critical: native SessionDB's root resolver is dynamic, not captured.
        # Restore actual HOME before ANY test fixture runs. TMPDIR stays owned.
        os.environ['HOME'] = str(OPERATOR_HOME)
        assert_native(s)
        s['home_restored_before_tests'] = os.environ['HOME']
        persist(s)


def pytest_collection_modifyitems(config, items):
    s = state(config)
    for item in items:
        if DENIED_MARKS.intersection(mark.name for mark in item.iter_markers()):
            raise pytest.UsageError('isolation selection may not bypass native protections: ' + item.nodeid)
    s['collected'] = [item.nodeid for item in items]
    persist(s)


@pytest.fixture(autouse=True)
def codex_isolation_proof(request, tmp_path, tmp_path_factory, _hermetic_environment,
                          _forbid_real_hermes_home_io, _kanban_write_guard,
                          _state_db_write_guard, _live_system_guard):
    s = state(request.config)
    assert_native(s)
    if os.environ.get('HOME') != str(OPERATOR_HOME):
        raise AssertionError('actual HOME not restored for dynamic native state guard')
    import hermes_constants
    from hermes_cli.profiles import get_profile_dir
    import hermes_state
    if hermes_state._STATE_DB_GUARD_BYPASS or os.environ.get('HERMES_STATE_DB_GUARD_BYPASS'):
        raise AssertionError('native state guard bypassed')
    paths = {'tmp_path': tmp_path, 'base': tmp_path_factory.getbasetemp(),
             'tempfile': Path(tempfile.gettempdir()),
             'hermes_home': hermes_constants.get_hermes_home(),
             'default_root': hermes_constants.get_default_hermes_root(),
             'default_profile': get_profile_dir('default')}
    for value in paths.values():
        owned_path(value, s['sandbox_path'])
    with tempfile.NamedTemporaryFile(prefix=PREFIX) as handle:
        probe = owned_path(handle.name, s['sandbox_path'])
        handle.write(b'actual-tempfile-probe')
        handle.flush()
    entry = {'nodeid': request.node.nodeid, 'paths': {k: str(v.resolve()) for k, v in paths.items()},
             'actual_tempfile_probe': str(probe), 'operator_root_capture_intact': True}
    s['tests'].append(entry)
    persist(s)
    yield s
    assert_native(s)


def pytest_runtest_logreport(report):
    # Config is deliberately not inferred from modules with potentially duplicated conftest identities.
    if _ACTIVE_CONFIG is not None:
        s = state(_ACTIVE_CONFIG)
        s['reports'].append({'nodeid': report.nodeid, 'phase': report.when, 'outcome': report.outcome})
        if report.failed:
            s.setdefault('primary_failure', {'type': 'NativeTestFailure', 'message': report.when})
        persist(s)


_ACTIVE_CONFIG = None


def pytest_configure(config):
    global _ACTIVE_CONFIG
    _ACTIVE_CONFIG = config


def pytest_sessionfinish(session, exitstatus):
    s = state(session.config)
    assert_native(s)
    s['sessionfinish_reached'] = True
    s['pytest_exit'] = int(exitstatus)
    s['actual_base'] = str(owned_path(session.config._tmp_path_factory.getbasetemp(), s['sandbox_path']))
    persist(s)


def pytest_unconfigure(config):
    s = getattr(config, '_codex_isolation_state', None)
    if s is not None:
        s['unconfigure_hook_reached'] = True
    # Full cleanup belongs to Config.add_cleanup: this hook is skipped before configure.
