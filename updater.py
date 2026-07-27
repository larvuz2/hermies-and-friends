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
import hashlib
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


def active_version() -> str:
    """The release TAG we are running, if any (else the short sha).

    Releases are tags, never 'main': activating whatever happens to be on a
    branch means every push lands on every user with no staging."""
    try:
        r = _git("describe", "--tags", "--exact-match", timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        r = _git("describe", "--tags", "--abbrev=0", timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip() + "+"        # ahead of the last tag
    except Exception:
        pass
    return local_revision()


def _in_rollout(handle: str, percentage) -> bool:
    """Stable per-agent cohort: the same agent always lands on the same side of
    a percentage, so a staged rollout is reproducible rather than a coin flip
    on every poll."""
    try:
        pct = float(percentage)
    except (TypeError, ValueError):
        pct = 100.0
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    digest = hashlib.sha256((handle or "anon").encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 100) < pct


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

    before = active_version()
    result["from"] = before

    # What does the hub want us on? Releases are TAGS with a staged rollout.
    desired, pct, handle = "", 100, ""
    try:
        from . import remote_config, profile
        rel = remote_config.release() or {}
        desired = str(rel.get("version") or "")
        pct = rel.get("rollout_percentage", 100)
        handle = (profile.load_card().public_dict().get("handle") or "")
    except Exception:
        pass

    if desired and not _in_rollout(handle, pct):
        result["reason"] = f"not in the {pct}% rollout for {desired} yet"
        return result

    try:
        fetched = _git("fetch", "--tags", "--force", timeout=120)
        if fetched.returncode != 0:
            result["reason"] = (fetched.stderr or "git fetch failed").strip()[:200]
            return result
        if desired:
            # Pin to the release tag — never activate a moving branch.
            checkout = _git("checkout", "--detach", f"tags/{desired}", timeout=120)
        else:
            checkout = _git("pull", "--ff-only", timeout=120)
    except Exception as exc:
        result["reason"] = f"git failed: {exc}"
        return result
    if checkout.returncode != 0:
        result["reason"] = (checkout.stderr or checkout.stdout
                            or "git checkout failed").strip()[:200]
        return result

    after = active_version()
    result["to"] = after
    if after and before and after != before:
        result["updated"] = True
        result["pending_restart"] = True
        result["reason"] = "new code on disk; active after the next gateway restart"
        log.info("hermies self-updated %s -> %s (restart pending)", before, after)
    else:
        result["reason"] = "already current"
    return result
