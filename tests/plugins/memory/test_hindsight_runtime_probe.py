"""The embedded wrapper launches an isolated API, not the host ML stack."""

import sys
from importlib.metadata import version
from types import ModuleType

import pytest

import plugins.memory.hindsight.embedded as embedded


def _wrapper(monkeypatch, command):
    package = ModuleType("hindsight_embed")
    package.__path__ = []
    manager_module = ModuleType("hindsight_embed.daemon_embed_manager")

    class DaemonEmbedManager:
        def _find_api_command(self, api_version, env=None):
            assert api_version == "0.9.2"
            assert env is None  # wrapper chooses its own default environment
            return command

    setattr(manager_module, "DaemonEmbedManager", DaemonEmbedManager)
    monkeypatch.setitem(sys.modules, "hindsight_embed", package)
    monkeypatch.setitem(sys.modules, "hindsight_embed.daemon_embed_manager", manager_module)
    monkeypatch.setattr(embedded, "_embed_distribution_version", lambda name: "0.9.2", raising=False)


def test_uvx_isolated_launcher_does_not_require_host_ml_modules(monkeypatch, tmp_path):
    _wrapper(monkeypatch, ["uvx", "hindsight-api@0.9.2"])
    launcher = tmp_path / "uvx"
    launcher.touch(mode=0o755)
    monkeypatch.setattr(embedded, "_which_launcher", lambda name: str(launcher) if name == "uvx" else None, raising=False)

    assert embedded._check_local_runtime() == (True, None)
    assert "hindsight" not in sys.modules
    assert "sentence_transformers" not in sys.modules


def test_missing_uvx_launcher_is_unavailable(monkeypatch):
    _wrapper(monkeypatch, ["uvx", "hindsight-api@0.9.2"])
    monkeypatch.setattr(embedded, "_which_launcher", lambda name: None, raising=False)

    available, reason = embedded._check_local_runtime()
    assert not available
    assert "launcher" in reason.lower()


def test_missing_wrapper_is_unavailable_with_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "hindsight_embed.daemon_embed_manager", None)

    available, reason = embedded._check_local_runtime()
    assert not available
    assert reason is not None
    assert "hindsight_embed" in reason


def test_empty_wrapper_command_is_unavailable(monkeypatch):
    _wrapper(monkeypatch, [])

    available, reason = embedded._check_local_runtime()
    assert not available
    assert "launcher" in reason.lower()


def test_absolute_wrapper_launcher_is_accepted(monkeypatch, tmp_path):
    launcher = tmp_path / "hindsight-api"
    launcher.touch(mode=0o755)
    _wrapper(monkeypatch, [str(launcher)])
    monkeypatch.setattr(embedded, "_which_launcher", lambda name: None, raising=False)

    assert embedded._check_local_runtime() == (True, None)


def test_nonexecutable_absolute_launcher_is_unavailable(monkeypatch, tmp_path):
    launcher = tmp_path / "hindsight-api"
    launcher.touch()
    launcher.chmod(0o600)
    _wrapper(monkeypatch, [str(launcher)])
    monkeypatch.setattr(embedded, "_which_launcher", lambda name: None)

    available, reason = embedded._check_local_runtime()
    assert not available
    assert reason is not None and "launcher" in reason.lower()


def test_installed_092_wrapper_is_compatible_without_starting_daemon():
    pytest.importorskip("hindsight_embed.daemon_embed_manager")
    if version("hindsight-embed") != "0.9.2":
        pytest.skip("requires hindsight-embed 0.9.2")

    assert embedded._check_local_runtime() == (True, None)


def test_probe_does_not_cross_profile_secret_or_base_url_scopes(monkeypatch, tmp_path):
    from agent import secret_scope

    _wrapper(monkeypatch, ["uvx", "hindsight-api@0.9.2"])
    launcher = tmp_path / "uvx"
    launcher.touch(mode=0o755)
    monkeypatch.setattr(embedded, "_which_launcher", lambda name: str(launcher) if name == "uvx" else None)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setenv("HINDSIGHT_API_LLM_API_KEY", "wrong-ambient-key")
    monkeypatch.setenv("HINDSIGHT_API_LLM_BASE_URL", "https://wrong-ambient.invalid")

    for key, url in (
        ("key-a", "https://a.invalid/v1"),
        ("key-b", "https://b.invalid/v1"),
        ("key-a", "https://a.invalid/v1"),
    ):
        scope = secret_scope.set_secret_scope({
            "HINDSIGHT_API_LLM_API_KEY": key,
            "HINDSIGHT_API_LLM_BASE_URL": url,
        })
        try:
            assert embedded._check_local_runtime() == (True, None)
            result = embedded._build_embedded_profile_env({"llm_provider": "openai"})
            assert result["HINDSIGHT_API_LLM_API_KEY"] == key
            assert result["HINDSIGHT_API_LLM_BASE_URL"] == url
        finally:
            secret_scope.reset_secret_scope(scope)
