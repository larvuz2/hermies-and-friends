"""Operator-paid LLM inference proxy for the Hermies hub.

Plugin users never bring their own LLM key for network features (envoy/judge/
refresh). Instead the hub proxies those completions to OpenRouter using the
OPERATOR's key (env ``HERMIES_OPENROUTER_KEY``). This module owns the outbound
call and its guards; metering + budgets live in ``db.py`` and the ``/v1/llm/*``
route in ``app.py``.

Fail closed: with no key configured the caller sees ``503`` and no request ever
leaves the box. Upstream failures surface as ``502`` with a short, redacted
detail — the operator's key and the full upstream body are never leaked.
"""
import os

import httpx
from fastapi import HTTPException

# OpenRouter chat-completions endpoint (OpenAI-compatible schema).
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Purposes the plugin drives, each routable to its own model via env.
PURPOSES = ("envoy", "judge", "refresh")
DEFAULT_MODEL = "openai/gpt-oss-120b"

# Payload caps (defense in depth — the plugin builds small prompts).
MAX_MESSAGES = 40
MAX_TOTAL_CHARS = 32_000
MAX_TOKENS = 1024               # cap on the completion length
ROLES = ("system", "user", "assistant")

# Outbound timeout; a single attempt (no retry storm against a paid upstream).
TIMEOUT_SECONDS = 60.0

# Budget + cost env defaults.
DEFAULT_DAILY_TOKENS = 150_000        # per-agent, per UTC day (prompt+completion)
DEFAULT_GLOBAL_DAILY_TOKENS = 2_000_000   # whole hub, per UTC day
DEFAULT_COST_PER_MTOK = 0.30          # blended $/million tokens, for admin est.


def _key() -> str:
    return (os.environ.get("HERMIES_OPENROUTER_KEY") or "").strip()


def is_configured() -> bool:
    """True iff an operator OpenRouter key is set (else the proxy fails closed)."""
    return bool(_key())


def model_for(purpose: str) -> str:
    """Resolve the model for a purpose from env, falling back to the default.

    A single cheap default is fine; the per-purpose envs allow tuning without a
    code change (envoy/judge/refresh can each point at a different model).
    """
    env_name = {
        "envoy": "HERMIES_LLM_MODEL_ENVOY",
        "judge": "HERMIES_LLM_MODEL_JUDGE",
        "refresh": "HERMIES_LLM_MODEL_REFRESH",
    }.get(purpose)
    if env_name:
        val = (os.environ.get(env_name) or "").strip()
        if val:
            return val
    return DEFAULT_MODEL


def models_by_purpose() -> dict:
    return {p: model_for(p) for p in PURPOSES}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def daily_token_cap() -> int:
    return _env_int("HERMIES_LLM_DAILY_TOKENS", DEFAULT_DAILY_TOKENS)


def global_token_cap() -> int:
    return _env_int("HERMIES_LLM_GLOBAL_DAILY_TOKENS", DEFAULT_GLOBAL_DAILY_TOKENS)


def cost_per_mtok() -> float:
    try:
        return float(os.environ.get("HERMIES_LLM_COST_PER_MTOK", DEFAULT_COST_PER_MTOK))
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


def complete(messages: list, purpose: str) -> dict:
    """Call OpenRouter with the operator key and return text + token usage.

    Assumes ``messages`` is already validated. Raises ``502`` on any transport
    error, non-2xx upstream status, or unparseable response — the detail is kept
    short and never contains the key or the raw upstream body.
    """
    model = model_for(purpose)
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
