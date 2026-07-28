"""Feature: Ask Another Agent.

The promise: ask your agent to find something out; it talks to the right agent
in the background, and comes back with what it learned. The other HUMAN is
never contacted, identity never moves, and the human never waits around.
"""
import json

import pytest

from hermies import matchmaker, profile, tools
from hermies.client import HermiesClient
from hermies.mock_backend import MockBackend

DAY = 86400


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMIES_QUIET_HOURS", "")


def _card():
    return profile.PublicCard(handle="gus-herald", represents="an AI filmmaker",
                              offer=["ai video"], need=["distribution"])


class AskLlm:
    """Routes by system prompt: the opener vs the final report."""
    REPORT = ("ANSWER: They have distributed two indie features.\n"
              "CONFIRMED: Their human has direct distribution experience.\n"
              "UNCERTAIN: Whether they have current capacity.\n"
              "USEFUL: Get legal clarity and audience evidence ready first.\n"
              "INTEREST: open\n"
              "NEXT: Ask what materials they expect before considering it.")

    def __call__(self, system, user, *, purpose=None):
        if system.startswith("You are reporting back"):
            return self.REPORT
        return "Hi - I represent an AI filmmaker. Have you handled distribution?"


def _client():
    b = MockBackend()
    b._inbox = []
    return b, HermiesClient(b)


# --- preview (MVP steps 3-4) ------------------------------------------------
def test_preview_shows_the_question_and_sends_nothing():
    b, client = _client()
    before = len(b.list_threads()["threads"])
    p = matchmaker.ask_preview(_card(), "mira-herald",
                               "have you distributed an indie feature?",
                               ["six years in game audio"])
    text = matchmaker.format_ask_preview(p)
    assert "nothing has been sent yet" in text
    assert "have you distributed an indie feature?" in text
    assert "six years in game audio" in text
    assert "human is not contacted" in text.lower()
    assert len(b.list_threads()["threads"]) == before


def test_preview_names_what_stays_private():
    text = matchmaker.format_ask_preview(
        matchmaker.ask_preview(_card(), "x", "q?", []))
    assert "contact details" in text and "private dossier" in text
    assert "your public card only" in text          # no ring-1 facts to offer


# --- starting an investigation (step 5) -------------------------------------
def test_ask_opens_a_thread_and_tracks_it():
    b, client = _client()
    st = matchmaker.new_state()
    res = matchmaker.start_ask(st, client, _card(), "mira-herald",
                               "have you handled distribution?", [], AskLlm(),
                               now=1_000_000.0)
    assert res["ok"] is True
    ask = st["asks"]["mira-herald"]
    assert ask["thread_id"] and ask["our_turns"] == 1 and ask["awaiting"] is True
    kinds = [t["kind"] for t in b.list_threads()["threads"]]
    assert "ask" in kinds                       # a real ask thread on the hub


def test_ask_needs_a_handle_and_a_question():
    _, client = _client()
    st = matchmaker.new_state()
    assert matchmaker.start_ask(st, client, _card(), "", "q", [], AskLlm())["ok"] is False
    assert matchmaker.start_ask(st, client, _card(), "x", "", [], AskLlm())["ok"] is False


# --- the background loop + report (steps 6-7) -------------------------------
def test_reply_produces_a_structured_report_delivered_to_the_human():
    b, client = _client()
    card, llm = _card(), AskLlm()
    st = matchmaker.new_state()
    matchmaker.start_ask(st, client, card, "mira-herald",
                         "have you handled distribution?", [], llm,
                         now=1_000_000.0)
    tid = st["asks"]["mira-herald"]["thread_id"]
    b.script_reply(tid, "Yes - two indie features. Get your legals in order.")

    done = matchmaker._advance_asks(st, client, card, llm, [], 1_000_100.0)
    assert len(done) == 1
    assert st["asks"]["mira-herald"]["concluded"] is True

    report = done[0]["report"]
    for label in ("ANSWER:", "CONFIRMED:", "UNCERTAIN:", "USEFUL:",
                  "INTEREST:", "NEXT:"):
        assert label in report, "report missing " + label

    # It reaches the human as an answer, not a pitch.
    out = matchmaker._emit(st, done, 1_000_100.0)
    assert out != matchmaker.SILENT
    assert "You asked me to find out from @mira-herald" in out
    assert "indie features" in out


def test_an_answer_is_never_suppressed_by_the_interrupt_bar(monkeypatch):
    """The human ASKED. That is not an interruption to be rationed."""
    monkeypatch.setenv("HERMIES_INTERRUPT_THRESHOLD", "9.9")
    monkeypatch.setenv("HERMIES_QUIET_HOURS", "0-23")     # always "quiet"
    st = matchmaker.new_state()
    st["notify_log"] = [1_000_000, 1_000_050]             # battery fully charged
    payload = matchmaker._ask_payload(
        "mira-herald", {"question": "q?", "report": "ANSWER: yes",
                        "last_their_msg": "yes", "our_turns": 1}, 1_000_100.0)
    out = matchmaker._emit(st, [payload], 1_000_100.0)
    assert out != matchmaker.SILENT


def test_no_reply_reports_honestly_instead_of_hanging():
    b, client = _client()
    card, llm = _card(), AskLlm()
    st = matchmaker.new_state()
    matchmaker.start_ask(st, client, card, "ghost-herald", "are you there?",
                         [], llm, now=1_000_000.0)
    late = 1_000_000.0 + 10 * DAY                 # past the reply window
    done = matchmaker._advance_asks(st, client, card, llm, [], late)
    assert len(done) == 1
    assert "never answered" in done[0]["report"]
    assert st["asks"]["ghost-herald"]["concluded"] is True


def test_turn_budget_is_respected(monkeypatch):
    monkeypatch.setenv("HERMIES_ASK_MAX_TURNS", "1")
    b, client = _client()
    card, llm = _card(), AskLlm()
    st = matchmaker.new_state()
    matchmaker.start_ask(st, client, card, "mira-herald", "q?", [], llm,
                         now=1_000_000.0)
    tid = st["asks"]["mira-herald"]["thread_id"]
    b.script_reply(tid, "here is an answer")
    matchmaker._advance_asks(st, client, card, llm, [], 1_000_100.0)
    # 1 outbound turn spent -> conclude rather than keep talking
    assert st["asks"]["mira-herald"]["our_turns"] == 1
    assert st["asks"]["mira-herald"]["concluded"] is True


# --- follow-ups + status (step 8) -------------------------------------------
def test_follow_up_reuses_the_open_conversation():
    b, client = _client()
    card, llm = _card(), AskLlm()
    st = matchmaker.new_state()
    matchmaker.start_ask(st, client, card, "mira-herald", "first?", [], llm,
                         now=1_000_000.0)
    threads_before = len(b.list_threads()["threads"])
    res = matchmaker.start_ask(st, client, card, "mira-herald", "and second?",
                               [], llm, now=1_000_050.0)
    assert res["ok"] and res["status"] == "follow-up sent"
    assert len(b.list_threads()["threads"]) == threads_before   # same thread
    assert st["asks"]["mira-herald"]["our_turns"] == 2


def test_status_distinguishes_working_from_finished():
    b, client = _client()
    card, llm = _card(), AskLlm()
    st = matchmaker.new_state()
    matchmaker.start_ask(st, client, card, "mira-herald", "q?", [], llm,
                         now=1_000_000.0)
    assert "still working" in matchmaker.ask_status(st)
    tid = st["asks"]["mira-herald"]["thread_id"]
    b.script_reply(tid, "answered")
    matchmaker._advance_asks(st, client, card, llm, [], 1_000_100.0)
    assert "finished" in matchmaker.ask_status(st)


# --- the tool surface -------------------------------------------------------
def test_ask_tool_returns_immediately_and_tells_the_agent_not_to_invent():
    b, client = _client()
    handlers = {s["name"]: s["handler"]
                for s in tools.build(client, _card(), llm=AskLlm())}
    res = json.loads(handlers["hermies_ask"](
        {"to": "@mira-herald", "question": "have you handled distribution?"}))
    assert res["success"] is True
    assert "invent" in res["note"].lower()
    assert matchmaker.load_state()["asks"]["mira-herald"]["thread_id"]


def test_ask_preview_tool_sends_nothing():
    b, client = _client()
    handlers = {s["name"]: s["handler"]
                for s in tools.build(client, _card(), llm=AskLlm())}
    before = len(b.list_threads()["threads"])
    res = json.loads(handlers["hermies_ask_preview"](
        {"to": "mira-herald", "question": "q?"}))
    assert res["success"] is True and "nothing has been sent" in res["preview_text"]
    assert len(b.list_threads()["threads"]) == before
