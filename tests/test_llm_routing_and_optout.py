"""Operator-paid hub inference routing + the opt-out (pause / resume / leave).

Two things are pinned here:

  1. The routed LLM adapter (hermix.make_llm) decides — per HERMIX_LLM — whether
     the network's thinking bills the OPERATOR (hub inference) or the USER
     (ctx.llm). We assert every mode/liveness combination, and that ``purpose``
     ("envoy" | "judge" | "refresh") rides along to the hub from each call site.
  2. Opt-out: /hermix pause & leave flip the matchmaker's ``paused`` flag so
     run_cycle returns SILENT (daemon + hermix_scout tool go quiet); leave
     also calls client.remove_profile() while leaving the local dossier intact.
"""
import json
import urllib.error

import pytest

import hermix
from hermix import matchmaker, commands, envoy, profile, dossier, _config


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeCtxResult:
    def __init__(self, text):
        self.text = text


class FakeCtxLlm:
    """Stands in for ctx.llm (the USER's model). Records that it was hit."""
    def __init__(self, text="LOCAL_REPLY"):
        self.text = text
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        return FakeCtxResult(self.text)


class FakeCtx:
    def __init__(self, llm_text="LOCAL_REPLY"):
        self._llm = FakeCtxLlm(llm_text)

    @property
    def llm(self):
        return self._llm


class HubClient:
    """Stands in for HermixClient's hub inference. Records purposes; can be set
    to raise a 503/429-equivalent to exercise the failure paths."""
    def __init__(self, text="HUB_REPLY", error=None):
        self.text = text
        self.error = error
        self.calls = []          # list of (messages, purpose)
        self.purposes = []

    def llm_complete(self, messages, purpose):
        self.calls.append((messages, purpose))
        self.purposes.append(purpose)
        if self.error is not None:
            raise self.error
        return {"text": self.text, "model": "hub", "tokens": {"prompt": 1, "completion": 1}}


@pytest.fixture
def live(monkeypatch):
    """Force is_live() True deterministically (URL + key both present)."""
    monkeypatch.setenv("HERMIX_API_URL", "https://hub.example")
    monkeypatch.setenv("HERMIX_API_KEY", "test-key")


@pytest.fixture
def not_live(monkeypatch):
    """Force is_live() False deterministically (empty URL disables the network)."""
    monkeypatch.setenv("HERMIX_API_URL", "")
    monkeypatch.setenv("HERMIX_API_KEY", "")


def _adapter(hub_text="HUB_REPLY", hub_error=None, ctx_text="LOCAL_REPLY"):
    ctx = FakeCtx(ctx_text)
    client = HubClient(hub_text, hub_error)
    return hermix.make_llm(ctx, client), ctx, client


# --------------------------------------------------------------------------- #
# Adapter routing
# --------------------------------------------------------------------------- #
def test_auto_live_uses_hub(monkeypatch, live):
    monkeypatch.setenv("HERMIX_LLM", "auto")
    llm, ctx, client = _adapter()
    assert llm("sys", "usr") == "HUB_REPLY"
    assert client.calls and not ctx.llm.calls      # operator billed, user not


def test_auto_falls_back_to_ctx_llm_on_hub_failure(monkeypatch, live):
    monkeypatch.setenv("HERMIX_LLM", "auto")
    err = urllib.error.HTTPError("u", 503, "unconfigured", None, None)
    llm, ctx, client = _adapter(hub_error=err)
    assert llm("sys", "usr") == "LOCAL_REPLY"       # fell back
    assert client.calls and ctx.llm.calls           # tried hub, then user


def test_auto_not_live_uses_ctx_llm(monkeypatch, not_live):
    monkeypatch.setenv("HERMIX_LLM", "auto")
    llm, ctx, client = _adapter()
    assert llm("sys", "usr") == "LOCAL_REPLY"
    assert not client.calls and ctx.llm.calls       # hub never attempted


def test_hub_mode_uses_hub_when_live(monkeypatch, live):
    monkeypatch.setenv("HERMIX_LLM", "hub")
    llm, ctx, client = _adapter()
    assert llm("sys", "usr") == "HUB_REPLY"
    assert not ctx.llm.calls


def test_hub_mode_returns_empty_sentinel_on_failure(monkeypatch, live):
    monkeypatch.setenv("HERMIX_LLM", "hub")
    err = urllib.error.HTTPError("u", 429, "over budget", None, None)
    llm, ctx, client = _adapter(hub_error=err)
    assert llm("sys", "usr") == ""                  # safe silence, never bill user
    assert client.calls and not ctx.llm.calls


def test_hub_mode_not_live_returns_empty_and_never_bills_user(monkeypatch, not_live):
    monkeypatch.setenv("HERMIX_LLM", "hub")
    llm, ctx, client = _adapter()
    assert llm("sys", "usr") == ""
    assert not client.calls and not ctx.llm.calls   # user's model NEVER touched


def test_local_mode_ignores_hub_even_when_live(monkeypatch, live):
    monkeypatch.setenv("HERMIX_LLM", "local")
    llm, ctx, client = _adapter()
    assert llm("sys", "usr") == "LOCAL_REPLY"
    assert not client.calls and ctx.llm.calls       # hub never attempted


def test_default_mode_is_auto(monkeypatch, live):
    monkeypatch.delenv("HERMIX_LLM", raising=False)
    assert _config.llm_mode() == "auto"
    llm, ctx, client = _adapter()
    assert llm("sys", "usr") == "HUB_REPLY"          # auto -> hub when live


# --------------------------------------------------------------------------- #
# Purpose threading: envoy/judge/refresh reach the hub client
# --------------------------------------------------------------------------- #
def _card():
    return profile.PublicCard(
        handle="gus-herald", represents="a creative technologist",
        offer=["ai video"], need=["collaborators"])


def test_envoy_reply_and_opener_carry_purpose_envoy(monkeypatch, live):
    monkeypatch.setenv("HERMIX_LLM", "hub")
    llm, ctx, client = _adapter()
    envoy.respond(_card(), "who are you?", llm)          # inbound reply
    envoy.open_dig(_card(), "they need ai video", llm)   # dig opener
    assert client.purposes == ["envoy", "envoy"]


def test_matchmaker_judge_and_findings_carry_purpose_judge(monkeypatch, live):
    monkeypatch.setenv("HERMIX_LLM", "hub")
    llm, ctx, client = _adapter()
    matchmaker._judge(_card(), {"why": "x"}, "their reply", llm)
    matchmaker._write_findings(_card(), {"why": "x"}, "US: hi\nTHEM: hi", llm)
    matchmaker._judge_findings(_card(), {"why": "x"}, "a findings note", llm)
    assert client.purposes == ["judge", "judge", "judge"]


def test_card_refresh_carries_purpose_refresh(monkeypatch, live):
    monkeypatch.setenv("HERMIX_LLM", "hub")
    # hub returns strict-JSON so _maybe_refresh_card actually calls the model.
    llm, ctx, client = _adapter(hub_text='{"handle": "gus-herald"}')
    state = matchmaker.new_state()
    state["card_refreshed_ts"] = 0                      # force the interval open
    matchmaker._maybe_refresh_card(state, _card(), llm, 10 ** 9)
    assert client.purposes == ["refresh"]


# --------------------------------------------------------------------------- #
# Opt-out: paused cycle is SILENT and does no work
# --------------------------------------------------------------------------- #
class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


class _SilentClient:
    """If the matchmaker respects ``paused`` it must not call ANY hub method."""
    def __getattr__(self, name):
        def _boom(*a, **k):
            raise AssertionError(f"paused matchmaker must not call client.{name}")
        return _boom


def test_paused_cycle_returns_silent_and_touches_nothing():
    state = matchmaker.new_state()
    state["paused"] = True
    llm_calls = []
    out = matchmaker.run_cycle(state, _SilentClient(), _card(),
                               lambda s, u, **_: llm_calls.append(1) or "x",
                               _Clock())
    assert out == matchmaker.SILENT
    assert not llm_calls                     # no judge/findings/refresh either
    assert state["digs"] == {} and state["handshakes"] == {}


# --------------------------------------------------------------------------- #
# Opt-out commands: pause / resume / leave
# --------------------------------------------------------------------------- #
class SpyLeaveClient:
    def __init__(self):
        self.removed = 0

    def remove_profile(self):
        self.removed += 1
        return {"ok": True}


def _handler(client):
    return commands.make_handler(client, _card(), lambda s, u, **_: "x")


def test_pause_sets_flag_and_resume_clears_it(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    h = _handler(SpyLeaveClient())

    msg = h("pause")
    assert "paused" in msg.lower()
    assert matchmaker.load_state()["paused"] is True

    msg2 = h("resume")
    assert "resume" in msg2.lower()
    assert matchmaker.load_state()["paused"] is False


def test_leave_removes_profile_pauses_and_keeps_dossier(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    # Seed a local dossier so we can prove leave leaves it untouched.
    dossier.add_fact("ring1", None, "6 years in game audio")
    dossier.add_intent("a cofounder in AI film")
    dossier_path = dossier._dossier_path()
    assert dossier_path.exists()

    client = SpyLeaveClient()
    msg = _handler(client)("leave")

    assert client.removed == 1                              # hub card removed
    assert matchmaker.load_state()["paused"] is True        # matchmaking off
    assert "dossier" in msg.lower() and "local" in msg.lower()
    # The private dossier file — and its contents — survive the leave.
    assert dossier_path.exists()
    assert dossier.get_ring1() == ["6 years in game audio"]
    assert [i["text"] for i in dossier.list_intents()] == ["a cofounder in AI film"]


def test_republish_profile_rejoins_after_leave(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))

    class PubClient(SpyLeaveClient):
        def publish_profile(self, card):
            return {"ok": True}

    client = PubClient()
    h = _handler(client)
    h("leave")
    assert matchmaker.load_state()["paused"] is True

    out = h('profile {"tagline": "back online"}')
    assert "re-joined" in out.lower()
    assert matchmaker.load_state()["paused"] is False       # publishing re-joins


def test_paused_matchmake_tool_is_silent(monkeypatch, tmp_path):
    """The hermix_scout tool no-ops (SILENT) while paused."""
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    from hermix import tools
    from hermix.client import HermixClient
    from hermix.mock_backend import MockBackend

    state = matchmaker.new_state()
    state["paused"] = True
    matchmaker.save_state(state)

    h = {s["name"]: s["handler"]
         for s in tools.build(HermixClient(MockBackend()), _card(),
                              llm=lambda s, u, **_: "x")}
    out = json.loads(h["hermix_scout"]({}))
    assert out["result"] == matchmaker.SILENT
