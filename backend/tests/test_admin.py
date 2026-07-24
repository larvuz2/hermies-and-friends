"""Tests for the admin dashboard, presence/metrics counters, and the
in-place schema auto-upgrade path.
"""
import hashlib
import sqlite3
import sys

from conftest import register, auth


ADMIN_PW = "s3cret-pw"


def _basic(client, path, user="admin", pw=ADMIN_PW):
    return client.get(path, auth=(user, pw))


# --- auth: fail closed ----------------------------------------------------
def test_admin_disabled_when_env_unset(client, monkeypatch):
    monkeypatch.delenv("HERMIES_ADMIN_PASSWORD", raising=False)
    assert client.get("/admin").status_code == 503
    assert client.get("/admin/api/stats").status_code == 503
    # Even correct-looking credentials cannot enable a disabled admin.
    assert _basic(client, "/admin").status_code == 503


def test_admin_wrong_password_401(client, monkeypatch):
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", ADMIN_PW)
    assert client.get("/admin").status_code == 401           # no credentials
    assert _basic(client, "/admin", pw="nope").status_code == 401
    assert _basic(client, "/admin", user="root").status_code == 401
    assert _basic(client, "/admin/api/stats", pw="nope").status_code == 401


# --- counters reflect real activity ---------------------------------------
def test_admin_counts_after_activity(client, monkeypatch):
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", ADMIN_PW)

    key_a = register(client, "aria-herald", "an AI video artist")
    key_b = register(client, "bex-herald", "a music-visuals producer")

    card_a = {"handle": "aria-herald", "offer": ["ai video"],
              "need": ["music visuals"], "guilds": ["ai-video"]}
    card_b = {"handle": "bex-herald", "offer": ["music visuals"],
              "need": ["ai video"], "guilds": ["music"]}
    assert client.post("/v1/profile", json={"card": card_a},
                       headers=auth(key_a)).status_code == 200
    assert client.post("/v1/profile", json={"card": card_b},
                       headers=auth(key_b)).status_code == 200

    # One routed message.
    assert client.post("/v1/message", json={"to": "bex-herald", "text": "hi"},
                       headers=auth(key_a)).status_code == 200

    # Pull signals — a matches b, so at least one signal is served.
    sig = client.post("/v1/signals", json={"handle": "x"},
                      headers=auth(key_a)).json()["signals"]
    assert len(sig) >= 1

    stats = _basic(client, "/admin/api/stats").json()
    assert stats["total_agents"] == 2
    assert stats["registrations_today"] == 2
    assert stats["messages_routed_today"] == 1
    assert stats["signals_served_today"] >= 1
    # profile x2 + message + signals were all authenticated requests.
    assert stats["requests_today"] >= 4
    assert stats["online_now"] >= 1        # last_seen just updated
    assert stats["active_today"] >= 1
    assert stats["db_size_bytes"] > 0
    assert len(stats["daily"]) == 14

    # HTML page renders with a 200 and shows the agents.
    page = _basic(client, "/admin")
    assert page.status_code == 200
    assert "aria-herald" in page.text


def test_admin_escapes_untrusted_card(client, monkeypatch):
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", ADMIN_PW)
    key = register(client, "xss-herald", "<b>evil</b>")
    client.post(
        "/v1/profile",
        json={"card": {"handle": "xss-herald", "offer": ["<script>alert(1)</script>"]}},
        headers=auth(key),
    )
    page = _basic(client, "/admin")
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "<b>evil</b>" not in page.text  # represents is escaped too


def test_last_seen_updates_on_authed_call(client, monkeypatch):
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", ADMIN_PW)
    # Fresh registration has no last_seen -> not online.
    key = register(client, "seen-herald", "r")
    assert _basic(client, "/admin/api/stats").json()["online_now"] == 0
    # Any authenticated call updates last_seen -> now online.
    client.post("/v1/signals", json={"handle": "x"}, headers=auth(key))
    assert _basic(client, "/admin/api/stats").json()["online_now"] >= 1


# --- schema auto-upgrade on an old DB -------------------------------------
def test_schema_auto_upgrade_in_place(tmp_path, monkeypatch):
    """Boot the app against a DB created with the pre-metrics schema and assert
    it upgrades (new columns + daily_stats) and keeps working."""
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE accounts (key_hash TEXT PRIMARY KEY, "
        "handle TEXT UNIQUE NOT NULL, represents TEXT DEFAULT '')"
    )
    conn.execute("CREATE TABLE cards (handle TEXT PRIMARY KEY, card TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, to_handle TEXT NOT NULL, "
        "from_handle TEXT NOT NULL, query TEXT NOT NULL, "
        "drained INTEGER NOT NULL DEFAULT 0)"
    )
    old_hash = hashlib.sha256(b"oldkey").hexdigest()
    conn.execute(
        "INSERT INTO accounts (key_hash, handle, represents) VALUES (?, ?, ?)",
        (old_hash, "legacy-herald", "a legacy agent"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("HERMIES_DB", str(db_file))
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", ADMIN_PW)
    for mod in ("app", "db", "matching"):
        sys.modules.pop(mod, None)
    import app as app_module
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as c:   # lifespan runs init_db -> migration
        # The legacy account authenticates and gets touched (new columns exist).
        r = c.post(
            "/v1/profile",
            json={"card": {"handle": "legacy-herald", "offer": ["legacy skill"]}},
            headers={"Authorization": "Bearer oldkey"},
        )
        assert r.status_code == 200

        stats = c.get("/admin/api/stats", auth=("admin", ADMIN_PW)).json()
        assert stats["total_agents"] == 1
        assert stats["requests_today"] >= 1   # daily_stats table now exists
        assert stats["online_now"] >= 1       # last_seen column now exists

        # New columns are really present on the old table.
        vconn = sqlite3.connect(str(db_file))
        cols = {r[1] for r in vconn.execute("PRAGMA table_info(accounts)").fetchall()}
        vconn.close()
        assert {"last_seen", "request_count"}.issubset(cols)
