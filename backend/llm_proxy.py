"""Operator-paid LLM inference proxy for the Hermix hub.

Plugin users never bring their own LLM key for network features (envoy/judge/
refresh). Instead the hub proxies those completions to OpenRouter using the
OPERATOR's key (env ``HERMIX_OPENROUTER_KEY``). This module owns the outbound
call and its guards; metering + budgets live in ``db.py`` and the ``/v1/llm/*``
route in ``app.py``.

Fail closed: with no key configured the caller sees ``503`` and no request ever
leaves the box. Upstream failures surface as ``502`` with a short, redacted
detail — the operator's key and the full upstream body are never leaked.
"""
import os

import httpx
from fastapi import HTTPException

try:
    import compat_env
except ImportError:  # loaded by path from outside backend/ (evals, tooling)
    import pathlib as _pl
    import sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import compat_env

# OpenRouter chat-completions endpoint (OpenAI-compatible schema).
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Purposes the plugin drives, each routable to its own model via env.
PURPOSES = ("envoy", "judge", "refresh")
DEFAULT_MODEL = "qwen/qwen3.7-max"

# Curated shortlist the admin dashboard offers as a model picker (real, current
# OpenRouter ids). Edit this list to change what shows in the dropdown.
TOP_MODELS = [
    ("qwen/qwen3.7-max",        "Qwen3.7 Max — capable + cheap (default)"),
    ("moonshotai/kimi-k3",      "Kimi K3 — Moonshot, strong reasoning"),
    ("anthropic/claude-opus-5", "Claude Opus 5 — top quality (pricier)"),
    ("openai/gpt-5.6-sol",      "GPT-5.6 Sol — OpenAI frontier"),
    ("google/gemini-3.6-flash", "Gemini 3.6 Flash — fastest / cheapest"),
]
TOP_MODEL_IDS = {m for m, _ in TOP_MODELS}

# Real OpenRouter list prices, USD per MILLION tokens (prompt, completion).
# Fetched from openrouter.ai/api/v1/models — update if they change. Unknown
# models fall back to the blended HERMIX_LLM_COST_PER_MTOK estimate.
MODEL_PRICES_PER_MTOK = {
    "qwen/qwen3.7-max":        (1.475, 4.425),
    "moonshotai/kimi-k3":      (3.00, 15.00),
    "anthropic/claude-opus-5": (5.00, 25.00),
    "openai/gpt-5.6-sol":      (5.00, 30.00),
    "google/gemini-3.6-flash": (1.50, 7.50),
}


def price_for(model: str):
    """(prompt, completion) USD per million tokens for a model."""
    if model in MODEL_PRICES_PER_MTOK:
        return MODEL_PRICES_PER_MTOK[model]
    blended = cost_per_mtok()
    return (blended, blended)


def cost_of(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Actual USD for this usage at the model's real price."""
    p_in, p_out = price_for(model)
    return (float(prompt_tokens) * p_in + float(completion_tokens) * p_out) / 1_000_000.0

# Payload caps (defense in depth — the plugin builds small prompts).
MAX_MESSAGES = 40
MAX_TOTAL_CHARS = 32_000
MAX_TOKENS = 1024               # cap on the completion length
ROLES = ("system", "user", "assistant")

# Outbound timeout; a single attempt (no retry storm against a paid upstream).
TIMEOUT_SECONDS = 60.0

# Budget + cost env defaults.
DEFAULT_DAILY_TOKENS = 150_000        # per-agent, per UTC day (prompt+completion)
DEFAULT_GLOBAL_DAILY_TOKENS = 3_000_000   # whole hub, per UTC day
# Sized from measured usage: ~2,300 tokens per completed dig (both envoys, both
# findings notes, the judge). At 100 agents x 8 thread-opens/day that is ~1.84M,
# so 3M leaves real headroom. Raising this raises the operator's bill — roughly
# $3.40 per million tokens at qwen3.7-max prices.
DEFAULT_COST_PER_MTOK = 0.30          # blended $/million tokens, for admin est.


def _key() -> str:
    return (compat_env.env("HERMIX_OPENROUTER_KEY") or "").strip()


def is_configured() -> bool:
    """True iff an operator OpenRouter key is set (else the proxy fails closed)."""
    return bool(_key())


def model_for(purpose: str, selected: str = None) -> str:
    """Resolve the model for a purpose. Priority:

    1. a per-purpose env var (ops override, highest — pins one purpose),
    2. ``selected`` — the model chosen in the admin dashboard (persisted in db),
    3. ``DEFAULT_MODEL``.
    """
    env_name = {
        "envoy": "HERMIX_LLM_MODEL_ENVOY",
        "judge": "HERMIX_LLM_MODEL_JUDGE",
        "refresh": "HERMIX_LLM_MODEL_REFRESH",
    }.get(purpose)
    if env_name:
        val = (os.environ.get(env_name) or "").strip()
        if val:
            return val
    if selected:
        return selected
    return DEFAULT_MODEL


def models_by_purpose(selected: str = None) -> dict:
    return {p: model_for(p, selected) for p in PURPOSES}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def daily_token_cap() -> int:
    return _env_int("HERMIX_LLM_DAILY_TOKENS", DEFAULT_DAILY_TOKENS)


def global_token_cap() -> int:
    return _env_int("HERMIX_LLM_GLOBAL_DAILY_TOKENS", DEFAULT_GLOBAL_DAILY_TOKENS)


def cost_per_mtok() -> float:
    try:
        return float(compat_env.env("HERMIX_LLM_COST_PER_MTOK", DEFAULT_COST_PER_MTOK))
    except (TypeError, ValueError):
        return DEFAULT_COST_PER_MTOK


def validate(messages, purpose: str) -> list:
    """Whitelist the purpose + messages and enforce the payload caps.

    Returns a cleaned ``[{role, content}]`` list. Raises ``400`` for a malformed
    body and ``413`` when the message count or total content exceeds the caps.
    """
    if purpose not in PURPOSES:
        raise HTTPException(status_code=400, detail="invalid purpose")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages required")
    if len(messages) > MAX_MESSAGES:
        raise HTTPException(status_code=413, detail="too many messages")
    cleaned = []
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            raise HTTPException(status_code=400, detail="malformed message")
        role = m.get("role")
        content = m.get("content")
        if role not in ROLES or not isinstance(content, str):
            raise HTTPException(status_code=400, detail="malformed message")
        total += len(content)
        if total > MAX_TOTAL_CHARS:
            raise HTTPException(status_code=413, detail="message content too large")
        cleaned.append({"role": role, "content": content})
    return cleaned


def complete(messages: list, purpose: str, selected: str = None) -> dict:
    """Call OpenRouter with the operator key and return text + token usage.

    ``selected`` is the dashboard-chosen model (from db). Assumes ``messages`` is
    already validated. Raises ``502`` on any transport error, non-2xx upstream
    status, or unparseable response — the detail is kept short and never contains
    the key or the raw upstream body.
    """
    model = model_for(purpose, selected)
    headers = {
        "Authorization": f"Bearer {_key()}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages, "max_tokens": MAX_TOKENS}
    try:
        resp = httpx.post(OPENROUTER_URL, json=payload, headers=headers,
                          timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError:
        # Connection/timeout — never echo the exception (could carry the URL/key).
        raise HTTPException(status_code=502, detail="llm upstream unreachable")
    if resp.status_code != 200:
        # Redact: surface only the status code, never the upstream body/key.
        raise HTTPException(
            status_code=502,
            detail=f"llm upstream error ({resp.status_code})",
        )
    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
    except (ValueError, KeyError, IndexError, TypeError):
        raise HTTPException(status_code=502, detail="llm upstream malformed response")
    return {
        "text": text if isinstance(text, str) else str(text),
        "model": model,
        "tokens": {"prompt": prompt_tokens, "completion": completion_tokens},
    }
