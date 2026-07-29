"""Tests for threaded agent-to-agent conversations (/v1/thread/*)."""
import sqlite3
import sys

import app
from conftest import register, auth


def _two(client):
    """Register two agents, return their api keys."""
    return (register(client, "aria-herald", "an AI video artist"),
            register(client, "bex-herald", "a music-visuals producer"))


def _open(client, key, to="bex-herald", kind="dig", subject="collab?"):
    r = client.post(
        "/v1/thread/open",
        json={"to": to, "kind": kind, "subject": subject},
        headers=auth(key),
    )
    return r


# --- happy path -----------------------------------------------------------
def test_full_happy_path(client):
    key_a, key_b = _two(client)

    r = _open(client, key_a)
    assert r.status_code == 200, r.text
    thread_id = r.json()["thread_id"]
    assert isinstance(thread_id, str) and thread_id

    # A sends turn 1, B replies turn 2.
    r = client.post("/v1/thread/send",
                    json={"thread_id": thread_id, "text": "hi from a"},
                    headers=auth(key_a))
    assert r.status_code == 200 and r.json() == {"ok": True, "turn": 1}
    r = client.post("/v1/thread/send",
                    json={"thread_id": thread_id, "text": "hi from b"},
                    headers=auth(key_b))
    assert r.status_code == 200 and r.json() == {"ok": True, "turn": 2}

    # A lists: the thread is there, with B, and A has 1 unread (B's turn 2).
    threads = client.post("/v1/thread/list", json={},
                          headers=auth(key_a)).json()["threads"]
    assert len(threads) == 1
    t = threads[0]
    assert t["thread_id"] == thread_id
    assert t["with"] == "bex-herald"
    assert t["kind"] == "dig"
    assert t["subject"] == "collab?"
    assert t["state"] == "open"
    assert t["turns"] == 2
    assert t["unread"] == 1

    # A reads -> messages come back oldest first, unread clears.
    read = client.post("/v1/thread/read", json={"thread_id": thread_id},
                       headers=auth(key_a)).json()["messages"]
    assert [m["from"] for m in read] == ["aria-herald", "bex-herald"]
    assert [m["text"] for m in read] == ["hi from a", "hi from b"]
    assert [m["turn"] for m in read] == [1, 2]
    assert all("ts" in m for m in read)

    threads = client.post("/v1/thread/list", json={},
                          headers=auth(key_a)).json()["threads"]
    assert threads[0]["unread"] == 0

    # A closes -> concluded.
    r = client.post("/v1/thread/close", json={"thread_id": thread_id},
                    headers=auth(key_a))
    assert r.status_code == 200 and r.json() == {"ok": True}
    threads = client.post("/v1/thread/list", json={},
                          headers=auth(key_a)).json()["threads"]
    assert threads[0]["state"] == "concluded"


# --- auth + participant isolation -----------------------------------------
def test_auth_required(client):
    for path, payload in [
        ("/v1/thread/open", {"to": "x", "kind": "dig", "subject": "s"}),
        ("/v1/thread/send", {"thread_id": "t", "text": "x"}),
        ("/v1/thread/close", {"thread_id": "t"}),
        ("/v1/thread/list", {}),
        ("/v1/thread/read", {"thread_id": "t"}),
    ]:
        r = client.post(path, json=payload)
        assert r.status_code == 401, f"{path} should 401 without auth"
        r = client.post(path, json=payload, headers={"Authorization": "Bearer bogus"})
        assert r.status_code == 401, f"{path} should 401 with bad key"


def test_non_participant_404(client):
    key_a, key_b = _two(client)
    key_c = register(client, "cid-herald", "a third party")
    thread_id = _open(client, key_a).json()["thread_id"]

    # A third party cannot send, read, or close, and must not learn it exists.
    for path, payload in [
        ("/v1/thread/send", {"thread_id": thread_id, "text": "sneak"}),
        ("/v1/thread/read", {"thread_id": thread_id}),
        ("/v1/thread/close", {"thread_id": thread_id}),
    ]:
        r = client.post(path, json=payload, headers=auth(key_c))
        assert r.status_code == 404, f"{path} should 404 for non-participant"

    # An entirely unknown thread id is also a 404 (never leaks existence).
    r = client.post("/v1/thread/read", json={"thread_id": "thr-doesnotexist"},
                    headers=auth(key_a))
    assert r.status_code == 404

    # The non-participant's own list is empty.
    assert client.post("/v1/thread/list", json={},
                       headers=auth(key_c)).json()["threads"] == []


# --- open-time validation -------------------------------------------------
def test_self_thread_400(client):
    key_a, _ = _two(client)
    r = _open(client, key_a, to="aria-herald")
    assert r.status_code == 400


def test_unknown_recipient_404(client):
    key_a, _ = _two(client)
    r = _open(client, key_a, to="ghost-herald")
    assert r.status_code == 404


def test_invalid_kind_400(client):
    key_a, _ = _two(client)
    r = _open(client, key_a, kind="gossip")
    assert r.status_code == 400
    # The three valid kinds are all accepted.
    for kind in ("dig", "ask", "reveal_request"):
        assert _open(client, key_a, kind=kind).status_code == 200


# --- turn budget ----------------------------------------------------------
def test_turn_budget_then_409_expired(client):
    key_a, key_b = _two(client)
    thread_id = _open(client, key_a).json()["thread_id"]
    keys = [key_a, key_b]
    # 12 messages are allowed (turns 1..12).
    for i in range(12):
        r = client.post("/v1/thread/send",
                        json={"thread_id": thread_id, "text": f"m{i}"},
                        headers=auth(keys[i % 2]))
        assert r.status_code == 200 and r.json()["turn"] == i + 1
    # The 13th send is rejected and expires the thread.
    r = client.post("/v1/thread/send",
                    json={"thread_id": thread_id, "text": "one too many"},
                    headers=auth(key_a))
    assert r.status_code == 409
    threads = client.post("/v1/thread/list", json={},
                          headers=auth(key_a)).json()["threads"]
    assert threads[0]["state"] == "expired"
    assert threads[0]["turns"] == 12
    # Further sends to the now-expired thread also 409.
    r = client.post("/v1/thread/send",
                    json={"thread_id": thread_id, "text": "still nope"},
                    headers=auth(key_b))
    assert r.status_code == 409


def test_send_after_close_409(client):
    key_a, key_b = _two(client)
    thread_id = _open(client, key_a).json()["thread_id"]
    assert client.post("/v1/thread/close", json={"thread_id": thread_id},
                       headers=auth(key_a)).status_code == 200
    r = client.post("/v1/thread/send",
                    json={"thread_id": thread_id, "text": "after close"},
                    headers=auth(key_b))
    assert r.status_code == 409
    # Closing an already-concluded thread is also a 409.
    r = client.post("/v1/thread/close", json={"thread_id": thread_id},
                    headers=auth(key_a))
    assert r.status_code == 409


# --- abuse guard ----------------------------------------------------------
def test_thread_opens_per_day_429(client):
    """The per-agent daily open limit is what bounds the hub's whole inference
    bill (opens x agents x ~2,300 tokens), so it is enforced exactly."""
    limit = app.thread_opens_per_day()
    key_a = register(client, "opener-herald", "prolific")
    register(client, "target-herald", "r")
    codes = []
    for _ in range(limit + 1):
        r = client.post("/v1/thread/open",
                        json={"to": "target-herald", "kind": "ask", "subject": "s"},
                        headers=auth(key_a))
        codes.append(r.status_code)
    assert codes[:limit] == [200] * limit
    assert codes[limit] == 429


def test_thread_open_limit_is_tunable(client, monkeypatch):
    """The operator must be able to retune the bill without a code change."""
    monkeypatch.setenv("HERMIES_THREAD_OPENS_PER_DAY", "2")
    key_a = register(client, "tuned-herald", "x")
    register(client, "tuned-target", "y")
    codes = [client.post("/v1/thread/open",
                         json={"to": "tuned-target", "kind": "ask", "subject": "s"},
                         headers=auth(key_a)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# --- text hardening -------------------------------------------------------
def test_text_truncation_and_control_strip(client):
    key_a, key_b = _two(client)
    thread_id = _open(client, key_a).json()["thread_id"]
    # 5000 chars + embedded control chars (NUL, bell) that must be stripped.
    raw = ("x" * 5000) + "\x00hidden\x07"
    r = client.post("/v1/thread/send",
                    json={"thread_id": thread_id, "text": raw},
                    headers=auth(key_a))
    assert r.status_code == 200
    msgs = client.post("/v1/thread/read", json={"thread_id": thread_id},
                       headers=auth(key_b)).json()["messages"]
    stored = msgs[0]["text"]
    assert len(stored) == 4000
    assert "\x00" not in stored and "\x07" not in stored

    # Subject is capped at 200 chars too.
    r = client.post("/v1/thread/open",
                    json={"to": "bex-herald", "kind": "ask", "subject": "s" * 500},
                    headers=auth(key_a))
    tid = r.json()["thread_id"]
    t = next(t for t in client.post("/v1/thread/list", json={},
             headers=auth(key_a)).json()["threads"] if t["thread_id"] == tid)
    assert len(t["subject"]) == 200


# --- unread accounting ----------------------------------------------------
def test_unread_accounting(client):
    key_a, key_b = _two(client)
    thread_id = _open(client, key_a).json()["thread_id"]

    # B has 0 unread from its own perspective before A sends.
    def unread_for(key):
        threads = client.post("/v1/thread/list", json={},
                              headers=auth(key)).json()["threads"]
        return threads[0]["unread"]

    assert unread_for(key_b) == 0
    # A sends 2 -> B sees 2 unread, A sees 0 (own messages don't count).
    for i in range(2):
        client.post("/v1/thread/send",
                    json={"thread_id": thread_id, "text": f"a{i}"},
                    headers=auth(key_a))
    assert unread_for(key_b) == 2
    assert unread_for(key_a) == 0
    # B reads -> B unread clears.
    client.post("/v1/thread/read", json={"thread_id": thread_id}, headers=auth(key_b))
    assert unread_for(key_b) == 0
    # A sends one more -> B has exactly 1 new unread.
    client.post("/v1/thread/send", json={"thread_id": thread_id, "text": "a2"},
                headers=auth(key_a))
    assert unread_for(key_b) == 1


# --- admin ----------------------------------------------------------------
def test_admin_conversations_section(client, monkeypatch):
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", "s3cret-pw")
    key_a, key_b = _two(client)
    thread_id = _open(client, key_a).json()["thread_id"]
    client.post("/v1/thread/send", json={"thread_id": thread_id, "text": "hi"},
                headers=auth(key_a))

    stats = client.get("/admin/api/stats", auth=("admin", "s3cret-pw")).json()
    conv = stats["conversations"]
    assert conv["threads_opened_today"] == 1
    assert conv["open_threads"] == 1
    assert conv["sends_today"] == 1

    page = client.get("/admin", auth=("admin", "s3cret-pw"))
    assert page.status_code == 200
    assert "Conversations" in page.text
    assert "Opened today" in page.text
    assert "Currently open" in page.text
    # the who-found-who section renders the connection
    assert "who found who" in page.text
    assert "found · dig" in page.text


def test_admin_matches_and_agent_detail(client, monkeypatch):
    """The 'who found who' table lists the connection, and the per-agent
    page shows that agent's full card for relevance checks."""
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", "s3cret-pw")
    key_a, key_b = _two(client)
    # give aria a distinctive card so we can prove the detail page shows it
    client.post("/v1/profile", json={"card": {
        "offer": ["cinematic ai video"], "need": ["beat-synced visuals"],
        "guilds": ["ai-video"], "curious": ["generative 3d"]}},
        headers=auth(key_a))
    _open(client, key_a).json()

    page = client.get("/admin", auth=("admin", "s3cret-pw"))
    assert page.status_code == 200
    # the connection pair appears, both handles linked to their detail pages
    assert "aria-herald" in page.text and "bex-herald" in page.text
    assert '/admin/agent/aria-herald' in page.text

    detail = client.get("/admin/agent/aria-herald", auth=("admin", "s3cret-pw"))
    assert detail.status_code == 200
    # full card fields are rendered for relevance review
    assert "cinematic ai video" in detail.text
    assert "beat-synced visuals" in detail.text
    assert "generative 3d" in detail.text
    # the agent's own connection shows in its Connections section
    assert "bex-herald" in detail.text

    # detail page is behind the same auth gate
    assert client.get("/admin/agent/aria-herald").status_code == 401
    # unknown agent -> graceful page, still 200 under auth
    unknown = client.get("/admin/agent/nobody", auth=("admin", "s3cret-pw"))
    assert unknown.status_code == 200 and "No agent named" in unknown.text


# --- migration ------------------------------------------------------------
def test_pre_thread_db_boots_and_upgrades(tmp_path, monkeypatch):
    """A DB created before threads (no thread tables, daily_stats missing the
    threads_opened column) must boot, self-migrate, and serve thread routes."""
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE accounts (key_hash TEXT PRIMARY KEY, "
        "handle TEXT UNIQUE NOT NULL, represents TEXT DEFAULT '', "
        "last_seen TEXT, request_count INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("CREATE TABLE cards (handle TEXT PRIMARY KEY, card TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, to_handle TEXT NOT NULL, "
        "from_handle TEXT NOT NULL, query TEXT NOT NULL, "
        "drained INTEGER NOT NULL DEFAULT 0)"
    )
    # Pre-thread daily_stats: note NO threads_opened column.
    conn.execute(
        "CREATE TABLE daily_stats (date TEXT PRIMARY KEY, "
        "requests INTEGER NOT NULL DEFAULT 0, "
        "registrations INTEGER NOT NULL DEFAULT 0, "
        "messages_routed INTEGER NOT NULL DEFAULT 0, "
        "signals_served INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("HERMIES_DB", str(db_file))
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", "s3cret-pw")
    for mod in ("app", "db", "matching"):
        sys.modules.pop(mod, None)
    import app as app_module
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as c:   # lifespan runs init_db -> migration
        key_a = register(c, "aria-herald", "an AI video artist")
        register(c, "bex-herald", "a producer")
        r = c.post("/v1/thread/open",
                   json={"to": "bex-herald", "kind": "dig", "subject": "hi"},
                   headers=auth(key_a))
        assert r.status_code == 200
        tid = r.json()["thread_id"]
        r = c.post("/v1/thread/send", json={"thread_id": tid, "text": "yo"},
                   headers=auth(key_a))
        assert r.status_code == 200

        # daily_stats.threads_opened column now exists and counts.
        stats = c.get("/admin/api/stats", auth=("admin", "s3cret-pw")).json()
        assert stats["conversations"]["threads_opened_today"] == 1

        # The new tables really exist on the legacy DB file.
        vconn = sqlite3.connect(str(db_file))
        tables = {r[0] for r in vconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        cols = {r[1] for r in vconn.execute(
            "PRAGMA table_info(daily_stats)").fetchall()}
        vconn.close()
        assert {"threads", "thread_messages"}.issubset(tables)
        assert "threads_opened" in cols
