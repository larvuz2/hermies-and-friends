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

from hermies import _config, remote_config, updater


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIES_HOME", str(tmp_path))
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
    monkeypatch.delenv("HERMIES_INTERRUPT_THRESHOLD", raising=False)
    assert _config.interrupt_threshold() == 5.0            # built-in
    remote_config.refresh(FakeClient(_doc(interrupt_threshold=7.25)), force=True)
    assert _config.interrupt_threshold() == 7.25           # tuned centrally


def test_explicit_env_still_wins_over_hub(monkeypatch):
    remote_config.refresh(FakeClient(_doc(interrupt_threshold=7.25)), force=True)
    monkeypatch.setenv("HERMIES_INTERRUPT_THRESHOLD", "9.5")
    assert _config.interrupt_threshold() == 9.5            # operator keeps control


def test_int_knobs_route_through_too(monkeypatch):
    monkeypatch.delenv("HERMIES_MATCH_EVERY_HOURS", raising=False)
    remote_config.refresh(FakeClient(_doc(match_every_hours=2)), force=True)
    assert _config.match_every_hours() == 2


def test_env_name_maps_to_hub_knob_name():
    assert _config._knob_name("HERMIES_PRESSURE_WEIGHT") == "pressure_weight"


# --- resilience -------------------------------------------------------------
def test_hub_down_keeps_last_known_good(monkeypatch):
    monkeypatch.delenv("HERMIES_INTERRUPT_THRESHOLD", raising=False)
    remote_config.refresh(FakeClient(_doc(interrupt_threshold=6.5)), force=True)
    assert remote_config.refresh(FakeClient(boom=True), force=True) is False
    assert _config.interrupt_threshold() == 6.5            # cache survives


def test_garbage_document_is_ignored(monkeypatch):
    monkeypatch.delenv("HERMIES_INTERRUPT_THRESHOLD", raising=False)
    remote_config.refresh(FakeClient(_doc(interrupt_threshold=6.5)), force=True)
    assert remote_config.refresh(FakeClient({"nope": 1}), force=True) is False
    assert _config.interrupt_threshold() == 6.5


def test_wrong_typed_value_falls_back(monkeypatch):
    monkeypatch.delenv("HERMIES_INTERRUPT_THRESHOLD", raising=False)
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
    monkeypatch.setenv("HERMIES_AUTO_UPDATE", "0")
    assert updater.enabled() is False
    assert updater.check_and_update(force=True)["reason"] == "disabled"


def test_update_refuses_a_dirty_or_non_git_checkout(monkeypatch):
    monkeypatch.delenv("HERMIES_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(updater, "_is_clean_git_checkout", lambda: False)
    res = updater.check_and_update(force=True)
    assert res["checked"] is True and res["updated"] is False
    assert "clean git checkout" in res["reason"]


def test_update_reports_pending_restart_without_restarting(monkeypatch):
    """New code lands on disk; the gateway is NEVER restarted for us."""
    monkeypatch.delenv("HERMIES_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(updater, "_is_clean_git_checkout", lambda: True)
    revs = iter(["aaaaaaa", "bbbbbbb"])
    monkeypatch.setattr(updater, "local_revision", lambda: next(revs))

    class OK:
        returncode, stdout, stderr = 0, "Updating aaa..bbb", ""
    monkeypatch.setattr(updater, "_git", lambda *a, **k: OK())
    res = updater.check_and_update(force=True)
    assert res["updated"] is True
    assert res["pending_restart"] is True
    assert res["from"] == "aaaaaaa" and res["to"] == "bbbbbbb"


def test_update_is_quiet_when_already_current(monkeypatch):
    monkeypatch.delenv("HERMIES_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(updater, "_is_clean_git_checkout", lambda: True)
    monkeypatch.setattr(updater, "local_revision", lambda: "same123")

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
