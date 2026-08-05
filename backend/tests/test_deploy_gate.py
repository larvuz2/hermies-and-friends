"""/healthz must expose matching QUALITY, not just liveness.

The hub stays up when the real embedding model cannot load, serving hashing
n-grams instead. That is the right availability choice, but it is a much weaker
product (recall@10 0.90 -> 0.77, cross-vocabulary 6/8 -> 2/8) and an HTTP 200
looks identical either way — so a deploy silently ships the weaker one and the
first evidence is users saying the network "doesn't find anything".

deploy/hostinger/smoke.py gates on the fields pinned here, so they are load
bearing: renaming one turns the gate into a no-op that always passes.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import auth, register


@pytest.fixture()
def loopback(client):
    """A client whose direct peer really is 127.0.0.1.

    Starlette's default TestClient reports the host as "testclient", which the
    reserved-prefix check correctly rejects — under uvicorn the deploy gate
    connects to 127.0.0.1 and reports exactly that. Presenting a real loopback
    peer keeps the production check strict instead of loosening it to make a
    test pass."""
    with TestClient(client._app_module.app,
                    client=("127.0.0.1", 54321)) as c:
        yield c


def test_healthz_still_reports_liveness(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["service"] == "hermix-hub"


def test_healthz_names_the_embedding_engine(client):
    """The suite runs on the fallback encoder (see conftest), so this is the
    degraded case — the exact one a deploy must refuse to call a success."""
    body = client.get("/healthz").json()
    assert body["engine"] == "fallback"
    assert body["degraded"] is True
    assert body["model"]


def test_degraded_is_derived_from_the_mode_not_hardcoded(client):
    """Guards the inversion: `degraded` must mean "not the real model"."""
    body = client.get("/healthz").json()
    assert body["degraded"] == (body["engine"] != "fastembed")


def test_healthz_needs_no_credentials(client):
    """The gate runs before any agent exists, so it cannot authenticate."""
    r = client.get("/healthz")
    assert r.status_code == 200


def test_the_gate_reports_index_size(client):
    """A loaded model indexing zero cards still fails the product, so the gate
    needs to see the corpus."""
    before = client.get("/healthz").json()["indexed_cards"]
    key = register(client, "gus-herald", "a filmmaker")
    client.post("/v1/profile", json={"card": {
        "handle": "gus-herald", "represents": "a filmmaker",
        "offer": ["ai video"], "need": ["a composer"]}}, headers=auth(key))
    assert client.get("/healthz").json()["indexed_cards"] == before + 1


# --------------------------------------------------------------------------- #
# Smoke accounts are infrastructure, not users
# --------------------------------------------------------------------------- #
def test_smoke_accounts_do_not_count_as_agents(client, loopback):
    """One pair per deploy would inflate the only number the operator uses to
    judge a 10-25 person beta."""
    import db
    register(client, "gus-herald", "a filmmaker")
    assert db.count_accounts() == 1

    for handle in ("smoke-a-123", "smoke-b-123"):
        r = loopback.post("/v1/register",
                          json={"handle": handle, "represents": "canary"})
        assert r.status_code == 200, r.text
    assert db.count_accounts() == 1, "deploy-gate accounts were counted as agents"


def test_the_reserved_prefix_cannot_be_claimed_remotely(client):
    """Otherwise anyone could register invisibly by choosing the prefix."""
    r = client.post("/v1/register",
                    json={"handle": "smoke-impostor", "represents": "x"},
                    headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 400
    assert "reserved" in r.text.lower()


def test_ordinary_handles_are_unaffected_by_the_reservation(client):
    for handle in ("smokey-herald", "smoke", "a-smoke-test"):
        r = client.post("/v1/register",
                        json={"handle": handle, "represents": "x"},
                        headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 200, f"{handle}: {r.text}"
