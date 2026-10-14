"""Additive real lifecycle smoke; fault injection belongs to the owned driver."""
import sys

import pytest

from tests.agent import _codex_isolation_plugin as isolation


@pytest.fixture
def lifecycle_fault(request, codex_isolation_proof):
    # Native safety fixtures and the proof fixture have already executed.
    if getattr(request.config, '_lifecycle_fault_lane', '') == 'fixture':
        raise RuntimeError('DECLARED_LIFECYCLE_FAULT:fixture')


def test_native_lifecycle_and_late_bootstrap(request, lifecycle_fault, codex_isolation_proof):
    s = codex_isolation_proof
    isolation.assert_native(s)
    assert s['operator_home'] == '/home/hermes'
    assert s['early_option_from_known_namespace'] is True
    assert s['home_restored_before_tests'] == '/home/hermes'
    required = {'_hermetic_environment', '_forbid_real_hermes_home_io',
                '_kanban_write_guard', '_state_db_write_guard', '_live_system_guard'}
    assert required.issubset(request.fixturenames)
    before_recovery = len(s['recovery_adapter']['events'])
    before_payload = len(s['payload_adapter']['events'])
    # Not preloaded to work around the native real-home tripwire.
    assert 'hermes_bootstrap' not in sys.modules
    import hermes_bootstrap
    assert hermes_bootstrap._root == isolation.CORE
    assert len(s['recovery_adapter']['events']) > before_recovery
    assert len(s['payload_adapter']['events']) > before_payload
    assert all(event['result'] is False for event in s['recovery_adapter']['events'])
    assert all(event['result'] is None for event in s['payload_adapter']['events'])
    s['preflight_native_fixture_dependencies'] = sorted(required)
    s['preflight_late_bootstrap_executed'] = True
    isolation.persist(s)
