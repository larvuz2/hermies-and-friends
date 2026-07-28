"""Environment-based config for the Hermies plugin.

Mirrors the humalike pattern: a fresh install needs zero env setup — sensible
defaults, and a device login later fills HERMIES_API_KEY into ~/.hermes/.env.
Setting HERMIES_API_URL empty disables all network calls (offline/demo mode).
"""
import os

# The public hub. A fresh install joins it with ZERO config: the plugin
# auto-registers on first card publish to obtain its key. Set HERMIES_API_URL
# empty to force offline/mock mode.
DEFAULT_API_URL = "https://srv1691895.hstgr.cloud"


def service_url() -> str:
    """Base URL for the Hermies backend. Empty string == network disabled."""
    val = os.getenv("HERMIES_API_URL", DEFAULT_API_URL)
    return val.strip()


def api_key() -> str:
    """Bearer token for the backend. Empty until the plugin auto-registers."""
    return os.getenv("HERMIES_API_KEY", "").strip()


def has_hub() -> bool:
    """True when a hub URL is configured (the default is the public hub). The
    key may still be missing — it is obtained by auto-registration on first
    publish. Controls transport selection (real HTTP vs the in-process mock)."""
    return bool(service_url())


def is_live() -> bool:
    """AUTHENTICATED-live: we have both a hub URL and a key, so authed network
    features (publish/discover/threads/LLM proxy) work. Distinct from has_hub(),
    which is true before we have registered."""
    return bool(service_url()) and bool(api_key())


def _env_file_path() -> str:
    """Where to persist the auto-obtained key. HERMIES_ENV_FILE overrides (tests);
    else ~/.hermes/.env (HERMES_HOME honoured)."""
    override = os.environ.get("HERMIES_ENV_FILE")
    if override:
        return override
    base = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(base, ".env")


def persist_api_key(key: str) -> None:
    """Save an auto-obtained key to this process's env (so the plugin is live
    immediately) and to ~/.hermes/.env (so it survives a restart). Best-effort:
    the in-process env alone is enough to function this session."""
    key = (key or "").strip()
    if not key:
        return
    os.environ["HERMIES_API_KEY"] = key
    try:
        path = _env_file_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        lines = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines()
                         if not ln.startswith("HERMIES_API_KEY=")]
        lines.append(f"HERMIES_API_KEY={key}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def llm_mode() -> str:
    """Where the network's "thinking" runs, from HERMIES_LLM:

      - "auto"  (default): use operator-paid hub inference when the plugin is
                 live; on any hub failure (or when not live) fall back to the
                 user's own ctx.llm so the plugin never goes mute.
      - "hub":   hub inference ONLY. The user's model budget is never spent; on
                 any hub failure the caller receives the safe silence sentinel.
      - "local": the user's own ctx.llm ONLY — never touch the hub.
    """
    raw = os.getenv("HERMIES_LLM", "")
    raw = raw.strip().lower() if isinstance(raw, str) else ""
    if raw in ("auto", "hub", "local"):
        return raw
    return "auto"


# --------------------------------------------------------------------------- #
# Matchmaker knobs — all env-overridable, all with sane silence-by-default
# values. Read through functions (never cached) so a value written to ~/.hermes
# /.env mid-session is honoured on the next cycle, and so tests can flip a knob
# with monkeypatch.setenv. See matchmaker.py.
# --------------------------------------------------------------------------- #

def _knob_name(env_name: str) -> str:
    """HERMIES_INTERRUPT_THRESHOLD -> interrupt_threshold (the hub's key)."""
    return env_name[len("HERMIES_"):].lower() if env_name.startswith("HERMIES_") \
        else env_name.lower()


def _remote(env_name: str, default):
    """The hub's live value for this knob, or ``default``.

    This is what lets the network keep improving without a single user running
    a command: we tune centrally, every agent picks it up within the hour. An
    explicit env var still wins (see _int_env/_float_env)."""
    try:
        from . import remote_config
        return remote_config.knob(_knob_name(env_name), default)
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return int(_remote(name, default))       # hub value, else built-in
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return float(_remote(name, default))     # hub value, else built-in
    try:
        return float(raw)
    except ValueError:
        return default


def min_score() -> float:
    """Stage-1 cheap filter: drop any candidate signal below this score."""
    return _float_env("HERMIES_MIN_SCORE", 3.0)


def match_every_hours() -> int:
    """How often the matchmaker cron/daemon looks for opportunities."""
    return _int_env("HERMIES_MATCH_EVERY_HOURS", 4)


# --------------------------------------------------------------------------- #
# When to interrupt the human.
#
# There is deliberately NO daily quota. A good friend doesn't ration contact —
# they judge whether this particular thing is worth your attention right now.
# The model: score every finding, and compare it against a threshold that RISES
# as we interrupt more (a recovering "social battery") and FALLS when the human
# engages with what we bring. Anything under the bar is not discarded — it rides
# along with the next natural conversation (see hermies_pending).
# --------------------------------------------------------------------------- #

def interrupt_threshold() -> float:
    """Base bar (0..10) a finding must clear to interrupt the human proactively.

    Deliberately modest: everything reaching this point has ALREADY survived the
    cheap filter, a real agent-to-agent dig, and an LLM judge that returned
    "notify". The bar's job is pacing, not re-litigating quality — pressure
    raises it after each interruption."""
    return _float_env("HERMIES_INTERRUPT_THRESHOLD", 5.0)


def urgent_threshold() -> float:
    """At/above this, it goes through regardless of pressure or quiet hours —
    the 'a friend calls you at midnight for this' line."""
    return _float_env("HERMIES_URGENT_THRESHOLD", 8.5)


def pressure_half_life_hours() -> float:
    """How fast the social battery recovers after an interruption."""
    return _float_env("HERMIES_PRESSURE_HALF_LIFE_H", 8.0)


def pressure_weight() -> float:
    """How much each recent interruption raises the bar."""
    return _float_env("HERMIES_PRESSURE_WEIGHT", 1.2)


def engagement_weight() -> float:
    """How much demonstrated interest (asking for more, requesting intros)
    lowers the bar — the human is telling us they want this."""
    return _float_env("HERMIES_ENGAGEMENT_WEIGHT", 0.8)


def quiet_hours() -> tuple:
    """Hours to stay silent unless something is urgent, as (start, end).

    OFF by default, deliberately: this reads the *machine's* clock, and the
    agent usually runs on a UTC VPS while its human lives somewhere else — so a
    default window would just as likely mute the workday as protect the night.
    Set HERMIES_QUIET_HOURS="22-8" when the host clock really is the human's."""
    raw = os.getenv("HERMIES_QUIET_HOURS", "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return ()
    try:
        a, b = raw.split("-")
        return (int(a) % 24, int(b) % 24)
    except (ValueError, AttributeError):
        return (22, 8)


def max_notify_per_day() -> int:
    """OPTIONAL hard safety cap. 0 (default) = no cap; judgement decides.
    Left in place for anyone who explicitly wants a ceiling."""
    return _int_env("HERMIES_MAX_NOTIFY_PER_DAY", 0)


def handshake_timeout_days() -> int:
    """If no reply arrives within this window, judge on cards alone."""
    return _int_env("HERMIES_HANDSHAKE_TIMEOUT_DAYS", 4)


def watch_days() -> int:
    """A 'watch' verdict is re-judged after this many days."""
    return _int_env("HERMIES_WATCH_DAYS", 7)


def drop_cooldown_days() -> int:
    """After a 'drop' verdict a candidate is ignored for this long."""
    return _int_env("HERMIES_DROP_COOLDOWN_DAYS", 14)


def card_refresh_days() -> int:
    """How often to propose (never auto-apply) an improved public card."""
    return _int_env("HERMIES_CARD_REFRESH_DAYS", 7)


def dig_max_turns() -> int:
    """Max OUTBOUND turns the matchmaker (initiator) spends on a dig thread
    before it concludes with a findings note. The opener counts as turn 1."""
    return _int_env("HERMIES_DIG_MAX_TURNS", 3)


def checkin_after_hours() -> int:
    """Hours after joining before the one-time "here's what I've been doing"
    check-in. A brand-new user cannot tell disciplined silence apart from a
    broken plugin, so we prove we're alive exactly once. 0 disables it."""
    return _int_env("HERMIES_CHECKIN_AFTER_HOURS", 24)


def ask_max_turns() -> int:
    """Max messages WE send in a user-requested investigation before reporting
    back. Enough to clarify, not enough to become a pen-pal."""
    return _int_env("HERMIES_ASK_MAX_TURNS", 3)


def envoy_max_replies() -> int:
    """Max replies the answering-side envoy daemon posts into a single thread
    before it politely concludes and closes it."""
    return _int_env("HERMIES_ENVOY_MAX_REPLIES", 6)
