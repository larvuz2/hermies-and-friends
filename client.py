"""Backend client + transport abstraction.

The client is transport-agnostic so the whole plugin runs offline against the
in-process MockBackend, and switches to real HTTP once a key is present — with
zero changes to commands/tools/service.
"""
import json
import urllib.request
import urllib.error

from . import _config


class HttpTransport:
    """Talks to the real Hermies backend over HTTPS with a Bearer key."""

    def __init__(self, base_url: str, key: str):
        self.base_url = base_url.rstrip("/")
        self.key = key

    def _post(self, path: str, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        # Tolerate an empty key: registration is unauthenticated, and until a
        # key exists we must NOT send a bogus "Bearer " header.
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # --- backend API contract (mirror these routes on the server) ---
    def register(self, handle: str, represents: str) -> dict:
        """Unauthenticated account creation. Returns {"api_key", "handle"}."""
        return self._post("/v1/register", {"handle": handle, "represents": represents})

    def publish_profile(self, card: dict) -> dict:
        return self._post("/v1/profile", {"card": card})

    def discover(self, card: dict) -> list:
        return self._post("/v1/discover", {"card": card}).get("signals", [])

    def list_inbound(self, handle: str) -> list:
        return self._post("/v1/inbound", {"handle": handle}).get("messages", [])

    def post_reply(self, message_id: str, text: str) -> dict:
        return self._post("/v1/reply", {"message_id": message_id, "text": text})

    def list_signals(self, handle: str) -> list:
        return self._post("/v1/signals", {"handle": handle}).get("signals", [])

    def search_agents(self, query: str) -> list:
        return self._post("/v1/search", {"query": query}).get("agents", [])

    def browse_skills(self, query: str) -> list:
        return self._post("/v1/skills", {"query": query}).get("skills", [])

    def send_message(self, to_handle: str, text: str) -> dict:
        return self._post("/v1/message", {"to": to_handle, "text": text})


class HermiesClient:
    """Thin façade over a transport. Adds nothing but a stable surface for the
    rest of the plugin; swap the transport to go live/offline."""

    def __init__(self, transport):
        self.t = transport

    def register(self, handle, represents): return self.t.register(handle, represents)
    def publish_profile(self, card): return self.t.publish_profile(card)
    def discover(self, card): return self.t.discover(card)
    def list_inbound(self, handle): return self.t.list_inbound(handle)
    def post_reply(self, mid, text): return self.t.post_reply(mid, text)
    def list_signals(self, handle): return self.t.list_signals(handle)
    def search_agents(self, query): return self.t.search_agents(query)
    def browse_skills(self, query): return self.t.browse_skills(query)
    def send_message(self, to_handle, text): return self.t.send_message(to_handle, text)


def make_transport():
    """Live HTTP when a key is present, otherwise the seeded mock backend."""
    if _config.is_live():
        return HttpTransport(_config.service_url(), _config.api_key())
    from .mock_backend import MockBackend
    return MockBackend()
