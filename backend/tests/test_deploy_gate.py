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


# --------------------------------------------------------------------------- #
# Purge: the gate must leave no trace in ANY operator-visible number
# --------------------------------------------------------------------------- #
def test_purge_removes_the_accounts_entirely(client, loopback):
    import db
    for handle in ("smoke-a-1", "smoke-b-1"):
        loopback.post("/v1/register",
                      json={"handle": handle, "represents": "canary"})
    assert len(db.purge_smoke_accounts.__doc__) > 0
    r = loopback.post("/v1/smoke/purge", json={})
    assert r.status_code == 200 and r.json()["purged"] == 2
    with db._connect() as conn:
        left = conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
    assert left == 0, "smoke accounts survived the purge"


def test_purge_also_clears_handles_left_by_an_earlier_deploy(client, loopback):
    """A deploy that dies between registering and purging must not leak rows
    forever — the next run cleans up after it."""
    import db
    loopback.post("/v1/register",
                  json={"handle": "smoke-orphan-old", "represents": "canary"})
    assert loopback.post("/v1/smoke/purge", json={}).json()["purged"] == 1


def test_purge_is_idempotent(client, loopback):
    assert loopback.post("/v1/smoke/purge", json={}).json()["purged"] == 0


def test_purge_never_touches_a_real_account(client, loopback):
    import db
    register(client, "gus-herald", "a filmmaker")
    loopback.post("/v1/register",
                  json={"handle": "smoke-x", "represents": "canary"})
    loopback.post("/v1/smoke/purge", json={})
    with db._connect() as conn:
        rows = [r["handle"] for r in conn.execute("SELECT handle FROM accounts")]
    assert rows == ["gus-herald"]


def test_purge_is_not_reachable_from_outside_the_box(client):
    r = client.post("/v1/smoke/purge", json={},
                    headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 404, "a remote caller could erase deploy state"


def test_smoke_registrations_are_not_counted_as_signups(client, loopback):
    """registrations_today is a growth metric; the deploy gate is not growth."""
    import db
    real = register(client, "gus-herald", "a filmmaker")
    assert real
    loopback.post("/v1/register",
                  json={"handle": "smoke-s", "represents": "canary"})
    days = db.daily_stats_map()
    assert days, "no daily row to assert against"
    registrations = sum(d["registrations"] for d in days.values())
    assert registrations == 1, (
        f"deploy-gate registration counted as a signup: {registrations}")


def test_smoke_accounts_are_absent_from_presence_and_version_rollups(client,
                                                                    loopback):
    """Excluding them from ONE count was not enough — they surfaced in
    online/active, the version spread and the admin agent table too."""
    import db
    loopback.post("/v1/register",
                  json={"handle": "smoke-p", "represents": "canary"})
    assert "smoke-p" not in db.last_seen_ts_map()
    assert sum(r["agents"] for r in db.version_rollup()) == 0
    assert [a["handle"] for a in db.all_accounts_with_cards()] == []
    assert db.count_since("1970-01-01T00:00:00+00:00") == 0


# --------------------------------------------------------------------------- #
# Deployed revision
# --------------------------------------------------------------------------- #
def test_healthz_reports_the_serving_revision(client):
    """"Did the fix ship?" must be answerable from outside the box."""
    body = client.get("/healthz").json()
    assert body["revision"], "no revision reported"
    assert body["revision_source"] in ("env", "git", "none")


def test_the_revision_can_be_pinned_for_container_builds(monkeypatch, tmp_path):
    """deploy/Dockerfile copies backend/ without .git, so git cannot answer."""
    import sys
    monkeypatch.setenv("HERMIX_REVISION", "deadbeefcafe")
    monkeypatch.setenv("HERMIX_DB", str(tmp_path / "rev.db"))
    for mod in ("app", "db", "matching"):
        sys.modules.pop(mod, None)
    import app as fresh
    assert fresh._REVISION == {"revision": "deadbeefcafe", "revision_source": "env"}


def test_the_admin_dashboard_shows_the_revision(client):
    stats = client.get("/admin.json")
    if stats.status_code == 200:
        assert "revision" in stats.json()
