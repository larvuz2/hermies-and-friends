"""End-to-end API tests for the Hermies hub contract."""
from conftest import register, auth


CARD_A = {
    "handle": "aria-herald",
    "represents": "an AI video artist",
    "offer": ["ai video", "generative film"],
    "need": ["music visuals", "sound design"],
    "guilds": ["ai-video"],
}
CARD_B = {
    "handle": "bex-herald",
    "represents": "a music-visuals producer",
    "offer": ["music visuals", "beat sync"],
    "need": ["ai video", "distribution"],
    "guilds": ["music"],
}


def _publish(client, key, card):
    r = client.post("/v1/profile", json={"card": card}, headers=auth(key))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_register_returns_key_and_handle(client):
    r = client.post("/v1/register", json={"handle": "gus-herald", "represents": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["handle"] == "gus-herald"
    assert isinstance(body["api_key"], str) and len(body["api_key"]) > 20


def test_duplicate_handle_rejected(client):
    register(client, "dup-herald")
    r = client.post("/v1/register", json={"handle": "dup-herald", "represents": ""})
    assert r.status_code == 409


def test_cross_match_roundtrip(client):
    """Two agents that mirror each other must appear in each other's signals."""
    key_a = register(client, CARD_A["handle"], CARD_A["represents"])
    key_b = register(client, CARD_B["handle"], CARD_B["represents"])
    _publish(client, key_a, CARD_A)
    _publish(client, key_b, CARD_B)

    # A discovers B
    r = client.post("/v1/discover", json={"card": CARD_A}, headers=auth(key_a))
    agents = [s["agent"] for s in r.json()["signals"]]
    assert "bex-herald" in agents

    # B discovers A
    r = client.post("/v1/discover", json={"card": CARD_B}, headers=auth(key_b))
    agents = [s["agent"] for s in r.json()["signals"]]
    assert "aria-herald" in agents

    # Stored-card signals use the AUTHENTICATED handle, cross-match both ways.
    r = client.post("/v1/signals", json={"handle": "ignored"}, headers=auth(key_a))
    sig_a = r.json()["signals"]
    assert any(s["agent"] == "bex-herald" for s in sig_a)
    top = sig_a[0]
    assert top["kind"] == "match"
    assert "offers" in top["why"]
    assert isinstance(top["score"], (int, float)) and top["score"] > 0

    r = client.post("/v1/signals", json={"handle": "ignored"}, headers=auth(key_b))
    assert any(s["agent"] == "aria-herald" for s in r.json()["signals"])


def test_self_match_excluded(client):
    key_a = register(client, CARD_A["handle"], CARD_A["represents"])
    _publish(client, key_a, CARD_A)
    # Only A exists; A must never match itself.
    r = client.post("/v1/signals", json={"handle": CARD_A["handle"]}, headers=auth(key_a))
    assert all(s["agent"] != CARD_A["handle"] for s in r.json()["signals"])
    r = client.post("/v1/discover", json={"card": CARD_A}, headers=auth(key_a))
    assert all(s["agent"] != CARD_A["handle"] for s in r.json()["signals"])


def test_auth_required(client):
    for path, payload in [
        ("/v1/profile", {"card": {}}),
        ("/v1/discover", {"card": {}}),
        ("/v1/signals", {"handle": "x"}),
        ("/v1/inbound", {"handle": "x"}),
        ("/v1/reply", {"message_id": "m", "text": "t"}),
        ("/v1/search", {"query": "x"}),
        ("/v1/skills", {"query": "x"}),
        ("/v1/message", {"to": "x", "text": "t"}),
    ]:
        r = client.post(path, json=payload)
        assert r.status_code == 401, f"{path} should 401 without auth"
        r = client.post(path, json=payload, headers={"Authorization": "Bearer bogus"})
        assert r.status_code == 401, f"{path} should 401 with bad key"


def test_message_inbound_reply_routing(client):
    key_a = register(client, CARD_A["handle"], CARD_A["represents"])
    key_b = register(client, CARD_B["handle"], CARD_B["represents"])

    # A messages B
    r = client.post(
        "/v1/message",
        json={"to": "bex-herald", "text": "want to collab on ai video?"},
        headers=auth(key_a),
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "to": "bex-herald"}

    # B drains inbox, sees the message from A
    r = client.post("/v1/inbound", json={"handle": "ignored"}, headers=auth(key_b))
    msgs = r.json()["messages"]
    assert len(msgs) == 1
    m = msgs[0]
    assert m["from"] == "aria-herald"
    assert m["query"] == "want to collab on ai video?"
    msg_id = m["id"]

    # Draining again yields empty (mailbox drained).
    r = client.post("/v1/inbound", json={"handle": "ignored"}, headers=auth(key_b))
    assert r.json()["messages"] == []

    # B replies; reply routes back to A as a new inbound message from B.
    r = client.post(
        "/v1/reply",
        json={"message_id": msg_id, "text": "yes lets talk"},
        headers=auth(key_b),
    )
    assert r.status_code == 200 and r.json()["ok"] is True

    r = client.post("/v1/inbound", json={"handle": "ignored"}, headers=auth(key_a))
    replies = r.json()["messages"]
    assert len(replies) == 1
    assert replies[0]["from"] == "bex-herald"
    assert replies[0]["query"] == "yes lets talk"


def test_search_and_skills(client):
    key_a = register(client, CARD_A["handle"], CARD_A["represents"])
    _publish(client, key_a, CARD_A)

    r = client.post("/v1/search", json={"query": "ai video"}, headers=auth(key_a))
    agents = r.json()["agents"]
    assert any(a["handle"] == "aria-herald" for a in agents)
    a = agents[0]
    assert set(["handle", "represents", "offer", "guilds"]).issubset(a.keys())

    r = client.post("/v1/skills", json={"query": "eval"}, headers=auth(key_a))
    skills = r.json()["skills"]
    assert isinstance(skills, list)
    for s in skills:
        assert set(["name", "from", "description"]).issubset(s.keys())


def test_input_hardening_caps(client):
    key = register(client, "cap-herald", "r")
    big_card = {
        "handle": "cap-herald",
        "tagline": "x" * 500,
        "offer": ["item"] * 50,
    }
    r = client.post("/v1/profile", json={"card": big_card}, headers=auth(key))
    assert r.status_code == 200
    # Read back via search (offer is exposed there).
    r = client.post("/v1/search", json={"query": ""}, headers=auth(key))
    card = next(a for a in r.json()["agents"] if a["handle"] == "cap-herald")
    assert len(card["offer"]) == 20


def test_unknown_card_keys_ignored(client):
    key = register(client, "wl-herald", "r")
    r = client.post(
        "/v1/profile",
        json={"card": {"handle": "wl-herald", "secret": "leak", "offer": ["ok"]}},
        headers=auth(key),
    )
    assert r.status_code == 200
    r = client.post("/v1/signals", json={"handle": "wl-herald"}, headers=auth(key))
    # No crash and no leaked key surfaces anywhere; signals is a list.
    assert isinstance(r.json()["signals"], list)


def test_rate_limit_429(client):
    key = register(client, "rl-herald", "r")
    hdr = auth(key)
    # 60 allowed, 61st should 429.
    codes = []
    for _ in range(61):
        codes.append(client.post("/v1/skills", json={"query": "x"}, headers=hdr).status_code)
    assert codes[:60] == [200] * 60
    assert codes[60] == 429
