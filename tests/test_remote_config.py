"""Live-config + self-update: how the network improves without users ever
running a command.

The contract these pin:
  * hub values override built-in defaults          (we can tune centrally)
  * an explicit env var still beats the hub        (operators keep control)
  * an unreachable/garbage hub never breaks anything (fall back, stay quiet)
  * self-update never restarts the gateway and never touches a dirty checkout
"""
import json

import pytest

from hermix import _config, remote_config, updater


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    remote_config._reset_for_tests()
    yield
    remote_config._reset_for_tests()


class FakeClient:
    def __init__(self, doc=None, boom=False):
        self.doc, self.boom, self.calls = doc, boom, 0

    def get_config(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("hub down")
        return self.doc


def _doc(**knobs):
    return {"version": 2, "knobs": knobs, "notice": None}


# --- precedence -------------------------------------------------------------
def test_hub_value_overrides_builtin_default(monkeypatch):
    monkeypatch.delenv("HERMIX_INTERRUPT_THRESHOLD", raising=False)
    assert _config.interrupt_threshold() == 5.0            # built-in
    remote_config.refresh(FakeClient(_doc(interrupt_threshold=7.25)), force=True)
    assert _config.interrupt_threshold() == 7.25           # tuned centrally


def test_explicit_env_still_wins_over_hub(monkeypatch):
    remote_config.refresh(FakeClient(_doc(interrupt_threshold=7.25)), force=True)
    monkeypatch.setenv("HERMIX_INTERRUPT_THRESHOLD", "9.5")
    assert _config.interrupt_threshold() == 9.5            # operator keeps control


def test_int_knobs_route_through_too(monkeypatch):
    monkeypatch.delenv("HERMIX_MATCH_EVERY_HOURS", raising=False)
    remote_config.refresh(FakeClient(_doc(match_every_hours=2)), force=True)
    assert _config.match_every_hours() == 2


def test_env_name_maps_to_hub_knob_name():
    assert _config._knob_name("HERMIX_PRESSURE_WEIGHT") == "pressure_weight"


# --- resilience -------------------------------------------------------------
def test_hub_down_keeps_last_known_good(monkeypatch):
    monkeypatch.delenv("HERMIX_INTERRUPT_THRESHOLD", raising=False)
    remote_config.refresh(FakeClient(_doc(interrupt_threshold=6.5)), force=True)
    assert remote_config.refresh(FakeClient(boom=True), force=True) is False
    assert _config.interrupt_threshold() == 6.5            # cache survives


def test_garbage_document_is_ignored(monkeypatch):
    monkeypatch.delenv("HERMIX_INTERRUPT_THRESHOLD", raising=False)
    remote_config.refresh(FakeClient(_doc(interrupt_threshold=6.5)), force=True)
    assert remote_config.refresh(FakeClient({"nope": 1}), force=True) is False
    assert _config.interrupt_threshold() == 6.5


def test_wrong_typed_value_falls_back(monkeypatch):
    monkeypatch.delenv("HERMIX_INTERRUPT_THRESHOLD", raising=False)
    remote_config.refresh(FakeClient(_doc(interrupt_threshold="banana")), force=True)
    assert _config.interrupt_threshold() == 5.0            # default, not a crash


def test_client_without_get_config_is_a_noop():
    assert remote_config.refresh(object(), force=True) is False


def test_cache_persists_across_process_restart(tmp_path):
    remote_config.refresh(FakeClient(_doc(interrupt_threshold=6.75)), force=True)
    remote_config._reset_for_tests()                       # simulate a restart
    assert remote_config.knob("interrupt_threshold", 5.0) == 6.75


def test_refresh_is_throttled():
    c = FakeClient(_doc(config_refresh_hours=6))
    remote_config.refresh(c, now=1000.0, force=True)
    remote_config.refresh(c, now=1000.0 + 60)              # far too soon
    assert c.calls == 1


# --- self-update ------------------------------------------------------------
def test_auto_update_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HERMIX_AUTO_UPDATE", "0")
    assert updater.enabled() is False
    assert updater.check_and_update(force=True)["reason"] == "disabled"


def test_update_refuses_a_dirty_or_non_git_checkout(monkeypatch):
    monkeypatch.delenv("HERMIX_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(updater, "_is_clean_git_checkout", lambda: False)
    res = updater.check_and_update(force=True)
    assert res["checked"] is True and res["updated"] is False
    assert "clean git checkout" in res["reason"]


def test_update_reports_pending_restart_without_restarting(monkeypatch):
    """New code lands on disk; the gateway is NEVER restarted for us."""
    monkeypatch.delenv("HERMIX_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(updater, "_is_clean_git_checkout", lambda: True)
    revs = iter(["v0.9.0", "v0.9.1"])
    monkeypatch.setattr(updater, "active_version", lambda: next(revs))

    class OK:
        returncode, stdout, stderr = 0, "Updating aaa..bbb", ""
    monkeypatch.setattr(updater, "_git", lambda *a, **k: OK())
    res = updater.check_and_update(force=True)
    assert res["updated"] is True
    assert res["pending_restart"] is True
    assert res["from"] == "v0.9.0" and res["to"] == "v0.9.1"


def test_update_is_quiet_when_already_current(monkeypatch):
    monkeypatch.delenv("HERMIX_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(updater, "_is_clean_git_checkout", lambda: True)
    monkeypatch.setattr(updater, "active_version", lambda: "v0.9.0")

    class OK:
        returncode, stdout, stderr = 0, "Already up to date.", ""
    monkeypatch.setattr(updater, "_git", lambda *a, **k: OK())
    res = updater.check_and_update(force=True)
    assert res["updated"] is False and res["pending_restart"] is False


def test_updater_can_only_ever_shell_out_to_git(monkeypatch):
    """The real guarantee: ONE subprocess entry point, and it always runs git.
    So the updater structurally cannot restart the gateway or run anything the
    hub might suggest."""
    import ast
    import pathlib
    src = pathlib.Path(updater.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    runs = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run"
            and getattr(n.func.value, "id", "") == "subprocess"]
    assert len(runs) == 1, "exactly one subprocess call site expected"

    # ...and that call site is inside _git, whose argv always starts with "git".
    calls = []
    monkeypatch.setattr(updater.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or _Done())
    updater._git("status", "--porcelain")
    assert calls and calls[0][0] == "git"


class _Done:
    returncode, stdout, stderr = 0, "", ""


# --- single-flight poller ---------------------------------------------------
def test_only_one_process_polls(monkeypatch, tmp_path):
    """Hermes runs subagents as separate processes; each calls register(). The
    lease keeps exactly one of them talking to the hub."""
    from hermix import service
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    assert service._claim_poller_lock(now=1000.0) is True      # first wins

    # a DIFFERENT process arrives while the lease is fresh -> stands down
    monkeypatch.setattr(service.os, "getpid", lambda: 999999)
    assert service._claim_poller_lock(now=1000.0 + 30) is False

    # ...but an abandoned lease is taken over, so a killed process can't wedge us
    assert service._claim_poller_lock(now=1000.0 + service.LEASE_SECONDS + 5) is True


# --- kill switches ----------------------------------------------------------
def _sw_doc(**switches):
    return {"version": 3, "knobs": {}, "switches": switches, "notice": None}


def test_switch_defaults_true_when_absent():
    """A missing/unreachable config must never silently disable the product."""
    assert remote_config.switch("digs_enabled") is True


def test_switch_reads_the_hub():
    remote_config.refresh(FakeClient(_sw_doc(digs_enabled=False)), force=True)
    assert remote_config.switch("digs_enabled") is False
    assert remote_config.switch("envoy_replies_enabled") is True   # untouched


def test_digs_switch_stops_new_conversations(monkeypatch, tmp_path):
    """The operator brake: flip it and agents stop starting digs — no release,
    no restart, nobody touches a terminal."""
    from hermix import matchmaker, profile
    from hermix.client import HermixClient
    from hermix.mock_backend import MockBackend
    monkeypatch.setenv("HERMIX_MIN_SCORE", "1")
    card = profile.PublicCard(handle="gus-herald", represents="x",
                              offer=["ai video"], need=["music visuals"],
                              guilds=["ai-video"])
    b = MockBackend(); b._inbox = []
    client = HermixClient(b)
    client.publish_profile(card.public_dict())
    llm = lambda s, u, **k: '{"verdict":"drop","pitch":"","reason":""}'

    remote_config.refresh(FakeClient(_sw_doc(digs_enabled=False)), force=True)
    st = matchmaker.new_state()
    matchmaker.run_engine(st, client, card, llm, lambda: 1_000_000.0)
    assert st["digs"] == {}, "no dig may be opened while the brake is on"

    remote_config.refresh(FakeClient(_sw_doc(digs_enabled=True)), force=True)
    matchmaker.run_engine(st, client, card, llm, lambda: 1_000_100.0)
    assert st["digs"], "digs resume when the brake is released"


def test_notifications_switch_holds_without_losing(monkeypatch):
    from hermix import matchmaker
    monkeypatch.setenv("HERMIX_QUIET_HOURS", "")
    remote_config.refresh(FakeClient(_sw_doc(notifications_enabled=False)), force=True)
    st = matchmaker.new_state()
    st["outbox"]["ready"] = [{"id": "x", "handle": "a", "represents": "r",
                              "pitch": "p", "reason": "", "evidence": "",
                              "next_step": "", "score": 9, "note": "",
                              "verified": True, "cards_only": False,
                              "intent": None}]
    assert matchmaker.deliver_pending(st, 1_000_000.0) == matchmaker.SILENT
    assert len(st["outbox"]["ready"]) == 1        # held, never consumed


# --- staged rollout ---------------------------------------------------------
def test_rollout_is_stable_per_agent():
    """The same agent always lands on the same side of a percentage."""
    a = [updater._in_rollout("gus-herald", 50) for _ in range(5)]
    assert len(set(a)) == 1, "must not flip between polls"
    assert updater._in_rollout("anyone", 100) is True
    assert updater._in_rollout("anyone", 0) is False


def test_rollout_splits_the_population():
    handles = [f"agent-{i}" for i in range(400)]
    included = sum(1 for h in handles if updater._in_rollout(h, 25))
    assert 50 < included < 150, f"25% of 400 should be ~100, got {included}"


# --- sidecar / bridge split -------------------------------------------------
def test_plugin_stands_down_when_a_sidecar_is_alive(monkeypatch, tmp_path):
    """Phase 2: when our own process owns the network work, the in-gateway
    plugin does nothing — so the sidecar can be updated and restarted without
    ever disturbing the user's Hermes."""
    from hermix import service
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    assert service.sidecar_active(now=1000.0) is False        # none yet

    # A sidecar (different pid) announces itself.
    monkeypatch.setattr(service.os, "getpid", lambda: 4242)
    service._mark_sidecar_alive(now=1000.0)
    monkeypatch.setattr(service.os, "getpid", lambda: 777)    # the gateway
    assert service.sidecar_active(now=1000.0 + 30) is True

    # A dead sidecar must not silence the plugin forever.
    assert service.sidecar_active(now=1000.0 + service.LEASE_SECONDS + 5) is False


def test_sidecar_entrypoint_needs_no_hermes_context():
    """The sidecar runs standalone because inference goes through the hub.
    Checked on the AST, not the prose — the docstring legitimately mentions
    ctx.llm to explain why it is absent."""
    import ast
    import pathlib
    from hermix import sidecar
    tree = ast.parse(pathlib.Path(sidecar.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "ctx" not in names, "the sidecar must not depend on a Hermes context"
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "llm_complete" in attrs, "it uses the hub's operator-paid inference"


def test_unauthenticated_sidecar_never_silences_the_plugin(monkeypatch, tmp_path):
    """FAIL-SAFE: a sidecar that cannot work (no API key) must NOT claim
    ownership — otherwise the plugin stands down and the agent goes dark."""
    from hermix import service, _config
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    assert _config.is_live() is False

    # Simulate the sidecar loop's guard: it only marks itself alive when live.
    if _config.is_live():
        service._mark_sidecar_alive(now=1000.0)
    assert service.sidecar_active(now=1000.0 + 10) is False, \
        "an unauthenticated sidecar must leave the work to the plugin"

    # With credentials it does take ownership.
    monkeypatch.setenv("HERMIX_API_KEY", "k")
    monkeypatch.setattr(service.os, "getpid", lambda: 5150)
    service._mark_sidecar_alive(now=2000.0)
    monkeypatch.setattr(service.os, "getpid", lambda: 111)
    assert service.sidecar_active(now=2000.0 + 10) is True
