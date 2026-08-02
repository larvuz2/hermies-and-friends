"""Block / unblock / report on the plugin side.

The human-facing half of the hub enforcement (backend/tests/test_safety.py).
What matters here: the words are honest about what happens, "spam" now actually
stops someone rather than only hiding them, and reporting is never silently a
block.
"""
import json

import pytest

from hermies import commands, matchmaker, profile, tools
from hermies.client import HermiesClient
from hermies.mock_backend import MockBackend


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIES_HOME", str(tmp_path))


def _card():
    return profile.PublicCard(handle="gus-herald", offer=["ai video"])


def _handler():
    return commands.make_handler(HermiesClient(MockBackend()), _card(), llm=None)


def _tools(client=None):
    client = client or HermiesClient(MockBackend())
    return {s["name"]: s["handler"] for s in tools.build(client, _card(), llm=None)}


# --- commands -------------------------------------------------------------- #
def test_block_says_plainly_what_happens_and_that_they_are_not_told():
    out = _handler()("block mira-herald")
    low = out.lower()
    assert "blocked @mira-herald" in low
    assert "not told" in low                  # no notification to them
    assert "unblock" in low                   # always offer the way back


def test_blocked_list_round_trips():
    h = _handler()
    assert "haven't blocked anyone" in h("blocked").lower()
    h("block mira-herald noisy")
    listed = h("blocked")
    assert "mira-herald" in listed and "noisy" in listed


def test_unblock_is_honest_when_there_was_no_block():
    assert "wasn't blocked" in _handler()("unblock nobody-herald")


def test_report_requires_a_reason_and_lists_the_valid_ones():
    out = _handler()("report mira-herald")
    assert "Which reason?" in out
    for reason in ("spam", "harassment", "impersonation", "scam"):
        assert reason in out


def test_report_says_it_is_not_a_block():
    """Conflating the two would make people hesitate to report someone they
    still want to hear from."""
    out = _handler()("report mira-herald spam kept pitching")
    low = out.lower()
    assert "operator" in low
    assert "not told" in low
    assert "does not block" in low and "/hermies block" in out


def test_the_help_line_advertises_the_safety_commands():
    out = _handler()("frobnicate")
    for cmd in ("block", "unblock", "blocked", "report"):
        assert cmd in out


# --- tools ----------------------------------------------------------------- #
def test_block_tool_reports_success_and_the_no_notification_rule():
    out = json.loads(_tools()["hermies_block"]({"handle": "@mira-herald"}))
    assert out["success"] and out["blocked"] == "mira-herald"
    assert "not told" in out["note"].lower()


def test_block_tool_needs_a_handle():
    assert json.loads(_tools()["hermies_block"]({}))["success"] is False


def test_report_tool_rejects_an_invented_reason():
    out = json.loads(_tools()["hermies_report"]({"handle": "x", "reason": "vibes"}))
    assert out["success"] is False and "spam" in out["accepted"]


def test_report_tool_tells_the_agent_it_did_not_block():
    out = json.loads(_tools()["hermies_report"](
        {"handle": "mira-herald", "reason": "scam", "detail": "asked for money"}))
    assert out["success"] and "hermies_block" in out["note"]


# --- spam feedback must actually stop them --------------------------------- #
def test_marking_a_finding_as_spam_blocks_them_at_the_hub():
    """Before this, "spam" only stopped us SURFACING them — they could still
    open threads and spend our inference."""
    backend = MockBackend()
    client = HermiesClient(backend)
    st = matchmaker.new_state()
    st["outbox"]["delivered"] = [{"id": "abc123", "handle": "mira-herald",
                                  "score": 5.0, "ts": 1_000_000.0}]
    matchmaker.save_state(st)

    out = json.loads(_tools(client)["hermies_feedback"](
        {"finding_id": "abc123", "verdict": "spam"}))
    assert out["success"] and out["verdict"] == "spam"
    assert "blocked" in out["note"].lower()
    assert "mira-herald" in backend._blocks


def test_other_verdicts_do_not_block_anyone():
    backend = MockBackend()
    client = HermiesClient(backend)
    st = matchmaker.new_state()
    st["outbox"]["delivered"] = [{"id": "abc123", "handle": "mira-herald",
                                  "score": 5.0, "ts": 1_000_000.0}]
    matchmaker.save_state(st)
    for verdict in ("useful", "wrong_fit", "too_early"):
        json.loads(_tools(client)["hermies_feedback"](
            {"finding_id": "abc123", "verdict": verdict}))
    assert backend._blocks == {}


def test_a_blocked_agent_disappears_from_discovery_offline_too():
    """Offline/mock mode must exercise the same path as the hub, or the demo
    keeps proposing someone the real network refuses to connect."""
    backend = MockBackend()
    client = HermiesClient(backend)
    # Terms chosen to overlap the mock's seeded agents, which match on tokens.
    client.publish_profile(profile.PublicCard(
        handle="gus-herald", offer=["3d worlds"],
        need=["music visualizers"], guilds=["ai-video"]).public_dict())
    before = {s["agent"] for s in client.list_signals("gus-herald")}
    assert before, "the mock returned no candidates to begin with"
    victim = sorted(before)[0]
    client.block(victim, "test")
    after = {s["agent"] for s in client.list_signals("gus-herald")}
    assert victim not in after


def test_a_blocked_agent_cannot_be_dug_offline_either():
    backend = MockBackend()
    client = HermiesClient(backend)
    client.block("mira-herald", "test")
    res = client.open_thread("mira-herald", "dig", "s")
    assert res.get("status") == 403 and "thread_id" not in res
