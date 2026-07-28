"""The one-time first check-in.

A brand-new user cannot tell disciplined silence from a broken plugin. So about
a day after joining we prove we're alive — once, with REAL numbers, and honest
when the network is thin. It must never become a status feed.
"""
import pytest

from hermies import matchmaker, profile

HOUR = 3600
DAY = 86400
T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMIES_QUIET_HOURS", "")


def _card():
    return profile.PublicCard(handle="gus-herald", represents="an AI filmmaker")


def _started(t=T0):
    st = matchmaker.new_state()
    matchmaker._maybe_checkin(st, _card(), t)      # starts the clock
    assert st["network_since"] == int(t)
    return st


def test_nothing_on_the_first_cycle():
    st = matchmaker.new_state()
    assert matchmaker._maybe_checkin(st, _card(), T0) is None


def test_silent_before_the_window():
    st = _started()
    assert matchmaker._maybe_checkin(st, _card(), T0 + 23 * HOUR) is None


def test_fires_once_after_24h_then_never_again():
    st = _started()
    first = matchmaker._maybe_checkin(st, _card(), T0 + 25 * HOUR)
    assert first is not None and first["kind"] == "checkin"
    # never again, no matter how much later
    assert matchmaker._maybe_checkin(st, _card(), T0 + 30 * DAY) is None


def test_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HERMIES_CHECKIN_AFTER_HOURS", "0")
    st = _started()
    assert matchmaker._maybe_checkin(st, _card(), T0 + 10 * DAY) is None


def test_window_is_tunable_from_the_hub(monkeypatch):
    monkeypatch.setenv("HERMIES_CHECKIN_AFTER_HOURS", "2")
    st = _started()
    assert matchmaker._maybe_checkin(st, _card(), T0 + 3 * HOUR) is not None


# --- what it actually says --------------------------------------------------
def test_reports_real_work_when_there_was_some():
    st = _started()
    st["digs"] = {
        "mira-herald": {"our_turns": 3, "concluded": True,
                        "their_card": {"why": "an AI music-video artist"}},
        "zoe-worlds": {"our_turns": 2, "concluded": False,
                       "their_card": {"why": "a 3D environment artist"}},
    }
    st["seen"] = {"mira-herald": {}, "zoe-worlds": {}, "kip-studio": {}}
    st["outbox"]["ready"] = [{"id": "x", "handle": "kip-studio"}]

    it = matchmaker._maybe_checkin(st, _card(), T0 + 25 * HOUR)
    out = matchmaker._emit(st, [it], T0 + 25 * HOUR)

    assert "not a match" in out                 # never oversold as a finding
    assert "talked with 2" in out
    assert "mira-herald" in out and "music-video artist" in out
    assert "1 conversation(s) still going" in out
    assert "1 maybe(s)" in out                  # held findings acknowledged
    assert "Nothing needed from you" in out


def test_is_honest_when_the_network_is_thin():
    st = _started()
    it = matchmaker._maybe_checkin(st, _card(), T0 + 25 * HOUR)
    out = matchmaker._emit(st, [it], T0 + 25 * HOUR)
    assert "haven't found anyone worth talking to yet" in out
    assert "still small in your areas" in out
    assert "expected this early" in out          # reassurance, not an apology


def test_mentions_a_standing_intent_when_there_is_one():
    st = _started()
    it = matchmaker._maybe_checkin(st, _card(), T0 + 25 * HOUR)
    it["intents"] = ["a colourist who has worked on AI footage"]
    out = matchmaker._emit(st, [it], T0 + 25 * HOUR)
    assert "Still hunting" in out and "colourist" in out


def test_it_reaches_the_human_even_when_the_bar_is_high(monkeypatch):
    """Its whole purpose is to break silence — the interrupt bar must not
    swallow it."""
    monkeypatch.setenv("HERMIES_INTERRUPT_THRESHOLD", "9.9")
    monkeypatch.setenv("HERMIES_QUIET_HOURS", "0-23")
    st = _started()
    st["notify_log"] = [T0 + 24 * HOUR, T0 + 24.5 * HOUR]   # battery charged
    it = matchmaker._maybe_checkin(st, _card(), T0 + 25 * HOUR)
    assert matchmaker._emit(st, [it], T0 + 25 * HOUR) != matchmaker.SILENT


def test_paused_users_are_left_alone():
    """A paused human hears nothing — the delivery plane holds it rather than
    speaking (run_engine also returns before generating one)."""
    st = _started()
    it = matchmaker._maybe_checkin(st, _card(), T0 + 25 * HOUR)
    st["outbox"]["ready"] = [it]
    st["paused"] = True
    assert matchmaker.deliver_pending(st, T0 + 25 * HOUR) == matchmaker.SILENT
    assert len(st["outbox"]["ready"]) == 1          # held, not dropped
