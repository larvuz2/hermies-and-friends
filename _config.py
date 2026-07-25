"""Environment-based config for the Hermies plugin.

Mirrors the humalike pattern: a fresh install needs zero env setup — sensible
defaults, and a device login later fills HERMIES_API_KEY into ~/.hermes/.env.
Setting HERMIES_API_URL empty disables all network calls (offline/demo mode).
"""
import os

DEFAULT_API_URL = "https://api.hermies.network"


def service_url() -> str:
    """Base URL for the Hermies backend. Empty string == network disabled."""
    val = os.getenv("HERMIES_API_URL", DEFAULT_API_URL)
    return val.strip()


def api_key() -> str:
    """Bearer token for the backend. Empty until the user logs in / connects."""
    return os.getenv("HERMIES_API_KEY", "").strip()


def is_live() -> bool:
    """True only when we have both an endpoint and a key — otherwise run on the
    in-process mock backend so the plugin still works out of the box."""
    return bool(service_url()) and bool(api_key())


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
