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

def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return default
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


def max_notify_per_day() -> int:
    """Hard cap on human interruptions per rolling 24h."""
    return _int_env("HERMIES_MAX_NOTIFY_PER_DAY", 2)


def notify_min_gap_hours() -> int:
    """Minimum spacing between two notifications."""
    return _int_env("HERMIES_NOTIFY_MIN_GAP_HOURS", 4)


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


def envoy_max_replies() -> int:
    """Max replies the answering-side envoy daemon posts into a single thread
    before it politely concludes and closes it."""
    return _int_env("HERMIES_ENVOY_MAX_REPLIES", 6)
