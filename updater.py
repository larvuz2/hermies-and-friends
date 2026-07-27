"""Background self-update for the plugin's CODE.

Config and behaviour text ship live via remote_config.py. Anything that needs
new Python still needs the file on disk — so instead of asking every human to
run `git pull`, the plugin quietly keeps its own checkout current.

What this does NOT do, deliberately:
  * it never restarts the gateway. Hermes blocks that from inside its own
    process, and killing the human's in-flight task to ship an update would be
    exactly the hostile behaviour we're trying to remove. New code sits on disk
    and is picked up at the next natural restart.
  * it never runs anything but `git pull --ff-only` in the plugin directory —
    no build steps, no dependency installs, no arbitrary commands from the hub.
  * it never touches a checkout with local modifications (someone is hacking on
    it) or one that isn't a git repo.

Opt out completely with HERMIES_AUTO_UPDATE=0.
"""
import logging
import os
import pathlib
import subprocess
import time

log = logging.getLogger("hermies.updater")

_LAST_CHECK = 0.0


def enabled() -> bool:
    raw = (os.environ.get("HERMIES_AUTO_UPDATE", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def plugin_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent


def _git(*args, cwd=None, timeout=60):
    """Run git with stdin closed and prompts disabled — this runs unattended."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/true")
    return subprocess.run(("git",) + args, cwd=str(cwd or plugin_dir()),
                          capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, env=env)


def local_revision() -> str:
    try:
        r = _git("rev-parse", "--short", "HEAD", timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _is_clean_git_checkout() -> bool:
    try:
        if not (plugin_dir() / ".git").exists():
            return False
        r = _git("status", "--porcelain", timeout=20)
        return r.returncode == 0 and not r.stdout.strip()
    except Exception:
        return False


def check_and_update(now=None, force=False) -> dict:
    """Pull new plugin code if there is any. Returns a small result dict.

    ``pending_restart`` is True when NEW code landed: the running process is
    still on the old code until the gateway next restarts."""
    global _LAST_CHECK
    result = {"checked": False, "updated": False, "pending_restart": False,
              "from": "", "to": "", "reason": ""}
    if not enabled():
        result["reason"] = "disabled"
        return result

    t = float(now() if callable(now) else (now if now is not None else time.time()))
    try:
        from . import remote_config
        every = max(1.0, float(remote_config.knob("auto_update_hours", 24.0))) * 3600.0
    except Exception:
        every = 24 * 3600.0
    if not force and (t - _LAST_CHECK) < every:
        result["reason"] = "not due"
        return result
    _LAST_CHECK = t
    result["checked"] = True

    if not _is_clean_git_checkout():
        result["reason"] = "not a clean git checkout — leaving it alone"
        return result

    before = local_revision()
    result["from"] = before
    try:
        pull = _git("pull", "--ff-only", timeout=120)
    except Exception as exc:
        result["reason"] = f"git pull failed: {exc}"
        return result
    if pull.returncode != 0:
        result["reason"] = (pull.stderr or pull.stdout or "git pull failed").strip()[:200]
        return result

    after = local_revision()
    result["to"] = after
    if after and before and after != before:
        result["updated"] = True
        result["pending_restart"] = True
        result["reason"] = "new code on disk; active after the next gateway restart"
        log.info("hermies self-updated %s -> %s (restart pending)", before, after)
    else:
        result["reason"] = "already current"
    return result
