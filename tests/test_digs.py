"""The dig-through-threads integration: the matchmaker now qualifies matches by
holding a REAL agent-to-agent conversation over a kind="dig" thread, concludes
each with a findings note, and judges on that note. The answering side (the
service daemon) drains inbound threads as the public envoy, caps its replies,
and NEVER auto-answers a reveal request. These tests pin all of that plus the
standing-intent discovery path and the deliver-on-next-interaction queue tool.
"""
import json

import pytest

from hermix import matchmaker, service, profile, tools
from hermix.client import HermixClient
from hermix.mock_backend import MockBackend

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


# The judge no longer authors prose. It returns claims that cite numbered
# transcript turns, and anything citing a turn that does not exist is dropped —
# which downgrades notify to watch. A fake emitting the old {verdict, pitch}
# shape is therefore correctly refused, so the fake speaks the real contract.
STRUCTURED_NOTIFY = """{"verdict": "notify", "user_relevance": {"text": "their unity tooling covers the gap in your pipeline", "source_ids": ["card:ours"]}, "claims": [{"text": "they are open to collaborating", "evidence_state": "counterpart_claim", "source_ids": ["turn:2"]}], "uncertainties": ["budget was not discussed"], "next_action_ids": ["ask_budget"], "reason": "offers meet needs"}"""


class DigLlm:
    """Routes on the system prompt across every dig call site: card refresh,
    envoy opener/turn, findings-note writer, and the findings judge."""
    def __init__(self,
                 verdict=STRUCTURED_NOTIFY,
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
        if system.startswith("You are a connection analyst"):
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
    return b, HermixClient(b)


# --------------------------------------------------------------------------- #
# Dig state machine: open -> converse -> conclude (findings) -> judge notify
# --------------------------------------------------------------------------- #
def test_dig_opens_thread_converses_concludes_with_findings_and_notifies(monkeypatch):
    monkeypatch.setenv("HERMIX_MIN_SCORE", "1")
    monkeypatch.setenv("HERMIX_DIG_MAX_TURNS", "3")
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

    # The compiler writes this now, so assert the PROPERTIES rather than the
    # sentence: grounded relevance, attributed claim, stated uncertainty, and
    # one real next step. The exact evidence quote lives in the receipt.
    assert out != matchmaker.SILENT
    assert "unity tooling covers the gap in your pipeline" in out
    assert "agent said they are open to collaborating" in out,         "a counterpart claim was stated as fact"
    assert "budget" in out                                    # uncertainty kept
    assert out.rstrip().endswith("?")                         # one real next step

    # The judge ran on the FINDINGS NOTE, not the raw reply.
    judged = [u for (s, u) in llm.calls
              if s.startswith("You are a connection analyst")]
    assert judged and "FINDINGS NOTE:" in judged[-1]


def test_dig_concludes_on_card_timeout_with_no_reply(monkeypatch):
    monkeypatch.setenv("HERMIX_MIN_SCORE", "1")
    monkeypatch.setenv("HERMIX_HANDSHAKE_TIMEOUT_DAYS", "4")
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
    # Nobody replied, so a verdict resting on a conversation turn cites a turn
    # that does not exist. That claim is refused and notify degrades to watch —
    # which is the whole point: we no longer interrupt on evidence we just threw
    # away. A cards-only finding must earn its way out on card sources alone.
    assert state["seen"]["mira-herald"]["verdict"] == "watch"
    # ...but nobody ever replied, so it is held for the next conversation
    # rather than interrupting the human (see _value_of: cards_only).
    assert out == matchmaker.SILENT
    # "watch" holds it for re-judging rather than queueing it for delivery —
    # nothing was established, so there is nothing to deliver yet.
    assert not any(i["handle"] == "mira-herald" for i in state["queue"])


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
    monkeypatch.setenv("HERMIX_ENVOY_MAX_REPLIES", "6")
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
    monkeypatch.setenv("HERMIX_MIN_SCORE", "1")
    monkeypatch.setenv("HERMIX_DIG_MAX_TURNS", "1")   # conclude fast
    b, client = _fresh_client()
    card, llm = _card(), DigLlm(
        verdict=STRUCTURED_NOTIFY)
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
    assert 'You asked me to look for "unity tooling"' in out       # intent-led
    # The handle deliberately stays OUT of the prose — it is machinery, and the
    # review flagged handles as agent-centric rather than human-centric. It is
    # still carried on the item so /hermix why and /hermix intro keep working.
    assert "kip-herald" not in out
    assert "kip-herald" in state["findings"]
    assert state["digs"]["kip-herald"]["intent"] == "unity tooling"


# --------------------------------------------------------------------------- #
# Deliver-on-next-interaction queue tool: peek (non-destructive) then pop
# --------------------------------------------------------------------------- #
def test_pending_tool_peek_and_pop(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
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

    h = _handlers(HermixClient(MockBackend()))

    peek = json.loads(h["hermix_pending"]({"action": "peek"}))
    assert len(peek["queued"]) == 2
    assert "a-herald" in peek["text"]
    assert 'You asked me to find "a cofounder"' in peek["text"]   # intent lead
    assert len(peek["pending_reveals"]) == 1
    # peek does not consume
    assert len(matchmaker.load_state()["queue"]) == 2

    pop = json.loads(h["hermix_pending"]({"action": "pop"}))
    assert len(pop["delivered"]) == 2
    assert matchmaker.load_state()["queue"] == []
    # reveals are NOT consumed by pop (they need the human's explicit action)
    assert len(pop["pending_reveals"]) == 1


# --------------------------------------------------------------------------- #
# Execution plane vs delivery plane
#
# The rule: cron failing may DELAY a notification, but must never stop agents
# thinking/matching/conversing — and a delivery that never lands must never
# silently consume the finding.
# --------------------------------------------------------------------------- #
def test_engine_works_and_delivers_nothing(monkeypatch):
    """run_engine opens digs and fills the outbox WITHOUT notifying anyone."""
    monkeypatch.setenv("HERMIX_MIN_SCORE", "1")
    b, client = _fresh_client()
    card, llm = _card(), DigLlm()
    client.publish_profile(card.public_dict())
    state = matchmaker.new_state()
    clock = Clock()

    matchmaker.run_engine(state, client, card, llm, clock)      # opens digs
    assert state["digs"], "engine must open digs"
    assert state["engine"]["last_success_at"] is not None       # heartbeat written
    assert state["engine"]["cycles_total"] == 1
    assert state["engine"]["last_digs_opened"] >= 1
    assert state["outbox"]["ready"] == []                       # nothing to say yet

    # Converse until the dig concludes (3 outbound turns), engine-only.
    tid = state["digs"]["mira-herald"]["thread_id"]
    for msg in ("Yes, I need AI-film collabs — timeline?",
                "Next month works.",
                "Let's co-produce the pilot together."):
        b.script_reply(tid, msg)
        clock.advance(3600)
        matchmaker.run_engine(state, client, card, llm, clock)

    assert state["digs"]["mira-herald"]["concluded"] is True
    assert state["outbox"]["ready"], "a concluded dig must land in the outbox"
    # ...and the engine never interrupted anyone doing it.
    assert state["notify_log"] == []
    assert state["outbox"]["inflight"] == []


def test_delivery_is_durable_when_nothing_lands(monkeypatch):
    """A finding claimed for delivery is NOT deleted; if it is never confirmed
    it comes back rather than being lost."""
    monkeypatch.setenv("HERMIX_QUIET_HOURS", "")
    st = matchmaker.new_state()
    st["outbox"]["ready"] = [{
        "id": "f1", "handle": "mira-herald", "represents": "artist",
        "pitch": "real fit", "reason": "verified", "evidence": "keen",
        "next_step": "reach out", "score": 9, "note": "", "verified": True,
        "cards_only": False, "intent": None,
    }]
    t = 1_000_000.0
    text = matchmaker.deliver_pending(st, t)
    assert text != matchmaker.SILENT
    assert st["outbox"]["ready"] == []                  # claimed...
    assert [i["id"] for i in st["outbox"]["inflight"]] == ["f1"]   # ...not deleted

    # never acknowledged -> after the expiry it is offered again
    later = t + matchmaker.INFLIGHT_EXPIRY_SECONDS + 60
    again = matchmaker.deliver_pending(st, later)
    assert again != matchmaker.SILENT and "mira-herald" in again

    # once acknowledged it stops coming back
    matchmaker.ack_delivered(st, now=later)
    assert st["outbox"]["inflight"] == []
    assert matchmaker.deliver_pending(st, later + 10) == matchmaker.SILENT


def test_delivery_holds_subbar_findings_without_consuming_them(monkeypatch):
    """Below the interrupt bar: stays in ready, and no interruption is recorded."""
    monkeypatch.setenv("HERMIX_QUIET_HOURS", "")
    monkeypatch.setenv("HERMIX_INTERRUPT_THRESHOLD", "9.5")
    st = matchmaker.new_state()
    st["outbox"]["ready"] = [{
        "id": "f2", "handle": "weak-herald", "represents": "x", "pitch": "meh",
        "reason": "", "evidence": "", "next_step": "", "score": 4,
        "note": "", "verified": False, "cards_only": False, "intent": None,
    }]
    assert matchmaker.deliver_pending(st, 1_000_000.0) == matchmaker.SILENT
    assert [i["id"] for i in st["outbox"]["ready"]] == ["f2"]   # still there
    assert st["outbox"]["inflight"] == []
    assert st["notify_log"] == []            # no interruption was "spent"


def test_cron_prompt_is_delivery_only():
    """Cron must not be able to trigger discovery/digs/judging."""
    assert "hermix_deliver_pending" in matchmaker.CRON_PROMPT
    assert "hermix_scout" not in matchmaker.CRON_PROMPT


# --------------------------------------------------------------------------- #
# Production bugs found by watching the live network (2026-07-27)
# --------------------------------------------------------------------------- #
def test_envoy_never_answers_a_dig_we_opened(monkeypatch):
    """BUG A: the envoy drain replied to threads the MATCHMAKER opened, so the
    agent was both asking and answering — four live threads hit the hub's
    12-message ceiling and doubled the inference bill."""
    monkeypatch.setenv("HERMIX_MIN_SCORE", "1")
    b, client = _fresh_client()
    card, llm = _card(), DigLlm()
    client.publish_profile(card.public_dict())
    state = matchmaker.new_state()
    clock = Clock()

    matchmaker.run_engine(state, client, card, llm, clock)     # we open digs
    tid = state["digs"]["mira-herald"]["thread_id"]
    b.script_reply(tid, "sure, tell me more")                  # unread for us

    before = len(b.read_thread(tid)["messages"])
    summary = service.drain_threads(client, card, llm, state)
    after = len(b.read_thread(tid)["messages"])

    assert summary["answered"] == 0, "envoy must not answer our own dig"
    assert after == before, "no extra message may be posted by the envoy"


def test_envoy_still_answers_threads_others_opened():
    """The guard must not silence the envoy on genuine inbound digs."""
    card, llm = _card(), DigLlm()
    ft = FakeThreadClient()
    tid = ft.open_thread("someone-else", "dig", "hello")["thread_id"]
    ft.add_counterpart(tid, "are our humans a fit?", frm="someone-else")
    state = matchmaker.new_state()                    # no digs of our own
    summary = service.drain_threads(ft, card, llm, state)
    assert summary["answered"] == 1


def test_no_duplicate_dig_when_the_hub_already_has_one(monkeypatch):
    """BUG B: local state loss (or a pre-lease race) produced THREE threads with
    the same agent in two hours. The hub is asked first now."""
    monkeypatch.setenv("HERMIX_MIN_SCORE", "1")
    b, client = _fresh_client()
    card, llm = _card(), DigLlm()
    client.publish_profile(card.public_dict())
    clock = Clock()

    state = matchmaker.new_state()
    matchmaker.run_engine(state, client, card, llm, clock)
    threads_after_first = len(b.list_threads()["threads"])
    assert threads_after_first > 0

    # Simulate losing local state entirely (crash, wiped file, other process).
    fresh = matchmaker.new_state()
    matchmaker.run_engine(fresh, client, card, llm, clock)

    # The invariant is one thread PER COUNTERPART, not a frozen thread count:
    # per-cycle dig rationing means a later cycle legitimately reaches a
    # candidate the first one didn't get to. Counting threads would conflate
    # "opened a duplicate" with "made normal progress".
    peers = [t["with"] for t in b.list_threads()["threads"]]
    assert len(peers) == len(set(peers)), f"duplicate thread per agent: {peers}"
    assert set(peers) >= {"mira-herald"}
    assert fresh["digs"]["mira-herald"].get("adopted") is True
