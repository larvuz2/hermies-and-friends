"""The dig-through-threads integration: the matchmaker now qualifies matches by
holding a REAL agent-to-agent conversation over a kind="dig" thread, concludes
each with a findings note, and judges on that note. The answering side (the
service daemon) drains inbound threads as the public envoy, caps its replies,
and NEVER auto-answers a reveal request. These tests pin all of that plus the
standing-intent discovery path and the deliver-on-next-interaction queue tool.
"""
import json

import pytest

from hermies import matchmaker, service, profile, tools
from hermies.client import HermiesClient
from hermies.mock_backend import MockBackend

DAY = 86400


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class DigLlm:
    """Routes on the system prompt across every dig call site: card refresh,
    envoy opener/turn, findings-note writer, and the findings judge."""
    def __init__(self,
                 verdict='{"verdict": "notify", "pitch": "Strong AI-film fit.", "reason": "offers match needs"}',
                 note=("mira-herald represents an AI music-video artist.\n"
                       "Offers: visualizers (verified in chat).\n"
                       "Needs: AI-film collaborators (claimed).\n"
                       "Mutual benefit: co-produce a pilot.\n"
                       "Next step: propose an intro.\nNo red flags.")):
        self.verdict = verdict
        self.note = note
        self.calls = []

    def __call__(self, system, user, *, purpose=None):
        self.calls.append((system, user))
        if system.startswith("You refine"):
            return "{}"
        if system.startswith("You are writing a FINDINGS NOTE"):
            return self.note
        if system.startswith("You are a matchmaking analyst"):
            return self.verdict
        # envoy dig opener / turn
        return "Tell me about your current projects and timing — is there a fit?"


class FakeThreadClient:
    """A minimal, budget-free thread backend so the envoy-drain reply cap can be
    isolated from the hub's 12-message limit. Our sends carry from='me'."""
    def __init__(self):
        self.threads = {}
        self.seq = 0

    def open_thread(self, to, kind, subject):
        self.seq += 1
        tid = f"t{self.seq}"
        self.threads[tid] = {"thread_id": tid, "with": to, "kind": kind,
                             "subject": subject, "state": "open", "messages": []}
        return {"thread_id": tid}

    def add_counterpart(self, tid, text, frm):
        self.threads[tid]["messages"].append({"from": frm, "text": text, "read": False})

    def list_threads(self):
        out = []
        for t in self.threads.values():
            unread = sum(1 for m in t["messages"]
                         if m["from"] != "me" and not m.get("read"))
            out.append({"thread_id": t["thread_id"], "with": t["with"],
                        "kind": t["kind"], "subject": t["subject"],
                        "state": t["state"], "turns": len(t["messages"]),
                        "unread": unread})
        return {"threads": out}

    def read_thread(self, tid):
        for m in self.threads[tid]["messages"]:
            if m["from"] != "me":
                m["read"] = True
        return {"messages": [{"from": m["from"], "text": m["text"]}
                             for m in self.threads[tid]["messages"]]}

    def send_thread(self, tid, text):
        self.threads[tid]["messages"].append({"from": "me", "text": text})
        return {"ok": True, "turn": len(self.threads[tid]["messages"])}

    def close_thread(self, tid):
        self.threads[tid]["state"] = "concluded"
        return {"ok": True}


def _card():
    return profile.PublicCard(
        handle="gus-herald",
        represents="a creative technologist in AI film",
        offer=["ai video", "3d worlds"],
        need=["collaborators", "distribution"],
        guilds=["ai-video", "agents"],
    )


def _handlers(client, llm=None):
    return {s["name"]: s["handler"] for s in tools.build(client, _card(), llm=llm)}


def _fresh_client():
    b = MockBackend()
    b._inbox = []                      # start with a clean mailbox
    return b, HermiesClient(b)


# --------------------------------------------------------------------------- #
# Dig state machine: open -> converse -> conclude (findings) -> judge notify
# --------------------------------------------------------------------------- #
def test_dig_opens_thread_converses_concludes_with_findings_and_notifies(monkeypatch):
    monkeypatch.setenv("HERMIES_MIN_SCORE", "1")
    monkeypatch.setenv("HERMIES_DIG_MAX_TURNS", "3")
    b, client = _fresh_client()
    card, llm = _card(), DigLlm()
    client.publish_profile(card.public_dict())
    state = matchmaker.new_state()
    clock = Clock()

    # Cycle 1 opens a dig thread with each matched agent — silence, no reply yet.
    out = matchmaker.run_cycle(state, client, card, llm, clock)
    assert out == matchmaker.SILENT
    dig = state["digs"]["mira-herald"]
    assert dig["our_turns"] == 1 and dig["concluded"] is False
    tid = dig["thread_id"]
    assert b.list_threads()["threads"]      # a real thread exists on the hub

    # Counterpart answers; we take our next turns across cycles (cap = 3 out).
    b.script_reply(tid, "Yes, I need AI-film collabs — what's your timeline?")
    clock.advance(3600)
    matchmaker.run_cycle(state, client, card, llm, clock)
    assert state["digs"]["mira-herald"]["our_turns"] == 2

    b.script_reply(tid, "Great, next month works.")
    clock.advance(3600)
    matchmaker.run_cycle(state, client, card, llm, clock)
    assert state["digs"]["mira-herald"]["our_turns"] == 3

    # Their final reply; now we've spent our 3 outbound turns -> conclude + judge.
    b.script_reply(tid, "Let's co-produce the pilot together.")
    clock.advance(3600)
    out = matchmaker.run_cycle(state, client, card, llm, clock)

    assert state["digs"]["mira-herald"]["concluded"] is True
    assert "mira-herald" in state["findings"]
    assert state["findings"]["mira-herald"]["note"]           # a note was written
    assert state["findings"]["mira-herald"]["verdict"] == "notify"
    assert state["seen"]["mira-herald"]["verdict"] == "notify"

    # The notification carries the handle, the pitch, and a real quote from chat.
    assert out != matchmaker.SILENT
    assert "mira-herald" in out
    assert "Strong AI-film fit." in out
    assert "co-produce the pilot" in out                      # evidence = their words

    # The judge ran on the FINDINGS NOTE, not the raw reply.
    judged = [u for (s, u) in llm.calls
              if s.startswith("You are a matchmaking analyst")]
    assert judged and "FINDINGS NOTE:" in judged[-1]


def test_dig_concludes_on_card_timeout_with_no_reply(monkeypatch):
    monkeypatch.setenv("HERMIES_MIN_SCORE", "1")
    monkeypatch.setenv("HERMIES_HANDSHAKE_TIMEOUT_DAYS", "4")
    b, client = _fresh_client()
    card, llm = _card(), DigLlm()
    client.publish_profile(card.public_dict())
    state = matchmaker.new_state()
    clock = Clock()

    matchmaker.run_cycle(state, client, card, llm, clock)     # opens digs, awaiting
    assert not state["digs"]["mira-herald"]["concluded"]
    clock.advance(5 * DAY)                                     # blow the window
    out = matchmaker.run_cycle(state, client, card, llm, clock)
    assert state["digs"]["mira-herald"]["concluded"] is True  # concluded on cards
    assert "mira-herald" in state["findings"]
    assert state["seen"]["mira-herald"]["verdict"] == "notify"   # judged
    # ...but nobody ever replied, so it is held for the next conversation
    # rather than interrupting the human (see _value_of: cards_only).
    assert out == matchmaker.SILENT
    assert any(i["handle"] == "mira-herald" for i in state["queue"])


# --------------------------------------------------------------------------- #
# Envoy drain (the answering side): reply, cap at 6, then conclude + close
# --------------------------------------------------------------------------- #
def test_envoy_drain_answers_thread_via_membrane():
    fc = FakeThreadClient()
    tid = fc.open_thread("mira-herald", "dig", "fit")["thread_id"]
    captured = {}

    def spy_llm(system, user):
        captured["system"], captured["user"] = system, user
        return "Happy to explore — what does your human need most right now?"

    fc.add_counterpart(tid, "Hi — what does your human offer?", "mira-herald")
    state = matchmaker.new_state()
    res = service.drain_threads(fc, _card(), spy_llm, state, ring1=["6 years in game audio"])

    assert res["answered"] == 1
    assert fc.threads[tid]["messages"][-1]["from"] == "me"
    # Membrane held: the envoy prompt is card + ring1 only, dig mode.
    assert "PUBLIC envoy" in captured["system"]
    assert "MODE — DIG" in captured["system"]
    assert "game audio" in captured["system"]


def test_envoy_drain_caps_replies_at_six_then_closes(monkeypatch):
    monkeypatch.setenv("HERMIES_ENVOY_MAX_REPLIES", "6")
    fc = FakeThreadClient()
    tid = fc.open_thread("mira-herald", "dig", "fit")["thread_id"]
    state = matchmaker.new_state()
    llm = lambda s, u, **_: "envoy answer"

    total_answered = 0
    for i in range(9):
        fc.add_counterpart(tid, f"question {i}", "mira-herald")
        total_answered += service.drain_threads(fc, _card(), llm, state)["answered"]

    assert total_answered == 6                       # never more than the cap
    assert fc.threads[tid]["state"] == "concluded"   # closed after the cap
    ours = [m for m in fc.threads[tid]["messages"] if m["from"] == "me"]
    assert len(ours) == 7                            # 6 replies + 1 polite closer


# --------------------------------------------------------------------------- #
# Reveal requests are NEVER auto-answered — queued for the human instead
# --------------------------------------------------------------------------- #
def test_reveal_request_is_queued_never_auto_answered():
    fc = FakeThreadClient()
    tid = fc.open_thread("mira-herald", "reveal_request", "connect")["thread_id"]
    payload = json.dumps({"reveal_request": True, "context": "let's connect on X",
                          "card": {"handle": "mira-herald"}})
    fc.add_counterpart(tid, payload, "mira-herald")
    state = matchmaker.new_state()

    def boom_llm(system, user):
        raise AssertionError("the envoy must never generate a reveal reply")

    res = service.drain_threads(fc, _card(), boom_llm, state)
    assert res["answered"] == 0 and res["reveals_queued"] == 1
    assert all(m["from"] != "me" for m in fc.threads[tid]["messages"])  # no reply
    pend = state["pending_reveals"]
    assert len(pend) == 1
    assert pend[0]["thread_id"] == tid
    assert pend[0]["handle"] == "mira-herald"
    assert "connect on X" in pend[0]["context"]

    # Draining again neither double-queues nor answers.
    res2 = service.drain_threads(fc, _card(), boom_llm, state)
    assert res2["reveals_queued"] == 0
    assert len(state["pending_reveals"]) == 1


# --------------------------------------------------------------------------- #
# Standing intents drive discovery + label the notification
# --------------------------------------------------------------------------- #
def test_standing_intent_discovers_and_labels_notification(monkeypatch):
    monkeypatch.setenv("HERMIES_MIN_SCORE", "1")
    monkeypatch.setenv("HERMIES_DIG_MAX_TURNS", "1")   # conclude fast
    b, client = _fresh_client()
    card, llm = _card(), DigLlm(
        verdict='{"verdict": "notify", "pitch": "Found your unity tooling.", "reason": "match"}')
    client.publish_profile(card.public_dict())
    state = matchmaker.new_state()
    clock = Clock()
    intents = [{"id": 1, "text": "unity tooling", "status": "active"}]

    # kip-herald advertises "unity tooling" -> intent discovery tags it.
    matchmaker.run_cycle(state, client, card, llm, clock, intents=intents)
    assert state["digs"]["kip-herald"]["intent"] == "unity tooling"
    tid = state["digs"]["kip-herald"]["thread_id"]

    # Drive kip's dig to conclusion (max 1 outbound turn -> concludes next turn).
    b.script_reply(tid, "Yes, I maintain the unity tooling you need.")
    clock.advance(3600)
    out = matchmaker.run_cycle(state, client, card, llm, clock, intents=intents)

    assert state["findings"]["kip-herald"]["verdict"] == "notify"
    assert out != matchmaker.SILENT
    assert 'You asked me to find "unity tooling"' in out       # intent-led
    assert "kip-herald" in out


# --------------------------------------------------------------------------- #
# Deliver-on-next-interaction queue tool: peek (non-destructive) then pop
# --------------------------------------------------------------------------- #
def test_pending_tool_peek_and_pop(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMIES_HOME", str(tmp_path))
    state = matchmaker.new_state()
    state["queue"] = [
        {"handle": "a-herald", "represents": "an artist", "pitch": "great fit",
         "evidence": "hi", "next_step": "reach out", "intent": None},
        {"handle": "b-herald", "represents": "a dev", "pitch": "good fit",
         "evidence": "yo", "next_step": "reach out", "intent": "a cofounder"},
    ]
    state["pending_reveals"] = [{"thread_id": "t1", "handle": "c-herald",
                                 "context": "connect", "ts": 1}]
    matchmaker.save_state(state)

    h = _handlers(HermiesClient(MockBackend()))

    peek = json.loads(h["hermies_pending"]({"action": "peek"}))
    assert len(peek["queued"]) == 2
    assert "a-herald" in peek["text"]
    assert 'You asked me to find "a cofounder"' in peek["text"]   # intent lead
    assert len(peek["pending_reveals"]) == 1
    # peek does not consume
    assert len(matchmaker.load_state()["queue"]) == 2

    pop = json.loads(h["hermies_pending"]({"action": "pop"}))
    assert len(pop["delivered"]) == 2
    assert matchmaker.load_state()["queue"] == []
    # reveals are NOT consumed by pop (they need the human's explicit action)
    assert len(pop["pending_reveals"]) == 1
