"""Tests for the operator-paid LLM proxy, metering/budgets, /v1/profile/remove,
and the admin LLM section.

The upstream (OpenRouter) is ALWAYS mocked via monkeypatched ``httpx.post`` — no
test ever touches the network.
"""
import json

import llm_proxy
from conftest import register, auth


ADMIN_PW = "s3cret-pw"
KEY_ENV = "HERMIES_OPENROUTER_KEY"


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _ok_payload(text="hello there", prompt=10, completion=5):
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def _install_upstream(monkeypatch, resp, captured=None):
    """Patch httpx.post to return ``resp`` (a _FakeResp) and capture the payload."""
    def fake_post(url, json=None, headers=None, timeout=None):
        if captured is not None:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            captured["timeout"] = timeout
        return resp
    monkeypatch.setattr(llm_proxy.httpx, "post", fake_post)


def _msgs(content="hi"):
    return [{"role": "user", "content": content}]


# --- fail closed ----------------------------------------------------------
def test_503_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    key = register(client, "envoy-herald", "r")
    r = client.post("/v1/llm/complete",
                    json={"messages": _msgs(), "purpose": "envoy"},
                    headers=auth(key))
    assert r.status_code == 503
    assert r.json()["error"] == "llm not configured"


def test_auth_required(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-operator-xyz")
    r = client.post("/v1/llm/complete",
                    json={"messages": _msgs(), "purpose": "envoy"})
    assert r.status_code == 401


# --- happy path + metering ------------------------------------------------
def test_happy_path_returns_text_and_records_usage(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-operator-xyz")
    _install_upstream(monkeypatch, _FakeResp(200, _ok_payload("an answer", 12, 7)))
    key = register(client, "envoy-herald", "r")

    r = client.post("/v1/llm/complete",
                    json={"messages": _msgs(), "purpose": "envoy"},
                    headers=auth(key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "an answer"
    assert body["model"] == "qwen/qwen3.7-max"
    assert body["tokens"] == {"prompt": 12, "completion": 7}

    # Usage was metered (per-agent + aggregate).
    db = client._app_module.db
    assert db.llm_tokens_today("envoy-herald") == 19
    usage = db.llm_usage_today()
    assert usage["calls"] == 1
    assert usage["prompt_tokens"] == 12 and usage["completion_tokens"] == 7


# --- budgets --------------------------------------------------------------
def test_per_agent_budget_429(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-operator-xyz")
    monkeypatch.setenv("HERMIES_LLM_DAILY_TOKENS", "5")   # tiny per-agent cap
    _install_upstream(monkeypatch, _FakeResp(200, _ok_payload("x", 10, 5)))
    key = register(client, "envoy-herald", "r")

    # First call records 15 tokens (usage was 0 < 5 before it).
    assert client.post("/v1/llm/complete",
                       json={"messages": _msgs(), "purpose": "envoy"},
                       headers=auth(key)).status_code == 200
    # Second call: recorded usage (15) >= cap (5) -> 429.
    r = client.post("/v1/llm/complete",
                    json={"messages": _msgs(), "purpose": "envoy"},
                    headers=auth(key))
    assert r.status_code == 429
    assert r.json()["error"] == "llm budget exceeded"


def test_global_budget_429(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-operator-xyz")
    monkeypatch.setenv("HERMIES_LLM_GLOBAL_DAILY_TOKENS", "5")   # tiny global cap
    _install_upstream(monkeypatch, _FakeResp(200, _ok_payload("x", 10, 5)))
    # Two different agents: the global cap trips regardless of who calls.
    key_a = register(client, "envoy-a", "r")
    key_b = register(client, "envoy-b", "r")

    assert client.post("/v1/llm/complete",
                       json={"messages": _msgs(), "purpose": "envoy"},
                       headers=auth(key_a)).status_code == 200
    r = client.post("/v1/llm/complete",
                    json={"messages": _msgs(), "purpose": "envoy"},
                    headers=auth(key_b))
    assert r.status_code == 429
    assert r.json()["error"] == "llm budget exceeded"


# --- payload caps ---------------------------------------------------------
def test_oversize_413(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-operator-xyz")
    key = register(client, "envoy-herald", "r")

    # Total content over 32k chars -> 413.
    big = client.post(
        "/v1/llm/complete",
        json={"messages": _msgs("x" * 32_001), "purpose": "envoy"},
        headers=auth(key),
    )
    assert big.status_code == 413

    # More than 40 messages -> 413.
    many = client.post(
        "/v1/llm/complete",
        json={"messages": [{"role": "user", "content": "hi"}] * 41,
              "purpose": "envoy"},
        headers=auth(key),
    )
    assert many.status_code == 413


def test_invalid_purpose_400(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-operator-xyz")
    key = register(client, "envoy-herald", "r")
    r = client.post("/v1/llm/complete",
                    json={"messages": _msgs(), "purpose": "chitchat"},
                    headers=auth(key))
    assert r.status_code == 400


# --- upstream errors are redacted -----------------------------------------
def test_upstream_error_502_redacts_details(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-operator-SECRET-999")
    # Upstream body carries a secret-looking detail that must NOT be echoed.
    payload = {"error": {"message": "invalid api key sk-operator-SECRET-999 leaked"}}
    _install_upstream(monkeypatch, _FakeResp(500, payload))
    key = register(client, "envoy-herald", "r")

    r = client.post("/v1/llm/complete",
                    json={"messages": _msgs(), "purpose": "envoy"},
                    headers=auth(key))
    assert r.status_code == 502
    detail = r.json()["error"]
    assert "500" in detail                       # status surfaced
    assert "SECRET" not in r.text                 # key never leaked
    assert "leaked" not in r.text                 # upstream body never leaked


# --- purpose -> model routing ---------------------------------------------
def test_purpose_routes_to_model(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-operator-xyz")
    monkeypatch.setenv("HERMIES_LLM_MODEL_JUDGE", "test/judge-model")
    captured = {}
    _install_upstream(monkeypatch, _FakeResp(200, _ok_payload()), captured)
    key = register(client, "envoy-herald", "r")

    # Judge routes to its configured model.
    r = client.post("/v1/llm/complete",
                    json={"messages": _msgs(), "purpose": "judge"},
                    headers=auth(key))
    assert r.status_code == 200
    assert captured["json"]["model"] == "test/judge-model"
    assert r.json()["model"] == "test/judge-model"
    # Envoy falls back to the default model.
    client.post("/v1/llm/complete",
                json={"messages": _msgs(), "purpose": "envoy"},
                headers=auth(key))
    assert captured["json"]["model"] == "qwen/qwen3.7-max"
    # The completion is capped.
    assert captured["json"]["max_tokens"] == 1024


# --- /v1/profile/remove (opt-out) -----------------------------------------
CARD_A = {"handle": "aria-herald", "represents": "an AI video artist",
          "offer": ["ai video", "generative film"],
          "need": ["music visuals", "sound design"], "guilds": ["ai-video"]}
CARD_B = {"handle": "bex-herald", "represents": "a music-visuals producer",
          "offer": ["music visuals", "beat sync"],
          "need": ["ai video", "distribution"], "guilds": ["music"]}


def _search_handles(client, key, query=""):
    r = client.post("/v1/search", json={"query": query}, headers=auth(key))
    return [a["handle"] for a in r.json()["agents"]]


def _discover_agents(client, key, card):
    r = client.post("/v1/discover", json={"card": card}, headers=auth(key))
    return [s["agent"] for s in r.json()["signals"]]


def test_profile_remove_clears_card_and_matches_then_republish(client):
    key_a = register(client, CARD_A["handle"], CARD_A["represents"])
    key_b = register(client, CARD_B["handle"], CARD_B["represents"])
    client.post("/v1/profile", json={"card": CARD_A}, headers=auth(key_a))
    client.post("/v1/profile", json={"card": CARD_B}, headers=auth(key_b))

    # Before removal: A is searchable and matches B in the live engine.
    assert "aria-herald" in _search_handles(client, key_b)
    assert "aria-herald" in _discover_agents(client, key_b, CARD_B)

    # Opt out.
    r = client.post("/v1/profile/remove", json={}, headers=auth(key_a))
    assert r.status_code == 200 and r.json() == {"ok": True}

    # Gone from search and from the engine index.
    assert "aria-herald" not in _search_handles(client, key_b)
    assert "aria-herald" not in _discover_agents(client, key_b, CARD_B)

    # Idempotent: removing again still succeeds.
    assert client.post("/v1/profile/remove", json={},
                       headers=auth(key_a)).status_code == 200

    # Account/key still valid: A can re-publish and reappears.
    assert client.post("/v1/profile", json={"card": CARD_A},
                       headers=auth(key_a)).status_code == 200
    assert "aria-herald" in _search_handles(client, key_b)
    assert "aria-herald" in _discover_agents(client, key_b, CARD_B)


# --- admin LLM section ----------------------------------------------------
def test_admin_llm_section_configured(client, monkeypatch):
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.setenv(KEY_ENV, "sk-operator-xyz")
    page = client.get("/admin", auth=("admin", ADMIN_PW))
    assert page.status_code == 200
    assert "LLM costs" in page.text
    assert "LLM: configured" in page.text
    assert "qwen/qwen3.7-max" in page.text     # default model shown
    assert "Top consumers today" in page.text
    stats = client.get("/admin/api/stats", auth=("admin", ADMIN_PW)).json()
    assert stats["llm"]["configured"] is True
    assert stats["llm"]["models"]["envoy"] == "qwen/qwen3.7-max"


def test_admin_llm_section_unconfigured(client, monkeypatch):
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.delenv(KEY_ENV, raising=False)
    page = client.get("/admin", auth=("admin", ADMIN_PW))
    assert page.status_code == 200
    assert "LLM costs" in page.text
    assert "LLM: not configured" in page.text
    stats = client.get("/admin/api/stats", auth=("admin", ADMIN_PW)).json()
    assert stats["llm"]["configured"] is False


# --- model picker ---------------------------------------------------------
def test_default_model_is_qwen(monkeypatch):
    for p in ("ENVOY", "JUDGE", "REFRESH"):
        monkeypatch.delenv(f"HERMIES_LLM_MODEL_{p}", raising=False)
    assert llm_proxy.DEFAULT_MODEL == "qwen/qwen3.7-max"
    assert llm_proxy.model_for("envoy") == "qwen/qwen3.7-max"


def test_selected_model_used_in_upstream_call(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-operator-xyz")
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", ADMIN_PW)
    for p in ("ENVOY", "JUDGE", "REFRESH"):
        monkeypatch.delenv(f"HERMIES_LLM_MODEL_{p}", raising=False)
    captured = {}
    _install_upstream(monkeypatch, _FakeResp(200, _ok_payload()), captured)
    key = register(client, "envoy-herald", "r")

    r = client.get("/admin/model", params={"model": "moonshotai/kimi-k3"},
                   auth=("admin", ADMIN_PW), follow_redirects=False)
    assert r.status_code == 303

    client.post("/v1/llm/complete",
                json={"messages": _msgs(), "purpose": "envoy"}, headers=auth(key))
    assert captured["json"]["model"] == "moonshotai/kimi-k3"


def test_set_model_rejects_unknown(client, monkeypatch):
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", ADMIN_PW)
    r = client.get("/admin/model", params={"model": "evil/not-a-model"},
                   auth=("admin", ADMIN_PW), follow_redirects=False)
    assert r.status_code == 400


def test_set_model_requires_auth(client, monkeypatch):
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", ADMIN_PW)
    r = client.get("/admin/model", params={"model": "moonshotai/kimi-k3"},
                   follow_redirects=False)
    assert r.status_code == 401


def test_admin_renders_model_picker(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-operator-xyz")
    monkeypatch.setenv("HERMIES_ADMIN_PASSWORD", ADMIN_PW)
    page = client.get("/admin", auth=("admin", ADMIN_PW)).text
    assert "Active model" in page
    assert 'action="/admin/model"' in page
    assert "moonshotai/kimi-k3" in page      # an option is present
