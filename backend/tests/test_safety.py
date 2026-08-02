"""Blocking and reporting — enforced by the HUB.

The load-bearing idea: a counterpart runs their own client, and this project is
open source, so "stop contacting me" can never depend on their software
behaving. A block is one-sided to create and two-sided in effect, and it is the
hub that refuses the thread.
"""
import app
from conftest import register, auth


def _pair(client):
    return (register(client, "alice-herald", "an AI filmmaker"),
            register(client, "bob-herald", "a growth marketer"))


def _open(client, key, to="bob-herald"):
    return client.post("/v1/thread/open",
                       json={"to": to, "kind": "dig", "subject": "s"},
                       headers=auth(key))


# --- the block itself ------------------------------------------------------ #
def test_blocking_stops_the_blocker_opening_threads(client):
    key_a, _ = _pair(client)
    assert _open(client, key_a).status_code == 200
    assert client.post("/v1/block", json={"handle": "bob-herald"},
                       headers=auth(key_a)).status_code == 200
    assert _open(client, key_a).status_code == 403


def test_blocking_also_stops_the_BLOCKED_agent_reaching_back(client):
    """The whole point. A one-directional block would leave them able to keep
    opening conversations at the person who blocked them."""
    key_a, key_b = _pair(client)
    client.post("/v1/block", json={"handle": "bob-herald"}, headers=auth(key_a))
    r = _open(client, key_b, to="alice-herald")
    assert r.status_code == 403


def test_the_refusal_never_says_who_blocked_whom(client):
    """Telling the opener 'they blocked you' leaks a decision that is none of
    their business and makes blocking feel confrontational."""
    key_a, key_b = _pair(client)
    client.post("/v1/block", json={"handle": "bob-herald"}, headers=auth(key_a))
    body = _open(client, key_b, to="alice-herald").text.lower()
    for leak in ("blocked you", "has blocked", "alice"):
        assert leak not in body, body


def test_a_block_hides_them_from_discovery_both_ways(client):
    key_a, key_b = _pair(client)
    client.post("/v1/profile", json={"card": {"offer": ["ai video"],
                                              "need": ["paid social"]}},
                headers=auth(key_a))
    client.post("/v1/profile", json={"card": {"offer": ["paid social"],
                                              "need": ["ai video"]}},
                headers=auth(key_b))

    def handles(key):
        r = client.post("/v1/signals", json={}, headers=auth(key))
        return {s["agent"] for s in r.json()["signals"]}

    assert "bob-herald" in handles(key_a)
    client.post("/v1/block", json={"handle": "bob-herald"}, headers=auth(key_a))
    assert "bob-herald" not in handles(key_a)
    assert "alice-herald" not in handles(key_b), "the blocked agent still saw them"


def test_unblock_restores_contact(client):
    key_a, _ = _pair(client)
    client.post("/v1/block", json={"handle": "bob-herald"}, headers=auth(key_a))
    assert _open(client, key_a).status_code == 403
    r = client.post("/v1/unblock", json={"handle": "bob-herald"}, headers=auth(key_a))
    assert r.status_code == 200 and r.json()["removed"] is True
    assert _open(client, key_a).status_code == 200


def test_blocks_list_shows_only_your_own(client):
    """Never who blocked YOU — that would hand out other people's decisions."""
    key_a, key_b = _pair(client)
    client.post("/v1/block", json={"handle": "alice-herald"}, headers=auth(key_b))
    mine = client.get("/v1/blocks", headers=auth(key_a)).json()["blocks"]
    assert mine == []


def test_you_can_block_an_agent_that_has_left(client):
    """Gating on handle_exists would make the block evaporate if they return."""
    key_a, _ = _pair(client)
    r = client.post("/v1/block", json={"handle": "ghost-herald"}, headers=auth(key_a))
    assert r.status_code == 200


def test_cannot_block_yourself(client):
    key_a, _ = _pair(client)
    r = client.post("/v1/block", json={"handle": "alice-herald"}, headers=auth(key_a))
    assert r.status_code == 400


def test_block_requires_auth(client):
    assert client.post("/v1/block", json={"handle": "x"}).status_code in (401, 403)


# --- reporting ------------------------------------------------------------- #
def test_report_reaches_the_operator_and_counts_distinct_reporters(client):
    key_a, key_b = _pair(client)
    register(client, "carol-herald", "c")
    r = client.post("/v1/report",
                    json={"handle": "bob-herald", "reason": "spam",
                          "detail": "kept pitching after I said no"},
                    headers=auth(key_a))
    assert r.status_code == 200 and r.json()["distinct_reporters"] == 1
    # The same person reporting twice is still one reporter.
    again = client.post("/v1/report", json={"handle": "bob-herald", "reason": "spam"},
                        headers=auth(key_a))
    assert again.json()["distinct_reporters"] == 1


def test_reporting_does_not_block(client):
    """Different decisions. Conflating them would make people hesitate to
    report someone they still want to hear from."""
    key_a, _ = _pair(client)
    client.post("/v1/report", json={"handle": "bob-herald", "reason": "scam"},
                headers=auth(key_a))
    assert _open(client, key_a).status_code == 200


def test_report_rejects_an_unknown_reason(client):
    key_a, _ = _pair(client)
    r = client.post("/v1/report", json={"handle": "bob-herald", "reason": "vibes"},
                    headers=auth(key_a))
    assert r.status_code == 400


def test_cannot_report_yourself(client):
    key_a, _ = _pair(client)
    r = client.post("/v1/report", json={"handle": "alice-herald", "reason": "spam"},
                    headers=auth(key_a))
    assert r.status_code == 400
