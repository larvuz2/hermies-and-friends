"""Credentials must resolve per PROFILE, not per process.

Hermes 0.19.1 added a fail-closed per-profile secret scope for the multiplexing
gateway (one process serving many profiles). We read HERMIX_API_KEY, which is
NOT on their global-env allowlist — so it is a profile secret, and reading
os.environ directly would resolve whichever profile's value happened to be in
the process env. For this plugin that means talking to the hub as ANOTHER user.

Multiplexing is off by default, so none of this changes behaviour today. These
tests exist so it stays correct when someone turns it on.
"""
import sys
import types

import pytest

from hermix import _config


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    monkeypatch.delenv("HERMIX_API_URL", raising=False)
    _config._KEY_CACHE.clear()
    yield
    sys.modules.pop("agent.secret_scope", None)
    sys.modules.pop("agent", None)
    _config._KEY_CACHE.clear()


def _install_scope(scope=None, multiplex=True, raises=False):
    """Stand in for Hermes' agent.secret_scope module."""
    class UnscopedSecretError(RuntimeError):
        pass

    def get_secret(name, default=None):
        if raises:
            raise UnscopedSecretError(name)
        if scope is None:
            return default
        return scope.get(name, default)

    mod = types.ModuleType("agent.secret_scope")
    mod.get_secret = get_secret
    mod.is_multiplex_active = lambda: multiplex
    mod.UnscopedSecretError = UnscopedSecretError
    pkg = types.ModuleType("agent")
    pkg.secret_scope = mod
    sys.modules["agent"] = pkg
    sys.modules["agent.secret_scope"] = mod
    return mod


# --------------------------------------------------------------------------- #
# Nothing changes for anyone running today
# --------------------------------------------------------------------------- #
def test_without_the_scope_module_behaviour_is_unchanged(monkeypatch):
    monkeypatch.setenv("HERMIX_API_KEY", "k-plain")
    monkeypatch.setenv("HERMIX_API_URL", "https://hub.example")
    assert _config.api_key() == "k-plain"
    assert _config.service_url() == "https://hub.example"


def test_default_url_still_applies():
    assert _config.service_url() == _config.DEFAULT_API_URL


# --------------------------------------------------------------------------- #
# The bug this fixes: another profile's value in os.environ
# --------------------------------------------------------------------------- #
def test_the_scope_wins_over_a_stale_process_env(monkeypatch):
    """os.environ holds agent B's key; the scope says we are agent A."""
    monkeypatch.setenv("HERMIX_API_KEY", "k-OTHER-PROFILE")
    _install_scope({"HERMIX_API_KEY": "k-mine"})
    assert _config.api_key() == "k-mine"


def test_the_scope_also_governs_which_hub_we_joined(monkeypatch):
    monkeypatch.setenv("HERMIX_API_URL", "https://other-profiles-hub")
    _install_scope({"HERMIX_API_URL": "https://my-hub"})
    assert _config.service_url() == "https://my-hub"


def test_an_unscoped_read_fails_closed_rather_than_guessing(monkeypatch):
    """Falling back to os.environ here would reintroduce the very leak the
    scope exists to prevent. Not acting beats acting as the wrong person."""
    monkeypatch.setenv("HERMIX_API_KEY", "k-OTHER-PROFILE")
    _install_scope(raises=True)
    assert _config.api_key() == ""
    assert _config.is_live() is False


def test_a_non_scope_error_still_falls_back(monkeypatch):
    """A broken resolver must not take the agent off the network."""
    monkeypatch.setenv("HERMIX_API_KEY", "k-plain")
    mod = _install_scope()
    mod.get_secret = lambda name, default=None: (_ for _ in ()).throw(
        RuntimeError("something else entirely"))
    assert _config.api_key() == "k-plain"


# --------------------------------------------------------------------------- #
# Auto-join must not hand our identity to every other profile
# --------------------------------------------------------------------------- #
def test_persist_does_not_pollute_the_shared_env_under_multiplex(monkeypatch):
    _install_scope({}, multiplex=True)
    _config.persist_api_key("k-fresh")
    assert "HERMIX_API_KEY" not in os_environ()
    # ...but the key is usable immediately, or auto-join would break.
    assert _config.api_key() == "k-fresh"


def test_persist_still_uses_the_env_on_a_normal_install(monkeypatch):
    """Single-profile deployments keep the behaviour they have always had."""
    _config.persist_api_key("k-fresh")
    assert os_environ().get("HERMIX_API_KEY") == "k-fresh"
    assert _config.api_key() == "k-fresh"


def test_the_session_cache_is_per_profile(monkeypatch, tmp_path):
    """One process, two profiles: neither may see the other's key."""
    _install_scope({}, multiplex=True)
    a, b = tmp_path / "a", tmp_path / "b"

    monkeypatch.setenv("HERMES_HOME", str(a))
    _config.persist_api_key("k-agent-a")
    assert _config.api_key() == "k-agent-a"

    monkeypatch.setenv("HERMES_HOME", str(b))
    assert _config.api_key() == "", "agent B saw agent A's key"
    _config.persist_api_key("k-agent-b")
    assert _config.api_key() == "k-agent-b"

    monkeypatch.setenv("HERMES_HOME", str(a))
    assert _config.api_key() == "k-agent-a"


def test_persist_still_writes_the_profile_env_file(monkeypatch, tmp_path):
    """The durable path is the profile's own .env, which survives a restart."""
    env_file = tmp_path / ".env"
    monkeypatch.setenv("HERMIX_ENV_FILE", str(env_file))
    _install_scope({}, multiplex=True)
    _config.persist_api_key("k-durable")
    assert "HERMIX_API_KEY=k-durable" in env_file.read_text(encoding="utf-8")


def os_environ():
    import os
    return os.environ


def test_a_cleared_key_does_not_linger_on_a_normal_install(monkeypatch):
    """The session cache exists only because multiplexing forbids os.environ.
    Consulting it outside that case would keep a rotated or revoked key alive
    for the life of the process."""
    _config.persist_api_key("k-old")
    assert _config.api_key() == "k-old"
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    assert _config.api_key() == "", "a cleared key survived in the cache"
