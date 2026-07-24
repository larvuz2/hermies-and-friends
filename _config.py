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
