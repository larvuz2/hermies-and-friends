"""Frictionless auto-join + connectivity: the website path (clone -> enable ->
hermes) must reach the real hub with zero env and no API key, auto-registering
on first publish. And the status command must make live/offline obvious."""
import os

from hermix import _config, profile, commands
from hermix.client import (HermixClient, HttpTransport, ensure_registered,
                            make_transport)
from hermix.mock_backend import MockBackend


def _fake_llm(system, user, **kw):
    return "x"


# --- config -----------------------------------------------------------------
def test_default_hub_is_the_real_hub(monkeypatch):
    monkeypatch.delenv("HERMIX_API_URL", raising=False)
    assert _config.service_url() == "https://api.hermix.dev"
    assert _config.has_hub() is True


def test_empty_url_forces_offline(monkeypatch):
    monkeypatch.setenv("HERMIX_API_URL", "")
    assert _config.has_hub() is False
    assert isinstance(make_transport(), MockBackend)


def test_hub_url_selects_http_transport_without_key(monkeypatch):
    monkeypatch.delenv("HERMIX_API_URL", raising=False)
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    assert isinstance(make_transport(), HttpTransport)   # live-by-URL, key comes later


def test_persist_api_key_sets_process_and_file(monkeypatch, tmp_path):
    envf = tmp_path / ".env"
    monkeypatch.setenv("HERMIX_ENV_FILE", str(envf))
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    _config.persist_api_key("sk-live-123")
    assert os.environ["HERMIX_API_KEY"] == "sk-live-123"
    assert "HERMIX_API_KEY=sk-live-123" in envf.read_text(encoding="utf-8")


# --- ensure_registered ------------------------------------------------------
def test_ensure_registered_noop_when_already_keyed(monkeypatch):
    monkeypatch.setenv("HERMIX_API_KEY", "already")
    assert ensure_registered(HermixClient(MockBackend()),
                             profile.PublicCard(handle="x")) is True


def test_ensure_registered_noop_offline(monkeypatch):
    monkeypatch.setenv("HERMIX_API_URL", "")
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    assert ensure_registered(HermixClient(MockBackend()),
                             profile.PublicCard(handle="x")) is False


def test_ensure_registered_claims_and_persists_key(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMIX_API_URL", raising=False)      # default real hub
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    monkeypatch.setenv("HERMIX_ENV_FILE", str(tmp_path / ".env"))
    ok = ensure_registered(HermixClient(MockBackend()),      # mock -> "mock-key"
                           profile.PublicCard(handle="gus-herald", represents="AI film"))
    assert ok is True
    assert _config.api_key() == "mock-key"


# --- publish triggers auto-join --------------------------------------------
def test_publish_auto_registers(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    monkeypatch.delenv("HERMIX_API_URL", raising=False)
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    monkeypatch.setenv("HERMIX_ENV_FILE", str(tmp_path / ".env"))
    handler = commands.make_handler(HermixClient(MockBackend()),
                                    profile.PublicCard(), _fake_llm)
    out = handler('profile {"handle":"gus-herald","represents":"AI film",'
                  '"offer":["ai video"]}')
    assert "published" in out.lower()
    assert _config.api_key() == "mock-key"        # got a key during publish


# --- connectivity in /hermix status ---------------------------------------
def test_status_offline_banner(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    monkeypatch.setenv("HERMIX_API_URL", "")
    out = commands.make_handler(HermixClient(MockBackend()),
                                profile.PublicCard(), _fake_llm)("status")
    assert "OFFLINE" in out


def test_status_connected_banner(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    monkeypatch.delenv("HERMIX_API_URL", raising=False)
    monkeypatch.setenv("HERMIX_API_KEY", "key123")
    out = commands.make_handler(HermixClient(MockBackend()),   # healthz -> True
                                profile.PublicCard(handle="gus"), _fake_llm)("status")
    assert "connected" in out.lower()
