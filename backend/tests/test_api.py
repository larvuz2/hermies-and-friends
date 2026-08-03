"""End-to-end API tests for the Hermix hub contract."""
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


def test_register_throttle_429(client, monkeypatch):
    """Per-IP registration throttle: configurable/hour, the next one is rejected."""
    monkeypatch.setenv("HERMIX_REGISTER_MAX_PER_HOUR", "5")
    codes = []
    for i in range(6):
        r = client.post("/v1/register", json={"handle": f"reg-{i}", "represents": ""})
        codes.append(r.status_code)
    assert codes[:5] == [200] * 5
    assert codes[5] == 429


def test_register_throttle_is_per_real_client_ip(client, monkeypatch):
    """Behind our proxy every request looks like 127.0.0.1, which would make the
    throttle a GLOBAL cap and block real signups. X-Forwarded-For must bucket
    per real client instead."""
    monkeypatch.setenv("HERMIX_REGISTER_MAX_PER_HOUR", "2")
    # Client A burns its budget.
    for i in range(2):
        assert client.post("/v1/register", json={"handle": f"a-{i}", "represents": ""},
                           headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 200
    assert client.post("/v1/register", json={"handle": "a-x", "represents": ""},
                       headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 429
    # A DIFFERENT client is unaffected.
    assert client.post("/v1/register", json={"handle": "b-0", "represents": ""},
                       headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 200
    # Multi-hop XFF: the first entry is the real client.
    assert client.post("/v1/register", json={"handle": "c-0", "represents": ""},
                       headers={"X-Forwarded-For": "7.7.7.7, 127.0.0.1"}).status_code == 200


def test_v1_signal_shape_unchanged(client):
    """/v1 SIGNAL shape stays {kind, agent, why, score} + additive components."""
    key_a = register(client, CARD_A["handle"], CARD_A["represents"])
    key_b = register(client, CARD_B["handle"], CARD_B["represents"])
    _publish(client, key_a, CARD_A)
    _publish(client, key_b, CARD_B)

    r = client.post("/v1/discover", json={"card": CARD_A}, headers=auth(key_a))
    signals = r.json()["signals"]
    assert signals, "expected at least one match"
    for s in signals:
        # Frozen keys are present and correctly typed.
        assert s["kind"] == "match"
        assert isinstance(s["agent"], str)
        assert isinstance(s["why"], str)
        assert isinstance(s["score"], (int, float))
        assert 0.0 <= s["score"] <= 10.0
        # Additive, non-breaking components block.
        comp = s["components"]
        assert set(comp) == {"need_to_offer", "offer_to_need", "guilds", "presence"}
        for v in comp.values():
            assert 0.0 <= v <= 1.0


# --- live client config ----------------------------------------------------
def test_client_config_served_to_agents(client):
    """Every plugin polls this; it is how tuning ships without users acting."""
    key = register(client, "cfg-herald", "r")
    r = client.post("/v1/config", json={}, headers=auth(key))
    assert r.status_code in (404, 405)          # it is a GET, not a POST
    r = client.get("/v1/config", headers=auth(key))
    assert r.status_code == 200
    body = r.json()
    assert "knobs" in body and isinstance(body["knobs"], dict)
    assert body["knobs"]["interrupt_threshold"] > 0
    assert "version" in body


def test_client_config_requires_auth(client):
    assert client.get("/v1/config").status_code == 401


def test_client_config_survives_a_broken_file(client, monkeypatch, tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("HERMIX_CLIENT_CONFIG_FILE", str(bad))
    key = register(client, "cfg2-herald", "r")
    r = client.get("/v1/config", headers=auth(key))
    assert r.status_code == 200                 # degrades, never 500s
    assert r.json()["knobs"] == {}


def test_operator_edit_is_served_live(client, monkeypatch, tmp_path):
    f = tmp_path / "cfg.json"
    f.write_text('{"version": 9, "knobs": {"interrupt_threshold": 7.7}}', encoding="utf-8")
    monkeypatch.setenv("HERMIX_CLIENT_CONFIG_FILE", str(f))
    key = register(client, "cfg3-herald", "r")
    assert client.get("/v1/config", headers=auth(key)).json()["knobs"]["interrupt_threshold"] == 7.7
    # edit on the hub -> next poll sees it, no restart
    f.write_text('{"version": 10, "knobs": {"interrupt_threshold": 3.3}}', encoding="utf-8")
    assert client.get("/v1/config", headers=auth(key)).json()["knobs"]["interrupt_threshold"] == 3.3
