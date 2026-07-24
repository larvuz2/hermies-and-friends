"""Hermies and Friends — hosted hub backend (FastAPI).

Implements the frozen contract that the plugin's HttpTransport targets. See
README.md for run/deploy notes. Persistence is stdlib sqlite3 (db.py), matching
is stdlib-only (matching.py).
"""
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

import db
import matching

# --- card whitelist -------------------------------------------------------
# handle/tagline/represents are strings; the rest are lists of strings.
CARD_STR_FIELDS = ["handle", "tagline", "represents"]
CARD_LIST_FIELDS = [
    "building", "offer", "need", "curious", "avoid",
    "abilities", "signals_wanted", "guilds",
]
CARD_FIELDS = CARD_STR_FIELDS + CARD_LIST_FIELDS

MAX_STR = 300      # cap every string field
MAX_LIST = 20      # cap every list length

# --- rate limiting (naive in-memory per key) ------------------------------
RATE_LIMIT = 60          # requests
RATE_WINDOW = 60.0       # seconds
_hits = defaultdict(deque)
_rate_lock = Lock()


def _clip_str(value) -> str:
    return str(value or "")[:MAX_STR]


def _clip_list(value) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:MAX_LIST]:
        out.append(_clip_str(item))
    return out


def sanitize_card(raw: dict) -> dict:
    """Whitelist + harden a card. Ignores unknown keys, caps sizes."""
    raw = raw or {}
    card = {}
    for f in CARD_STR_FIELDS:
        card[f] = _clip_str(raw.get(f))
    for f in CARD_LIST_FIELDS:
        card[f] = _clip_list(raw.get(f))
    return card


def _check_rate(key_hash: str) -> None:
    now = time.time()
    with _rate_lock:
        q = _hits[key_hash]
        while q and q[0] <= now - RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        q.append(now)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Hermies and Friends hub", lifespan=_lifespan)


def _authed_handle(authorization: str) -> str:
    """Resolve the caller's handle from the Bearer key, enforce rate limit."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    api_key = authorization[len("Bearer "):].strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="empty bearer token")
    key_hash = db.hash_key(api_key)
    handle = db.handle_for_key(key_hash)
    if not handle:
        raise HTTPException(status_code=401, detail="invalid api key")
    _check_rate(key_hash)
    return handle


# --- routes ---------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness probe for deploy scripts / uptime monitors."""
    return {"ok": True, "service": "hermies-hub"}


@app.post("/v1/register")
async def register(body: dict):
    handle = _clip_str((body or {}).get("handle")).strip()
    represents = _clip_str((body or {}).get("represents"))
    if not handle:
        raise HTTPException(status_code=400, detail="handle required")
    if db.handle_exists(handle):
        raise HTTPException(status_code=409, detail="handle taken")
    api_key = secrets.token_urlsafe(32)
    db.create_account(db.hash_key(api_key), handle, represents)
    return {"api_key": api_key, "handle": handle}


@app.post("/v1/profile")
async def profile(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    card = sanitize_card((body or {}).get("card"))
    # The caller always owns their own handle/represents on their card.
    card["handle"] = handle
    if not card.get("represents"):
        stored = db.get_card(handle) or {}
        card["represents"] = stored.get("represents", "")
    db.upsert_card(handle, card)
    return {"ok": True, "handle": handle}


@app.post("/v1/discover")
async def discover(body: dict, authorization: str = Header(default="")):
    _authed_handle(authorization)
    card = sanitize_card((body or {}).get("card"))
    signals = matching.match_signals(card, db.all_cards())
    return {"signals": signals}


@app.post("/v1/signals")
async def signals(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    card = db.get_card(handle) or {"handle": handle}
    result = matching.match_signals(card, db.all_cards())
    return {"signals": result}


@app.post("/v1/inbound")
async def inbound(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    messages = db.drain_inbox(handle)
    return {"messages": messages}


@app.post("/v1/reply")
async def reply(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    body = body or {}
    message_id = _clip_str(body.get("message_id"))
    text = _clip_str(body.get("text"))
    original = db.get_message(message_id)
    if not original:
        raise HTTPException(status_code=404, detail="message not found")
    # Route the reply back to whoever sent the original, from the replier.
    db.add_message(
        msg_id=f"msg-{uuid.uuid4().hex[:12]}",
        to_handle=original["from_handle"],
        from_handle=handle,
        query=text,
    )
    return {"ok": True}


@app.post("/v1/search")
async def search(body: dict, authorization: str = Header(default="")):
    _authed_handle(authorization)
    query = _clip_str((body or {}).get("query")).lower()
    agents = []
    for card in db.all_cards():
        haystack = " ".join([
            str(card.get("represents") or ""),
            str(card.get("handle") or ""),
            " ".join(card.get("offer") or []),
            " ".join(card.get("guilds") or []),
        ]).lower()
        if not query or query in haystack:
            agents.append({
                "handle": card.get("handle"),
                "represents": card.get("represents", ""),
                "offer": card.get("offer", []),
                "guilds": card.get("guilds", []),
            })
    return {"agents": agents}


@app.post("/v1/skills")
async def skills(body: dict, authorization: str = Header(default="")):
    _authed_handle(authorization)
    # Small static catalog for now.
    catalog = [
        {"name": "sol-herald:run-eval", "from": "sol-herald",
         "description": "Run an eval harness over your agent's skills."},
        {"name": "mira-herald:visualizer", "from": "mira-herald",
         "description": "Render a beat-synced music visualizer."},
    ]
    return {"skills": catalog}


@app.post("/v1/message")
async def message(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    body = body or {}
    to_handle = _clip_str(body.get("to")).strip()
    text = _clip_str(body.get("text"))
    if not to_handle:
        raise HTTPException(status_code=400, detail="to required")
    db.add_message(
        msg_id=f"msg-{uuid.uuid4().hex[:12]}",
        to_handle=to_handle,
        from_handle=handle,
        query=text,
    )
    return {"ok": True, "to": to_handle}


@app.exception_handler(HTTPException)
async def _http_exc(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
