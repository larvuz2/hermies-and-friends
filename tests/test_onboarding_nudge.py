"""Tests for the first-run onboarding nudge (commands.onboarding_nudge).

The nudge is a ``pre_llm_call`` hook — the gateway-safe replacement for the
``inject_message`` bootstrap, which is a no-op in gateway mode (Telegram/Discord/
etc.). It steers the agent to run onboarding on the human's first message, then
gets out of the way: silent once onboarded, silent when the human opted out
(paused), and throttled to at most once per hour.

The pre_llm_call return contract is verified verbatim against the real Hermes
source (see docs/HERMES-API-GROUND-TRUTH.md §4): a callback returns
``{"context": <str>}`` (or a plain string), which Hermes appends to the current
turn's user message; ``None`` contributes nothing.
"""
import pytest

from hermies import commands, dossier, matchmaker


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    """Isolate dossier + matchmaker state under a fresh HERMIES_HOME and clear
    the per-process nudge caches so each test starts from a known state."""
    monkeypatch.setenv("HERMIES_HOME", str(tmp_path))
    commands._reset_nudge_state()
    yield
    commands._reset_nudge_state()


@pytest.fixture
def clock(monkeypatch):
    """A controllable fake clock for the throttle logic."""
    holder = {"t": 1_000_000.0}
    monkeypatch.setattr(commands, "_now", lambda: holder["t"])
    return holder


# --------------------------------------------------------------------------- #
# Core behaviour
# --------------------------------------------------------------------------- #

def test_nudge_fires_when_not_onboarded():
    result = commands.onboarding_nudge()
    assert isinstance(result, dict)
    # References the skill the agent must run and the opt-out lever.
    assert "hermies-onboarding" in result["context"]
    assert "hermies_pause" in result["context"]


def test_nudge_silent_when_onboarded():
    dossier.set_onboarded(True)
    assert commands.onboarding_nudge() is None


def test_nudge_silent_when_paused():
    """Human declined / left (paused=True) -> never nag, even if not onboarded."""
    state = matchmaker.load_state()
    state["paused"] = True
    matchmaker.save_state(state)
    assert commands.onboarding_nudge() is None


def test_nudge_throttled_within_the_hour(clock):
    first = commands.onboarding_nudge()
    assert isinstance(first, dict)  # fired

    clock["t"] += 59 * 60           # 59 minutes later — still inside the window
    assert commands.onboarding_nudge() is None

    clock["t"] += 2 * 60            # now 61 minutes since the first — window over
    again = commands.onboarding_nudge()
    assert isinstance(again, dict)  # fires again


def test_throttle_persists_across_a_process_restart(clock):
    """The stamp lives in matchmaker state, so a fresh process (cleared module
    caches) still honours the hourly throttle."""
    assert isinstance(commands.onboarding_nudge(), dict)   # stamps state

    commands._reset_nudge_state()          # simulate a gateway restart
    clock["t"] += 10 * 60                  # 10 minutes later
    assert commands.onboarding_nudge() is None  # persisted stamp still throttles

    commands._reset_nudge_state()
    clock["t"] += 60 * 60                  # well past the hour now
    assert isinstance(commands.onboarding_nudge(), dict)


# --------------------------------------------------------------------------- #
# Cost discipline: steady state does zero file IO
# --------------------------------------------------------------------------- #

def test_onboarded_steady_state_does_no_file_io(monkeypatch):
    """After the first check confirms onboarding, the latch means later calls
    read neither the dossier nor the matchmaker state file."""
    dossier.set_onboarded(True)
    assert commands.onboarding_nudge() is None  # first call latches _onboarded

    calls = {"dossier": 0, "state": 0}
    real_is_onboarded = dossier.is_onboarded
    real_load_state = matchmaker.load_state

    def spy_is_onboarded(*a, **k):
        calls["dossier"] += 1
        return real_is_onboarded(*a, **k)

    def spy_load_state(*a, **k):
        calls["state"] += 1
        return real_load_state(*a, **k)

    monkeypatch.setattr(commands.dossier, "is_onboarded", spy_is_onboarded)
    monkeypatch.setattr(commands.matchmaker, "load_state", spy_load_state)

    for _ in range(5):
        assert commands.onboarding_nudge() is None
    assert calls == {"dossier": 0, "state": 0}


# --------------------------------------------------------------------------- #
# pre_llm_call return-shape contract (conformance style)
# --------------------------------------------------------------------------- #

def test_return_shape_matches_hermes_pre_llm_call_contract():
    """Hermes extracts context as ``r["context"]`` when r is a dict (else a bare
    string); anything else is skipped (agent/turn_context.py). Assert we return
    exactly the dict form, with a non-empty string, and NONE of the pre_tool_call
    directive keys that belong to a different hook."""
    result = commands.onboarding_nudge()
    assert isinstance(result, dict)
    assert set(result) == {"context"}
    assert isinstance(result["context"], str) and result["context"].strip()
    # Not a pre_tool_call directive — those keys must not appear here.
    assert "action" not in result and "message" not in result

    # And the "nothing to inject" path is a bare None (falsy, skipped by Hermes),
    # never an empty dict/string.
    dossier.set_onboarded(True)
    commands._reset_nudge_state()
    assert commands.onboarding_nudge() is None


def test_hook_accepts_the_real_pre_llm_call_kwargs():
    """Hermes invokes the hook kwargs-only with these keys (turn_context.py).
    Our ``def onboarding_nudge(**kwargs)`` must swallow them without error."""
    out = commands.onboarding_nudge(
        session_id="s1", task_id="t1", turn_id="u1",
        user_message="hello", conversation_history=[], is_first_turn=True,
        model="x", platform="telegram", sender_id="123",
        telemetry_schema_version=1,
    )
    assert isinstance(out, dict) and out.get("context")
