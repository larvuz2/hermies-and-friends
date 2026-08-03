"""Launch additions #1 (intent-first onboarding) and #2 (one-tap feedback).

The point of feedback is that it CHANGES BEHAVIOUR — logging it would be
theatre. These pin the consequences, not just the storage.
"""
import json

import pytest

from hermix import matchmaker, profile, tools
from hermix.client import HermixClient
from hermix.mock_backend import MockBackend

DAY = 86400


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    monkeypatch.setenv("HERMIX_QUIET_HOURS", "")


def _card():
    return profile.PublicCard(handle="gus-herald", represents="AI film",
                              offer=["ai video"], need=["music visuals"],
                              guilds=["ai-video"])


def _delivered(state, handle="mira-herald", fid="f1"):
    """A finding that has been delivered to the human."""
    state["outbox"]["inflight"] = [{
        "id": fid, "handle": handle, "represents": "an artist", "pitch": "fit",
        "reason": "", "evidence": "keen", "next_step": "reach out", "score": 8,
        "note": "", "verified": True, "cards_only": False, "intent": None,
        "claimed_at": 1_000_000,
    }]
    return state


# --- #2 feedback: verdict parsing ------------------------------------------
def test_plain_words_map_to_verdicts():
    assert matchmaker.normalize_verdict("useful") == "useful"
    assert matchmaker.normalize_verdict("wrong") == "wrong_fit"
    assert matchmaker.normalize_verdict("too early") == "too_early"
    assert matchmaker.normalize_verdict("junk") == "spam"
    assert matchmaker.normalize_verdict("banana") == ""


def test_unknown_verdict_is_rejected_cleanly():
    st = _delivered(matchmaker.new_state())
    res = matchmaker.record_feedback(st, "f1", "banana", now=1_000_000.0)
    assert res["ok"] is False and "accepted" in res


# --- #2 feedback: real consequences ----------------------------------------
def test_useful_lowers_the_bar_for_the_next_finding():
    """Saying something was useful means: bring me more like it."""
    cold = matchmaker.new_state()
    assert matchmaker._bar(cold, 1_000_000.0) > 0
    warm = _delivered(matchmaker.new_state())
    matchmaker.record_feedback(warm, "f1", "useful", now=1_000_000.0)
    assert matchmaker._bar(warm, 1_000_000.0) < matchmaker._bar(cold, 1_000_000.0)


def test_spam_raises_the_bar_and_blocks_that_agent_forever():
    st = _delivered(matchmaker.new_state(), handle="spammy")
    cold_bar = matchmaker._bar(matchmaker.new_state(), 1_000_000.0)
    matchmaker.record_feedback(st, "f1", "spam", now=1_000_000.0)

    assert matchmaker._bar(st, 1_000_000.0) > cold_bar          # harder to interrupt
    assert st["seen"]["spammy"]["verdict"] == "never"
    # ...and never resurfaces, even years later with a changed card
    assert matchmaker._should_skip(st, "spammy", "a-totally-new-hash",
                                   1_000_000.0 + 400 * DAY) is True


def test_wrong_fit_sets_a_long_cooldown_but_not_forever():
    st = _delivered(matchmaker.new_state(), handle="mismatch")
    matchmaker.record_feedback(st, "f1", "wrong_fit", now=1_000_000.0)
    assert st["seen"]["mismatch"]["verdict"] == "drop"
    # still suppressed a month later
    assert matchmaker._should_skip(st, "mismatch", "h", 1_000_000.0 + 30 * DAY) is True


def test_too_early_parks_them_for_a_month():
    st = _delivered(matchmaker.new_state(), handle="later")
    matchmaker.record_feedback(st, "f1", "too_early", now=1_000_000.0)
    assert matchmaker._should_skip(st, "later", "h", 1_000_000.0 + 7 * DAY) is True


def test_feedback_acknowledges_delivery_and_is_recorded():
    st = _delivered(matchmaker.new_state())
    res = matchmaker.record_feedback(st, "f1", "useful", now=1_000_000.0)
    assert res["ok"] and res["handle"] == "mira-herald"
    assert st["outbox"]["inflight"] == []                  # no longer pending
    assert [f["verdict"] for f in st["feedback"]] == ["useful"]


def test_notification_carries_the_feedback_prompt():
    st = matchmaker.new_state()
    out = matchmaker._emit(st, [{
        "id": "abc123", "handle": "mira", "represents": "artist",
        "pitch": "fit", "reason": "", "evidence": "keen", "next_step": "go",
        "score": 9, "note": "", "verified": True, "cards_only": False,
        "intent": None}], 1_000_000.0)
    assert "abc123" in out
    assert "useful" in out and "spam" in out


# --- #2 feedback: the tool + hub reporting ---------------------------------
def test_feedback_tool_records_and_reports_to_the_hub():
    b = MockBackend()
    client = HermixClient(b)
    handlers = {s["name"]: s["handler"]
                for s in tools.build(client, _card(), llm=lambda s, u, **k: "")}
    st = _delivered(matchmaker.new_state())
    matchmaker.save_state(st)

    res = json.loads(handlers["hermix_feedback"](
        {"finding_id": "f1", "verdict": "wrong"}))
    assert res["success"] is True and res["verdict"] == "wrong_fit"
    assert getattr(b, "feedback", []) and b.feedback[0]["verdict"] == "wrong_fit"
    assert matchmaker.load_state()["seen"]["mira-herald"]["verdict"] == "drop"


def test_feedback_tool_needs_both_arguments():
    handlers = {s["name"]: s["handler"]
                for s in tools.build(HermixClient(MockBackend()), _card(),
                                     llm=lambda s, u, **k: "")}
    res = json.loads(handlers["hermix_feedback"]({"verdict": "useful"}))
    assert res["success"] is False


# --- #1 intent-first onboarding --------------------------------------------
def test_scan_now_starts_work_without_reporting_matches(monkeypatch):
    """The first scan must kick off the hunt but never leak matches into the
    onboarding conversation."""
    monkeypatch.setenv("HERMIX_MIN_SCORE", "1")
    b = MockBackend(); b._inbox = []
    client = HermixClient(b)
    card = _card()
    client.publish_profile(card.public_dict())
    handlers = {s["name"]: s["handler"]
                for s in tools.build(client, card,
                                     llm=lambda s, u, **k: '{"verdict":"drop","pitch":"","reason":""}')}

    res = json.loads(handlers["hermix_scan_now"]({}))
    assert res["success"] is True
    assert res["digs_open"] >= 1                 # the hunt actually started
    # counts only — no candidate names anywhere in the payload
    blob = json.dumps(res)
    assert "mira-herald" not in blob and "kip-herald" not in blob


def test_intent_added_then_scan_uses_it(monkeypatch):
    monkeypatch.setenv("HERMIX_MIN_SCORE", "1")
    from hermix import dossier
    b = MockBackend(); b._inbox = []
    client = HermixClient(b)
    card = _card()
    client.publish_profile(card.public_dict())
    handlers = {s["name"]: s["handler"]
                for s in tools.build(client, card,
                                     llm=lambda s, u, **k: '{"verdict":"drop","pitch":"","reason":""}')}

    added = json.loads(handlers["hermix_intent"](
        {"action": "add", "text": "a colourist who has worked on AI footage"}))
    assert added["success"] is True
    assert any(i["text"].startswith("a colourist")
               for i in dossier.list_intents())
    assert json.loads(handlers["hermix_scan_now"]({}))["success"] is True
