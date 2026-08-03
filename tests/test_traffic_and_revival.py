"""The three production failures found on 2026-07-29, each pinned.

1. TRAFFIC — two agents were making ~48 req/min where ~3 was expected. The
   poller lease only guarded the daemon loop; register() ran in every spawned
   Hermes process and each one called the hub.
2. ZOMBIE THREADS — a dig whose opener failed to send left a 0-turn thread open
   on the hub forever, blocking that counterpart permanently.
3. SATURATION — once every pair had concluded one dig, discovery had nothing
   left to return and the network went silent for 42 hours.
"""
import pytest

from hermix import matchmaker, throttle, remote_config, service, profile

HOUR = 3600
DAY = 86400
T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    remote_config._reset_for_tests()
    yield
    remote_config._reset_for_tests()


# --------------------------------------------------------------------------- #
# 1. The cross-process gate
# --------------------------------------------------------------------------- #
def test_gate_admits_one_caller_per_window():
    assert throttle.due("startup", 300, now=T0) is True
    assert throttle.due("startup", 300, now=T0 + 1) is False
    assert throttle.due("startup", 300, now=T0 + 299) is False
    assert throttle.due("startup", 300, now=T0 + 301) is True


def test_gate_is_shared_across_processes_not_per_process():
    """The whole point: a *different* process must see the same gate. Nothing
    is kept in memory, so a fresh import cannot reopen it."""
    assert throttle.due("startup", 300, now=T0) is True
    import importlib
    importlib.reload(throttle)
    assert throttle.due("startup", 300, now=T0 + 5) is False


def test_gate_can_be_disabled():
    for i in range(3):
        assert throttle.due("startup", 0, now=T0 + i) is True


def test_gate_fails_open_when_it_cannot_write(monkeypatch):
    """A chatty agent beats a disconnected one — never block work on IO."""
    monkeypatch.setattr(throttle, "_gate_path",
                        lambda name: (_ for _ in ()).throw(OSError("no home")))
    assert throttle.due("startup", 300, now=T0) is True


def test_config_staleness_survives_a_process_restart():
    """_FETCHED_AT was a module global, so every new Hermes process refetched
    the config on startup. The clock now lives on disk."""
    calls = []

    class Hub:
        def get_config(self):
            calls.append(1)
            return {"knobs": {"interrupt_threshold": 5.0}}

    hub = Hub()
    assert remote_config.refresh(hub, now=T0) is True
    assert len(calls) == 1

    remote_config._reset_for_tests()          # simulate a brand-new process
    remote_config.refresh(hub, now=T0 + 60)
    assert len(calls) == 1, "a fresh process refetched an already-fresh config"


def test_only_one_service_thread_per_process():
    """The lease compares pids, so a second thread in the SAME process sails
    through it. Hermes calls register() more than once per gateway, so each
    call used to add another poller."""
    service._reset_for_tests()

    class NoClient:
        def __getattr__(self, k):
            raise AttributeError(k)

    card = profile.PublicCard(handle="gus-herald")
    a = service.start(NoClient(), card, lambda *a, **k: None, None, interval=9999)
    b = service.start(NoClient(), card, lambda *a, **k: None, None, interval=9999)
    c = service.start(NoClient(), card, lambda *a, **k: None, None, interval=9999)
    assert a is b is c, "register() spawned extra pollers"
    service._reset_for_tests()


def test_the_lease_still_blocks_a_second_process(monkeypatch):
    """Per-process guard must not weaken the cross-process lease."""
    assert service._claim_poller_lock(now=T0) is True
    monkeypatch.setattr(service.os, "getpid", lambda: 999999)
    assert service._claim_poller_lock(now=T0 + 5) is False
    assert service._claim_poller_lock(now=T0 + service.LEASE_SECONDS + 5) is True


# --------------------------------------------------------------------------- #
# 2. Zombie threads
# --------------------------------------------------------------------------- #
class _Hub:
    """Minimal thread hub whose send can be made to fail."""

    def __init__(self, send_ok=True):
        self.send_ok = send_ok
        self.opened, self.closed, self.sent = [], [], []
        self.messages = {}

    def open_thread(self, to, kind, subject):
        tid = f"thr-{len(self.opened)}"
        self.opened.append((to, tid))
        self.messages[tid] = []
        return {"thread_id": tid}

    def send_thread(self, tid, text):
        if not self.send_ok:
            return {"error": "hub refused", "status": 409}
        self.sent.append((tid, text))
        self.messages.setdefault(tid, []).append({"from": "me", "text": text})
        return {"ok": True, "turn": len(self.messages[tid])}

    def close_thread(self, tid):
        self.closed.append(tid)
        return {"ok": True}

    def read_thread(self, tid):
        return {"messages": self.messages.get(tid, [])}

    def list_threads(self):
        return {"threads": []}


def _card():
    from hermix import profile
    return profile.PublicCard(handle="gus-herald", represents="an AI filmmaker",
                              offer=["ai video"], need=["a composer"])


def _sig(agent="mira-herald"):
    return {"kind": "match", "agent": agent, "why": "an AI music-video artist",
            "score": 8.0}


def _llm(system, user, *, purpose=None):
    return "Hello from the opener."


def test_failed_opener_does_not_leave_a_thread_open():
    st = matchmaker.new_state()
    hub = _Hub(send_ok=False)
    matchmaker._open_dig(st, hub, _card(), _sig(), "mira-herald", "h1", _llm, [], T0)

    assert hub.closed == ["thr-0"], "the empty thread was left on the hub"
    assert "mira-herald" not in st["digs"], "we recorded a conversation we never had"
    assert any(e.get("verdict") == "dig_opener_failed" for e in st["log"])


def test_successful_opener_still_records_the_dig():
    st = matchmaker.new_state()
    hub = _Hub(send_ok=True)
    matchmaker._open_dig(st, hub, _card(), _sig(), "mira-herald", "h1", _llm, [], T0)
    dig = st["digs"]["mira-herald"]
    assert dig["our_turns"] == 1 and dig["awaiting"] is True
    assert hub.closed == []


def test_an_existing_zombie_is_revived_then_abandoned():
    """Threads already stuck in production have no messages at all. We re-send,
    and if that keeps failing we free the counterpart instead of waiting."""
    st = matchmaker.new_state()
    hub = _Hub(send_ok=True)
    st["digs"]["mira-herald"] = {
        "thread_id": "thr-0", "our_turns": 1, "awaiting": True,
        "concluded": False, "opened_at": T0, "their_card": {"why": "artist"},
    }
    hub.messages["thr-0"] = []                       # the zombie: zero turns

    matchmaker._advance_dig(st, hub, _card(), "mira-herald",
                            st["digs"]["mira-herald"], _llm, [], T0 + HOUR)
    assert hub.sent, "the lost opener was never re-sent"
    assert any(e.get("verdict") == "dig_opener_resent" for e in st["log"])

    # Now one that can never send: two attempts, then the candidate is freed.
    st2 = matchmaker.new_state()
    dead = _Hub(send_ok=False)
    dead.messages["thr-0"] = []
    st2["digs"]["mira-herald"] = {
        "thread_id": "thr-0", "our_turns": 1, "awaiting": True,
        "concluded": False, "opened_at": T0, "their_card": {"why": "artist"},
    }
    for i in range(3):
        dig = st2["digs"].get("mira-herald")
        if dig is None:
            break
        matchmaker._advance_dig(st2, dead, _card(), "mira-herald", dig,
                                _llm, [], T0 + (i + 1) * HOUR)
    assert "mira-herald" not in st2["digs"], "the candidate stayed blocked forever"
    assert dead.closed == ["thr-0"]
    assert any(e.get("verdict") == "dig_abandoned" for e in st2["log"])


# --------------------------------------------------------------------------- #
# 3. Saturation — the re-look
# --------------------------------------------------------------------------- #
def _concluded(st, cand="mira-herald", ts=T0, redigs=0):
    st["digs"][cand] = {"thread_id": "thr-0", "concluded": True,
                        "concluded_ts": ts, "our_turns": 3, "redigs": redigs}


def test_no_relook_before_the_cooldown():
    st = matchmaker.new_state()
    _concluded(st)
    assert matchmaker._redig_due(st, "mira-herald", T0 + 13 * DAY) is False


def test_relook_after_the_cooldown():
    st = matchmaker.new_state()
    _concluded(st)
    assert matchmaker._redig_due(st, "mira-herald", T0 + 15 * DAY) is True


def test_relook_is_capped_so_it_never_becomes_pestering():
    st = matchmaker.new_state()
    _concluded(st, redigs=3)
    assert matchmaker._redig_due(st, "mira-herald", T0 + 90 * DAY) is False


def test_a_rejected_agent_is_never_revisited():
    for verdict in ("never", "drop"):
        st = matchmaker.new_state()
        _concluded(st)
        st["seen"]["mira-herald"] = {"verdict": verdict, "ts": T0}
        assert matchmaker._redig_due(st, "mira-herald", T0 + 90 * DAY) is False, verdict


def test_relook_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HERMIX_REDIG_AFTER_DAYS", "0")
    st = matchmaker.new_state()
    _concluded(st)
    assert matchmaker._redig_due(st, "mira-herald", T0 + 900 * DAY) is False


def test_an_open_dig_is_never_treated_as_due():
    st = matchmaker.new_state()
    st["digs"]["mira-herald"] = {"thread_id": "t", "concluded": False,
                                 "opened_at": T0}
    assert matchmaker._redig_due(st, "mira-herald", T0 + 90 * DAY) is False


def test_redig_opens_a_new_thread_and_carries_the_count():
    st = matchmaker.new_state()
    _concluded(st, ts=T0)
    hub = _Hub(send_ok=True)
    matchmaker._redig(st, hub, _card(), _sig(), "mira-herald", "h2",
                      _llm, [], T0 + 20 * DAY)
    dig = st["digs"]["mira-herald"]
    assert dig["concluded"] is False and dig["redigs"] == 1
    assert hub.sent, "the re-look opened a thread but never spoke"


def test_redig_keeps_the_old_dig_when_opening_fails():
    """A failed re-look must not erase the history that gates the next one."""
    class NoOpen(_Hub):
        def open_thread(self, to, kind, subject):
            return {"error": "hub down"}

    st = matchmaker.new_state()
    _concluded(st, ts=T0, redigs=1)
    matchmaker._redig(st, NoOpen(), _card(), _sig(), "mira-herald", "h2",
                      _llm, [], T0 + 20 * DAY)
    assert st["digs"]["mira-herald"]["concluded"] is True
    assert st["digs"]["mira-herald"]["redigs"] == 1


def test_a_dig_concludes_before_the_hub_budget_kills_it():
    """14 of 24 production threads expired at the hub's 12-message limit: a
    full budget of inference spent, findings note written only by the expiry
    path. Conclude on our own terms instead."""
    st = matchmaker.new_state()
    hub = _Hub(send_ok=True)
    hub.messages["thr-0"] = ([{"from": "me", "text": "ours"},
                              {"from": "mira-herald", "text": "theirs"}] * 5)
    st["digs"]["mira-herald"] = {
        "thread_id": "thr-0", "our_turns": 1, "awaiting": False,
        "concluded": False, "opened_at": T0, "their_card": {"why": "artist"},
    }
    matchmaker._advance_dig(st, hub, _card(), "mira-herald",
                            st["digs"]["mira-herald"], _llm, [], T0 + HOUR)
    assert st["digs"]["mira-herald"]["concluded"] is True
    assert st["findings"].get("mira-herald"), "no findings note was written"
    assert not hub.sent, "we spent another turn instead of concluding"


def test_one_thread_listing_per_cycle_not_one_per_dig():
    """At 20 active digs the old code made 20 full listings per cycle — enough
    for an agent to trip the hub's own 60-req/min limit and stall itself."""
    calls = {"list": 0}

    class Counting(_Hub):
        def list_threads(self):
            calls["list"] += 1
            return {"threads": []}

        def list_signals(self, handle):
            return []

        def discover(self, card):
            return []

    hub = Counting()
    st = matchmaker.new_state()
    for i in range(20):
        hub.messages[f"t{i}"] = [{"from": "mira", "text": "hi"}]
        st["digs"][f"agent-{i}"] = {
            "thread_id": f"t{i}", "our_turns": 1, "awaiting": False,
            "concluded": False, "opened_at": T0, "their_card": {"why": "x"},
        }
    matchmaker._run_threads_path(st, hub, _card(), _llm, T0 + HOUR, [], [])
    assert calls["list"] <= 2, f"{calls['list']} listings for 20 digs"


def test_new_digs_are_rationed_per_cycle():
    """Launch day: everyone is new to everyone. Opening every candidate at once
    burns the per-agent rate limit and a day's inference in one wave."""
    class Wide(_Hub):
        def list_signals(self, handle):
            return [{"kind": "match", "agent": f"agent-{i}", "score": 8.0,
                     "why": "an AI music-video artist"} for i in range(20)]

        def discover(self, card):
            return []

    st = matchmaker.new_state()
    hub = Wide()
    matchmaker._run_threads_path(st, hub, _card(), _llm, T0, [], [])
    assert len(st["digs"]) == 4, f"opened {len(st['digs'])} digs in one cycle"
    # ...and the rest are picked up on later cycles, not dropped.
    matchmaker._run_threads_path(st, hub, _card(), _llm, T0 + 4 * HOUR, [], [])
    assert len(st["digs"]) == 8
