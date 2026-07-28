"""Launch additions #4 (trust receipt) and #5 (guided introduction).

Both are about making the privacy architecture VISIBLE and the final step
unambiguous. The receipt must never overstate what was shared or verified, and
the introduction preview must send nothing.
"""
import json

import pytest

from hermies import matchmaker, profile, tools
from hermies.client import HermiesClient
from hermies.mock_backend import MockBackend


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMIES_QUIET_HOURS", "")


def _card():
    return profile.PublicCard(handle="gus-herald", represents="AI film",
                              offer=["ai video"], need=["music visuals"])


def _finding(state, **kw):
    item = {
        "id": "abc123", "handle": "mira-herald", "represents": "an artist",
        "pitch": "They score AI film and need exactly your footage.",
        "reason": "", "evidence": "happy to collaborate in August",
        "next_step": "reach out", "score": 8.4, "note": "", "verified": True,
        "cards_only": False, "intent": None, "why_matched": "their 'scoring' fits your need 'music visuals'",
        "ring1_available": ["six years in game audio"], "turns": 3,
    }
    item.update(kw)
    state["outbox"]["inflight"] = [item]
    return state


# --- #4 trust receipt -------------------------------------------------------
def test_receipt_covers_all_five_questions():
    st = _finding(matchmaker.new_state())
    r = matchmaker.receipt(st, "abc123")
    for section in ("WHY IT MATCHED", "WHAT WAS VERIFIED",
                    "WHAT THE CONVERSATION COULD DRAW ON",
                    "WHAT NEVER LEFT THIS MACHINE",
                    "WHY I INTERRUPTED YOU NOW"):
        assert section in r, f"missing: {section}"
    assert "mira-herald" in r
    assert "happy to collaborate in August" in r        # their actual words
    assert "six years in game audio" in r               # the approved fact


def test_receipt_is_honest_when_nothing_was_verified():
    """A verdict reached on profiles alone must SAY so, not imply a chat."""
    st = _finding(matchmaker.new_state(), verified=False, cards_only=True,
                  evidence="")
    r = matchmaker.receipt(st, "abc123")
    assert "never replied" in r
    assert "public profile only" in r
    assert "never replied" in r.split("WHY I INTERRUPTED")[1]   # also in why-now


def test_receipt_does_not_overstate_shared_facts():
    """With no Ring-1 facts it must say the card alone was used."""
    st = _finding(matchmaker.new_state(), ring1_available=[])
    r = matchmaker.receipt(st, "abc123")
    assert "No extra approved facts" in r


def test_receipt_explains_an_intent_led_interruption():
    st = _finding(matchmaker.new_state(), intent="a colourist for AI footage")
    r = matchmaker.receipt(st, "abc123")
    assert "you asked me to find" in r and "colourist" in r


def test_receipt_handles_an_unknown_id_gracefully():
    assert "don't have a finding" in matchmaker.receipt(matchmaker.new_state(), "nope")


def test_why_tool_returns_the_receipt():
    st = _finding(matchmaker.new_state())
    matchmaker.save_state(st)
    handlers = {s["name"]: s["handler"]
                for s in tools.build(HermiesClient(MockBackend()), _card(),
                                     llm=lambda s, u, **k: "")}
    res = json.loads(handlers["hermies_why"]({"finding_id": "abc123"}))
    assert res["success"] and "WHY IT MATCHED" in res["receipt"]


# --- #5 guided introduction -------------------------------------------------
CONTACT = {"name": "Gus Garza", "email": "gus@example.com",
           "socials": ["@gus"], "never_share": False}


def test_preview_shows_exactly_what_would_be_shared():
    st = _finding(matchmaker.new_state())
    text = matchmaker.format_intro_preview(
        matchmaker.intro_preview(st, "mira-herald", CONTACT))
    assert "nothing has been sent yet" in text
    assert "gus@example.com" in text and "Gus Garza" in text
    assert "WHAT THEY WOULD NOT RECEIVE" in text
    assert "must approve too" in text


def test_preview_sends_nothing():
    """The whole point: previewing must have no side effects on the network."""
    b = MockBackend()
    client = HermiesClient(b)
    st = _finding(matchmaker.new_state())
    matchmaker.save_state(st)
    handlers = {s["name"]: s["handler"]
                for s in tools.build(client, _card(), llm=lambda s, u, **k: "")}
    before = len(b.list_threads()["threads"])
    res = json.loads(handlers["hermies_intro_preview"]({"to": "mira-herald"}))
    assert res["success"] is True
    assert len(b.list_threads()["threads"]) == before, "preview must not open a thread"


def test_preview_refuses_when_contact_is_marked_never_share():
    st = _finding(matchmaker.new_state())
    p = matchmaker.intro_preview(st, "mira-herald",
                                 {**CONTACT, "never_share": True})
    assert p["blocked"] is True
    assert "can't offer" in matchmaker.format_intro_preview(p)


def test_preview_says_so_when_no_contact_is_saved():
    st = _finding(matchmaker.new_state())
    p = matchmaker.intro_preview(st, "mira-herald", {})
    assert p["have_contact"] is False
    assert "no contact details saved" in matchmaker.format_intro_preview(p)


def test_intro_note_uses_what_the_dig_established():
    st = _finding(matchmaker.new_state())
    p = matchmaker.intro_preview(st, "mira-herald", CONTACT)
    assert "score AI film" in p["intro"]
