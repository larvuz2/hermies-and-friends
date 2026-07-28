"""Hermies and Friends — hosted hub backend (FastAPI).

Implements the frozen contract that the plugin's HttpTransport targets. See
README.md for run/deploy notes. Persistence is stdlib sqlite3 (db.py), matching
is stdlib-only (matching.py).
"""
import base64
import contextvars
import html
import json
import logging
import os
import pathlib
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import engine as engine_mod
import llm_proxy
import matching

log = logging.getLogger("hermies.app")

# Process start, used for the admin "uptime" tile.
_START = time.time()

# --- semantic matching engine v2 -----------------------------------------
MATCH_TOP_K = 20               # hard cap on signals returned
DEFAULT_MATCH_FLOOR = 2.0      # drop matches scoring below this (0..10 scale)

# The live engine (built at startup in the lifespan). Module-global so every
# request handler and the admin page share the one in-memory index.
_engine = None

# Rolling match latency samples (seconds) for the admin dashboard.
_match_latencies = deque(maxlen=200)
_latency_lock = Lock()


def _match_floor() -> float:
    try:
        return float(os.environ.get("HERMIES_MATCH_FLOOR", DEFAULT_MATCH_FLOOR))
    except (TypeError, ValueError):
        return DEFAULT_MATCH_FLOOR


def _record_latency(seconds: float) -> None:
    with _latency_lock:
        _match_latencies.append(seconds)


def _avg_match_latency_ms() -> float:
    with _latency_lock:
        if not _match_latencies:
            return 0.0
        return 1000.0 * sum(_match_latencies) / len(_match_latencies)


def _engine_match_signals(card: dict, exclude_handle: str) -> list:
    """Run the semantic engine and shape results into the frozen SIGNAL list.

    Shape is unchanged ({kind, agent, why, score}); ``components`` is added as an
    additive, non-breaking extra key. Floors below HERMIES_MATCH_FLOOR are
    dropped, results capped at MATCH_TOP_K, and latency sampled for the admin.
    """
    floor = _match_floor()
    t0 = time.perf_counter()
    results = _engine.match(card, exclude_handle=exclude_handle, top_k=MATCH_TOP_K)
    _record_latency(time.perf_counter() - t0)
    signals = []
    for r in results:
        if r["score"] < floor:
            continue
        signals.append({
            "kind": "match",
            "agent": r["agent"],
            "why": r["why"],
            "score": r["score"],
            "components": r["components"],
        })
    return signals

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

# --- threaded conversations -----------------------------------------------
THREAD_KINDS = ("dig", "ask", "reveal_request")
THREAD_MAX_TURNS = 12          # total messages allowed per thread
THREAD_TEXT_MAX = 4000         # per-message text cap
THREAD_SUBJECT_MAX = 200       # subject cap
THREAD_OPENS_PER_DAY = 20      # per-agent thread-open abuse guard (UTC day)


def _clean_text(value, limit: int) -> str:
    """Cap length and strip control chars (defense in depth). Keeps \\n and \\t."""
    s = str(value if value is not None else "")
    s = "".join(ch for ch in s if ch in "\n\t" or ord(ch) >= 32)
    return s[:limit]

# --- rate limiting (naive in-memory per key) ------------------------------
RATE_LIMIT = 60          # requests
RATE_WINDOW = 60.0       # seconds
_hits = defaultdict(deque)
_rate_lock = Lock()

# Public-launch hardening: throttle account creation per client IP so a single
# source cannot spray registrations. In-memory + process-local (single worker).
DEFAULT_REGISTER_MAX = 20
REGISTER_WINDOW = 3600.0     # seconds (per hour)
_reg_hits = defaultdict(deque)
_reg_lock = Lock()


def _register_max() -> int:
    try:
        return int(os.environ.get("HERMIES_REGISTER_MAX_PER_HOUR",
                                  DEFAULT_REGISTER_MAX))
    except (TypeError, ValueError):
        return DEFAULT_REGISTER_MAX


def _client_ip(request) -> str:
    """The real client IP.

    We run behind Caddy, so ``request.client.host`` is the PROXY (127.0.0.1) for
    every request — which would turn the per-IP registration throttle into a
    global cap and block real signups. Prefer the first hop in X-Forwarded-For,
    which our own trusted proxy sets.
    """
    xff = ""
    try:
        xff = request.headers.get("x-forwarded-for", "") or ""
    except Exception:
        xff = ""
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    try:
        return request.client.host if request.client else "unknown"
    except Exception:
        return "unknown"


def _check_register_rate(ip: str) -> None:
    now = time.time()
    limit = _register_max()
    with _reg_lock:
        q = _reg_hits[ip]
        while q and q[0] <= now - REGISTER_WINDOW:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(
                status_code=429, detail="registration rate limit exceeded")
        q.append(now)


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
    global _engine
    db.init_db()
    # Build the semantic index once at cold start (loads persisted embeddings +
    # cards from sqlite; re-encodes anything missing so upgrades self-heal).
    _engine = engine_mod.build_engine(db)
    log.warning(
        "hermies engine ready: mode=%s model=%s indexed_cards=%d floor=%.1f",
        _engine.mode, _engine.model_name, _engine.card_count, _match_floor(),
    )
    yield


app = FastAPI(title="Hermies and Friends hub", lifespan=_lifespan)

# Version telemetry rides on headers the plugin sends. Captured once here rather
# than threaded through every route signature.
_req_versions = contextvars.ContextVar("hermies_versions", default=("", ""))


@app.middleware("http")
async def _capture_client_versions(request: Request, call_next):
    _req_versions.set((
        (request.headers.get("x-hermies-version") or "")[:40],
        (request.headers.get("x-hermies-disk") or "")[:40],
    ))
    return await call_next(request)


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
    # Presence + request metrics for accepted, authenticated requests.
    db.touch_account(handle)
    db.bump_stat("requests")
    # Version telemetry rides along on the headers the plugin already sends, so
    # the operator can see which agents are running which release.
    active, disk = _req_versions.get()
    if active or disk:
        db.set_versions(handle, active, disk)
    return handle


# --- routes ---------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness probe for deploy scripts / uptime monitors."""
    return {"ok": True, "service": "hermies-hub"}


@app.post("/v1/register")
async def register(body: dict, request: Request):
    _check_register_rate(_client_ip(request))
    handle = _clip_str((body or {}).get("handle")).strip()
    represents = _clip_str((body or {}).get("represents"))
    if not handle:
        raise HTTPException(status_code=400, detail="handle required")
    if db.handle_exists(handle):
        raise HTTPException(status_code=409, detail="handle taken")
    api_key = secrets.token_urlsafe(32)
    db.create_account(db.hash_key(api_key), handle, represents)
    db.bump_stat("registrations")
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
    # Encode + (re)index this card in the live semantic engine. last_seen is now
    # (the caller just authenticated), which also refreshes their presence.
    if _engine is not None:
        _engine.upsert_card(handle, card, last_seen_ts=time.time())
    return {"ok": True, "handle": handle}


@app.post("/v1/discover")
async def discover(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    card = sanitize_card((body or {}).get("card"))
    signals = _engine_match_signals(card, exclude_handle=handle)
    db.bump_stat("signals_served", len(signals))
    return {"signals": signals}


@app.post("/v1/signals")
async def signals(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    card = db.get_card(handle) or {"handle": handle}
    result = _engine_match_signals(card, exclude_handle=handle)
    db.bump_stat("signals_served", len(result))
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
    db.bump_stat("messages_routed")
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
    db.bump_stat("messages_routed")
    return {"ok": True, "to": to_handle}


# --- live client configuration --------------------------------------------
# Every connected plugin polls this. It is how the network improves WITHOUT any
# user ever running a command: tuning + behaviour text ship from here in
# minutes. Only changes that need new Python require a code release (which the
# plugin then self-updates in the background — see updater.py client-side).
CLIENT_CONFIG_DEFAULT = pathlib.Path(__file__).resolve().parent / "client_config.json"


def _client_config() -> dict:
    """Read the served config fresh each time so an operator edit is live
    immediately (the file is tiny; no caching games)."""
    path = pathlib.Path(os.environ.get("HERMIES_CLIENT_CONFIG_FILE")
                        or CLIENT_CONFIG_DEFAULT)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("client config unreadable (%s): %s", path, exc)
        return {"version": 0, "knobs": {}, "notice": None}
    if not isinstance(data, dict):
        return {"version": 0, "knobs": {}, "notice": None}
    knobs = data.get("knobs")
    switches = data.get("switches")
    release = data.get("release")
    return {
        "version": data.get("version", 0),
        "knobs": knobs if isinstance(knobs, dict) else {},
        "switches": switches if isinstance(switches, dict) else {},
        "release": release if isinstance(release, dict) else {},
        "notice": data.get("notice"),
        # Clients compare this against their own checkout to decide whether to
        # self-update. Bump it in the file when a code release should roll out.
        "plugin_revision": data.get("plugin_revision"),
    }


def _switch(name: str, default: bool = True) -> bool:
    """A kill switch's current value. Read fresh so flipping one in the file
    takes effect on the very next request — this is the emergency brake."""
    val = _client_config().get("switches", {}).get(name, default)
    return bool(val)


def _require_switch(name: str, what: str) -> None:
    """Hub-side ENFORCEMENT. Client-side checks are a courtesy; this is what
    makes a switch real even for a stale or broken client."""
    if not _switch(name):
        raise HTTPException(status_code=423,
                            detail=f"{what} is temporarily disabled by the operator")


@app.get("/v1/config")
async def client_config(authorization: str = Header(default="")):
    """Live tuning + behaviour text for connected plugins."""
    _authed_handle(authorization)
    return _client_config()


# --- operator-paid LLM proxy ----------------------------------------------
@app.post("/v1/llm/complete")
async def llm_complete(body: dict, authorization: str = Header(default="")):
    """Proxy an envoy/judge/refresh completion to OpenRouter on the operator key.

    Fail closed (503) when unconfigured; enforce per-agent + global daily token
    budgets (429) BEFORE spending on upstream; meter every successful call.
    """
    handle = _authed_handle(authorization)
    _require_switch("inference_enabled", "operator-paid inference")
    if not llm_proxy.is_configured():
        raise HTTPException(status_code=503, detail="llm not configured")
    body = body or {}
    purpose = _clip_str(body.get("purpose")).strip()
    messages = llm_proxy.validate(body.get("messages"), purpose)   # 400 / 413
    # Budget: reject when today's ALREADY-recorded usage is at/over either cap,
    # checked before we spend on the upstream call.
    if (db.llm_tokens_today(handle) >= llm_proxy.daily_token_cap()
            or db.llm_global_tokens_today() >= llm_proxy.global_token_cap()):
        raise HTTPException(status_code=429, detail="llm budget exceeded")
    selected = db.get_setting("llm_model")     # dashboard-chosen model, if any
    result = llm_proxy.complete(messages, purpose, selected)        # 502 on failure
    tok = result["tokens"]
    db.record_llm_usage(handle, tok["prompt"], tok["completion"],
                        model=result.get("model", ""))
    db.bump_stat("llm_calls")
    db.bump_stat("llm_tokens", tok["prompt"] + tok["completion"])
    return result


@app.post("/v1/profile/remove")
async def profile_remove(body: dict, authorization: str = Header(default="")):
    """Opt-out: clear the caller's card + vectors from the db and live engine.

    Account/key stay valid so they can re-publish later. Idempotent.
    """
    handle = _authed_handle(authorization)
    db.delete_card(handle)
    if _engine is not None:
        _engine.remove(handle)     # drops from the index + in-memory + card_vectors
    db.bump_stat("profiles_removed")
    return {"ok": True}


# --- threaded conversations -----------------------------------------------
def _utc_midnight_ts() -> float:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _load_thread_for(thread_id: str, handle: str) -> dict:
    """Fetch a thread the caller participates in, or 404.

    A non-participant (or a missing thread) both yield 404 so the endpoint never
    leaks whether a thread exists to anyone outside it.
    """
    thread = db.get_thread(_clip_str(thread_id))
    if not thread or handle not in (thread["a_handle"], thread["b_handle"]):
        raise HTTPException(status_code=404, detail="thread not found")
    return thread


@app.post("/v1/thread/open")
async def thread_open(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    body = body or {}
    to_handle = _clip_str(body.get("to")).strip()
    kind = _clip_str(body.get("kind")).strip()
    # Emergency brakes, enforced here so a stale client cannot bypass them.
    if kind == "reveal_request":
        _require_switch("reveals_enabled", "contact reveals")
    else:
        _require_switch("digs_enabled", "new agent-to-agent conversations")
    subject = _clean_text(body.get("subject"), THREAD_SUBJECT_MAX)
    if kind not in THREAD_KINDS:
        raise HTTPException(status_code=400, detail="invalid kind")
    if not to_handle:
        raise HTTPException(status_code=400, detail="to required")
    if to_handle == handle:
        raise HTTPException(status_code=400, detail="cannot open a thread with yourself")
    if not db.handle_exists(to_handle):
        raise HTTPException(status_code=404, detail="recipient not found")
    if db.count_thread_opens_since(handle, _utc_midnight_ts()) >= THREAD_OPENS_PER_DAY:
        raise HTTPException(status_code=429, detail="daily thread-open limit exceeded")
    thread_id = f"thr-{uuid.uuid4().hex[:12]}"
    db.create_thread(thread_id, handle, to_handle, kind, subject, time.time())
    db.bump_stat("threads_opened")
    return {"thread_id": thread_id}


@app.post("/v1/thread/send")
async def thread_send(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    body = body or {}
    thread = _load_thread_for(body.get("thread_id"), handle)
    if thread["state"] != "open":
        raise HTTPException(status_code=409, detail="thread is not open")
    turns = db.count_thread_messages(thread["id"])
    if turns >= THREAD_MAX_TURNS:
        # The 13th send exhausts the budget: expire the thread and reject.
        db.set_thread_state(thread["id"], "expired")
        raise HTTPException(status_code=409, detail="thread turn budget exhausted")
    text = _clean_text(body.get("text"), THREAD_TEXT_MAX)
    turn = turns + 1
    db.add_thread_message(thread["id"], turn, handle, text, time.time())
    db.bump_stat("messages_routed")
    return {"ok": True, "turn": turn}


@app.post("/v1/thread/close")
async def thread_close(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    thread = _load_thread_for((body or {}).get("thread_id"), handle)
    if thread["state"] != "open":
        raise HTTPException(status_code=409, detail="thread is not open")
    db.set_thread_state(thread["id"], "concluded")
    return {"ok": True}


@app.post("/v1/thread/list")
async def thread_list(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    threads = []
    for t in db.list_threads_for(handle):
        other = t["b_handle"] if t["a_handle"] == handle else t["a_handle"]
        last_read = (t["a_last_read_turn"] if t["a_handle"] == handle
                     else t["b_last_read_turn"])
        threads.append({
            "thread_id": t["id"],
            "with": other,
            "kind": t["kind"],
            "subject": t["subject"],
            "state": t["state"],
            "turns": db.count_thread_messages(t["id"]),
            "unread": db.count_unread(t["id"], other, last_read),
        })
    return {"threads": threads}


@app.post("/v1/thread/read")
async def thread_read(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    thread = _load_thread_for((body or {}).get("thread_id"), handle)
    messages = db.get_thread_messages(thread["id"])
    # Reading marks everything up to the latest turn as read for this caller.
    if messages:
        db.set_last_read(thread["id"], handle, messages[-1]["turn"])
    return {"messages": messages}


# --- admin dashboard ------------------------------------------------------
def _require_admin(authorization: str) -> None:
    """HTTP Basic gate for admin surfaces. Fails closed.

    503 if HERMIES_ADMIN_PASSWORD is unset (admin disabled — never a default
    password). 401 (with a Basic challenge) on missing/wrong credentials.
    """
    password = os.environ.get("HERMIES_ADMIN_PASSWORD")
    if not password:
        raise HTTPException(status_code=503, detail="admin disabled")
    ok = False
    if authorization and authorization.startswith("Basic "):
        try:
            decoded = base64.b64decode(authorization[len("Basic "):]).decode("utf-8")
            user, _, pw = decoded.partition(":")
            ok = secrets.compare_digest(user, "admin") and \
                secrets.compare_digest(pw, password)
        except Exception:
            ok = False
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="hermies-admin"'},
        )


def _humanize(iso: str) -> str:
    """Render an ISO timestamp as a compact relative age, e.g. '3m ago'."""
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _fmt_uptime(seconds: float) -> str:
    secs = int(seconds)
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    mins, secs = divmod(secs, 60)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m {secs}s"


def _gather_stats() -> dict:
    """Collect every number the dashboard / stats API needs from real data."""
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    online_cutoff = (now - timedelta(minutes=10)).isoformat()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    stats_map = db.daily_stats_map()
    empty = {"requests": 0, "registrations": 0, "messages_routed": 0,
             "signals_served": 0, "threads_opened": 0, "llm_calls": 0,
             "llm_tokens": 0, "profiles_removed": 0}
    today_row = {**empty, **stats_map.get(today, {})}

    # 14-day window (oldest first), filling gaps with zeros.
    daily = []
    for i in range(13, -1, -1):
        day = (now.date() - timedelta(days=i)).isoformat()
        row = {**empty, **stats_map.get(day, {})}
        daily.append({
            "date": day,
            "requests": row["requests"],
            "messages_routed": row["messages_routed"],
            "signals_served": row["signals_served"],
            "registrations": row["registrations"],
        })

    return {
        "total_agents": db.count_accounts(),
        "online_now": db.count_since(online_cutoff),
        "active_today": db.count_since(midnight),
        "messages_routed_today": today_row["messages_routed"],
        "signals_served_today": today_row["signals_served"],
        "requests_today": today_row["requests"],
        "registrations_today": today_row["registrations"],
        "db_size_bytes": db.db_size_bytes(),
        "uptime_seconds": time.time() - _START,
        "accounts": db.all_accounts_with_cards(),
        "daily": daily,
        "conversations": {
            "threads_opened_today": today_row["threads_opened"],
            "open_threads": db.count_open_threads(),
            "sends_today": db.count_thread_messages_since(
                now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),
            "total_threads": db.count_threads_total(),
            "recent": db.admin_list_threads(60),
        },
        "switches": _client_config().get("switches", {}),
        "release": _client_config().get("release", {}),
        "versions": db.version_rollup(),
        "engine": {
            "mode": _engine.mode if _engine else "unbuilt",
            "model": _engine.model_name if _engine else "?",
            "indexed_cards": _engine.card_count if _engine else 0,
            "avg_match_latency_ms": _avg_match_latency_ms(),
        },
        "llm": _gather_llm_stats(),
    }


def _gather_llm_stats() -> dict:
    """Operator-paid LLM section for the admin dashboard (usage + est. cost)."""
    usage = db.llm_usage_today()
    tokens_today = usage["prompt_tokens"] + usage["completion_tokens"]
    tokens_month = db.llm_tokens_month()
    rate = llm_proxy.cost_per_mtok()
    selected = db.get_setting("llm_model")
    active = selected or llm_proxy.DEFAULT_MODEL

    # --- what the operator is ACTUALLY paying, at each model's real price ---
    by_model_today = db.llm_usage_by_model(1)
    for row in by_model_today:
        row["cost"] = llm_proxy.cost_of(row["model"], row["prompt_tokens"],
                                        row["completion_tokens"])
    real_cost_today = sum(r["cost"] for r in by_model_today)

    week = db.llm_usage_by_model_daily(7)
    per_day = {}
    for row in week:
        per_day.setdefault(row["date_utc"], 0.0)
        per_day[row["date_utc"]] += llm_proxy.cost_of(
            row["model"], row["prompt_tokens"], row["completion_tokens"])
    days_seen = max(1, len(per_day))
    avg_daily = sum(per_day.values()) / days_seen
    active_agents = db.count_since(
        (datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    p_in, p_out = llm_proxy.price_for(active)

    return {
        "configured": llm_proxy.is_configured(),
        "models": llm_proxy.models_by_purpose(selected),
        "selected_model": active,
        "top_models": llm_proxy.TOP_MODELS,
        "active_price_in": p_in,
        "active_price_out": p_out,
        "by_model_today": by_model_today,
        "real_cost_today": real_cost_today,
        "avg_daily_cost": avg_daily,
        "projected_monthly": avg_daily * 30.0,
        "active_agents_24h": active_agents,
        "cost_per_agent_today": (real_cost_today / active_agents) if active_agents else 0.0,
        "daily_cost_series": sorted(per_day.items()),
        "daily_cap": llm_proxy.daily_token_cap(),
        "global_cap": llm_proxy.global_token_cap(),
        "cost_per_mtok": rate,
        "calls_today": usage["calls"],
        "prompt_tokens_today": usage["prompt_tokens"],
        "completion_tokens_today": usage["completion_tokens"],
        "tokens_today": tokens_today,
        "tokens_month": tokens_month,
        "cost_today": tokens_today / 1_000_000.0 * rate,
        "cost_month": tokens_month / 1_000_000.0 * rate,
        "top": db.top_llm_consumers_today(5),
    }


def _e(value) -> str:
    """HTML-escape any value. Cards are untrusted user input — escape all of it."""
    return html.escape(str(value if value is not None else ""))


def _trunc(value, limit: int = 60) -> str:
    s = str(value if value is not None else "")
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _ago(ts) -> str:
    """Short relative time from a float epoch (thread created_ts)."""
    try:
        delta = time.time() - float(ts)
    except (TypeError, ValueError):
        return "?"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _agent_link(handle) -> str:
    """A link to the per-agent card detail page (handle text is escaped)."""
    from urllib.parse import quote
    h = str(handle or "")
    return f'<a href="/admin/agent/{quote(h, safe="")}">{_e(h)}</a>'


# Human labels for the thread kinds shown in the connections table.
_KIND_LABEL = {
    "dig": "match · dig",
    "ask": "discreet ask",
    "reveal_request": "reveal request",
}


def _render_admin(stats: dict) -> str:
    tiles = [
        ("Total agents", str(stats["total_agents"])),
        ("Online now", str(stats["online_now"])),
        ("Active today", str(stats["active_today"])),
        ("Messages routed today", str(stats["messages_routed_today"])),
        ("Signals served today", str(stats["signals_served_today"])),
        ("Requests today", str(stats["requests_today"])),
        ("DB size", _fmt_bytes(stats["db_size_bytes"])),
        ("Uptime", _fmt_uptime(stats["uptime_seconds"])),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="num">{_e(v)}</div>'
        f'<div class="lbl">{_e(label)}</div></div>'
        for label, v in tiles
    )

    rows = []
    for acct in stats["accounts"]:
        card = acct.get("card") or {}
        offer = ", ".join(card.get("offer") or [])
        need = ", ".join(card.get("need") or [])
        guilds = ", ".join(card.get("guilds") or [])
        rows.append(
            "<tr>"
            f"<td>{_agent_link(acct['handle'])}</td>"
            f"<td>{_e(_trunc(acct['represents'], 50))}</td>"
            f"<td>{_e(_humanize(acct['last_seen']))}</td>"
            f"<td class=\"r\">{_e(acct['request_count'])}</td>"
            f"<td>{_e(acct.get('version_active') or '—')}"
            + ("" if (acct.get('version_disk') or acct.get('version_active'))
                     == acct.get('version_active')
               else f" <span class=muted>(disk {_e(acct.get('version_disk'))})</span>")
            + "</td>"
            f"<td>{_e(_trunc(offer))}</td>"
            f"<td>{_e(_trunc(need))}</td>"
            f"<td>{_e(_trunc(guilds))}</td>"
            "</tr>"
        )
    agents_rows = "".join(rows) or (
        '<tr><td colspan="8" class="muted">no agents yet</td></tr>'
    )

    eng = stats["engine"]
    engine_mode = eng["mode"]
    mode_label = ("semantic (fastembed)" if engine_mode == "fastembed"
                  else "fallback (hashing n-gram)" if engine_mode == "fallback"
                  else engine_mode)
    engine_rows = "".join(
        "<tr>"
        f"<td>{_e(label)}</td><td>{_e(value)}</td>"
        "</tr>"
        for label, value in [
            ("Mode", mode_label),
            ("Model", eng["model"]),
            ("Indexed cards", eng["indexed_cards"]),
            ("Avg match latency", f"{eng['avg_match_latency_ms']:.1f} ms"),
        ]
    )

    # --- kill switches + release rollout -----------------------------------
    sw = stats.get("switches") or {}
    rel = stats.get("release") or {}
    sw_rows = "".join(
        "<tr>"
        f"<td>{_e(k)}</td>"
        f"<td>{'<b>ON</b>' if v else '<b style=color:#ff8080>DISABLED</b>'}</td>"
        "</tr>"
        for k, v in sorted(sw.items())
    ) or '<tr><td colspan="2" class="muted">no switches configured</td></tr>'
    ver_rows = "".join(
        f"<tr><td>{_e(r['version_active'] or 'unknown')}</td>"
        f"<td class=\"r\">{_e(r['agents'])}</td></tr>"
        for r in (stats.get("versions") or [])
    ) or '<tr><td colspan="2" class="muted">no agents yet</td></tr>'
    release_html = (
        '<h2>Releases &amp; switches</h2>'
        f'<p class="muted">Desired version <code>{_e(rel.get("version", "?"))}</code> '
        f'({_e(rel.get("channel", "stable"))}), rolling out to '
        f'<b>{_e(rel.get("rollout_percentage", 100))}%</b> of agents. '
        'Edit <code>backend/client_config.json</code> — agents pick it up within '
        'the hour, no restarts.</p>'
        '<h3>Kill switches (hub-enforced)</h3><div class="wrap"><table>'
        f'<tbody>{sw_rows}</tbody></table></div>'
        '<h3>Agents by running version</h3><div class="wrap"><table>'
        '<thead><tr><th>Active version</th><th class="r">Agents</th></tr></thead>'
        f'<tbody>{ver_rows}</tbody></table></div>'
    )

    conv = stats["conversations"]
    conv_rows = "".join(
        "<tr>"
        f"<td>{_e(label)}</td><td class=\"r\">{_e(value)}</td>"
        "</tr>"
        for label, value in [
            ("Total connections (all time)", conv["total_threads"]),
            ("Opened today", conv["threads_opened_today"]),
            ("Currently open", conv["open_threads"]),
            ("Messages exchanged today", conv["sends_today"]),
        ]
    )

    # Who matched with who — every agent-to-agent connection, newest first.
    match_rows = "".join(
        "<tr>"
        f"<td>{_agent_link(m['a_handle'])} &harr; {_agent_link(m['b_handle'])}</td>"
        f"<td>{_e(_KIND_LABEL.get(m['kind'], m['kind']))}</td>"
        f"<td>{_e(_trunc(m['subject'], 70))}</td>"
        f"<td>{_e(m['state'])}</td>"
        f"<td class=\"r\">{_e(m['turns'])}</td>"
        f"<td>{_e(_ago(m['created_ts']))}</td>"
        "</tr>"
        for m in conv["recent"]
    ) or '<tr><td colspan="6" class="muted">no connections yet</td></tr>'

    llm = stats["llm"]
    if not llm["configured"]:
        llm_section = (
            '<p class="muted"><b>LLM: not configured</b> — set '
            '<code>HERMIES_OPENROUTER_KEY</code> to enable operator-paid '
            'envoy/judge/refresh inference. All <code>/v1/llm/complete</code> '
            'calls currently fail closed (503).</p>'
        )
    else:
        rate = llm["cost_per_mtok"]
        llm_rows = "".join(
            "<tr>"
            f"<td>{_e(label)}</td><td class=\"r\">{_e(value)}</td>"
            "</tr>"
            for label, value in [
                ("Calls today", llm["calls_today"]),
                ("Prompt tokens today", llm["prompt_tokens_today"]),
                ("Completion tokens today", llm["completion_tokens_today"]),
                ("Tokens today", llm["tokens_today"]),
                ("Est. cost today", f"${llm['cost_today']:.4f}"),
                ("Est. cost this month", f"${llm['cost_month']:.4f}"),
                ("Blended rate", f"${rate:.2f} / M tokens"),
                ("Per-agent daily cap", f"{llm['daily_cap']} tokens"),
                ("Global daily cap", f"{llm['global_cap']} tokens"),
            ]
        )
        model_rows = "".join(
            "<tr>"
            f"<td>{_e(purpose)}</td><td>{_e(llm['models'][purpose])}</td>"
            "</tr>"
            for purpose in ("envoy", "judge", "refresh")
        )
        top_rows = "".join(
            "<tr>"
            f"<td>{_e(row['handle'])}</td><td class=\"r\">{_e(row['tokens'])}</td>"
            "</tr>"
            for row in llm["top"]
        ) or '<tr><td colspan="2" class="muted">no usage today</td></tr>'

        # Model picker — a GET form so no extra form-parsing dependency is needed.
        sel = llm["selected_model"]
        options = "".join(
            f'<option value="{_e(mid)}"{" selected" if mid == sel else ""}>'
            f'{_e(label)}</option>'
            for mid, label in llm["top_models"]
        )
        picker = (
            '<h3>Active model</h3>'
            f'<p class="muted">The network\'s thinking (envoy / judge / refresh) '
            f'currently runs on <code>{_e(sel)}</code>. Pick another and it '
            'applies immediately to new calls.</p>'
            '<form method="get" action="/admin/model" '
            'style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
            f'<select name="model" style="padding:7px 10px;background:#171a21;'
            'color:#e6e8ec;border:1px solid #23262e;border-radius:6px;'
            f'font-size:13px">{options}</select>'
            '<button type="submit" style="padding:7px 14px;background:#2f6df6;'
            'color:#fff;border:0;border-radius:6px;font-size:13px;cursor:pointer">'
            'Set model</button>'
            '</form>'
            '<p class="muted" style="font-size:12px;margin-top:6px">Per-purpose '
            'env vars (<code>HERMIES_LLM_MODEL_ENVOY</code> etc.) still override '
            'this if set.</p>'
        )

        # --- what you're covering, at the ACTIVE model's real price ---------
        model_cost_rows = "".join(
            "<tr>"
            f"<td>{_e(r['model'])}</td>"
            f"<td class=\"r\">{_e(r['calls'])}</td>"
            f"<td class=\"r\">{_e(r['prompt_tokens'])}</td>"
            f"<td class=\"r\">{_e(r['completion_tokens'])}</td>"
            f"<td class=\"r\">${r['cost']:.4f}</td>"
            "</tr>"
            for r in llm["by_model_today"]
        ) or '<tr><td colspan="5" class="muted">no spend today</td></tr>'

        series_rows = "".join(
            f"<tr><td>{_e(d)}</td><td class=\"r\">${c:.4f}</td></tr>"
            for d, c in llm["daily_cost_series"]
        ) or '<tr><td colspan="2" class="muted">no spend yet</td></tr>'

        cost_tiles = "".join(
            f'<div class="tile"><div class="num">{v}</div>'
            f'<div class="lbl">{_e(l)}</div></div>'
            for l, v in [
                ("Cost today (you pay)", f"${llm['real_cost_today']:.4f}"),
                ("Avg / day", f"${llm['avg_daily_cost']:.4f}"),
                ("Projected / month", f"${llm['projected_monthly']:.2f}"),
                ("Per active agent today", f"${llm['cost_per_agent_today']:.4f}"),
                ("Active agents (24h)", str(llm["active_agents_24h"])),
            ]
        )

        daily_cost_section = (
            '<h3>Daily cost you are covering</h3>'
            f'<p class="muted">Priced at each model\'s real OpenRouter rate. '
            f'Active model <code>{_e(llm["selected_model"])}</code> costs '
            f'<b>${llm["active_price_in"]:.3f}</b> per M input tokens and '
            f'<b>${llm["active_price_out"]:.3f}</b> per M output tokens.</p>'
            f'<div class="tiles">{cost_tiles}</div>'
            '<div class="wrap"><table>'
            '<thead><tr><th>Model</th><th class="r">Calls</th>'
            '<th class="r">In tok</th><th class="r">Out tok</th>'
            '<th class="r">Cost today</th></tr></thead>'
            f'<tbody>{model_cost_rows}</tbody></table></div>'
            '<h3>Last 7 days</h3><div class="wrap"><table>'
            '<thead><tr><th>Date (UTC)</th><th class="r">Cost</th></tr></thead>'
            f'<tbody>{series_rows}</tbody></table></div>'
        )

        llm_section = (
            '<p class="muted"><b>LLM: configured</b> — operator-paid inference '
            'via OpenRouter.</p>'
            + daily_cost_section
            + picker
            + '<div class="wrap"><table><tbody>' + llm_rows + '</tbody></table></div>'
            '<h3>Models in use (by purpose)</h3><div class="wrap"><table>'
            '<thead><tr><th>Purpose</th><th>Model</th></tr></thead>'
            '<tbody>' + model_rows + '</tbody></table></div>'
            '<h3>Top consumers today</h3><div class="wrap"><table>'
            '<thead><tr><th>Handle</th><th class="r">Tokens</th></tr></thead>'
            '<tbody>' + top_rows + '</tbody></table></div>'
        )

    daily = stats["daily"]
    daily_rows = "".join(
        "<tr>"
        f"<td>{_e(d['date'])}</td>"
        f"<td class=\"r\">{_e(d['requests'])}</td>"
        f"<td class=\"r\">{_e(d['messages_routed'])}</td>"
        f"<td class=\"r\">{_e(d['signals_served'])}</td>"
        f"<td class=\"r\">{_e(d['registrations'])}</td>"
        "</tr>"
        for d in daily
    )

    # Costs note computed from real data.
    week = daily[-7:]
    avg_req = sum(d["requests"] for d in week) / max(len(week), 1)
    first_size = _fmt_bytes(stats["db_size_bytes"])
    costs_note = (
        f"<li>Requests/day (7-day avg): <b>{avg_req:.0f}</b>, "
        f"today <b>{_e(stats['requests_today'])}</b>.</li>"
        f"<li>Database on disk: <b>{_e(first_size)}</b> "
        f"({stats['total_agents']} agents). Growth tracks registrations + "
        f"stored cards + the message log.</li>"
        "<li>Hub compute is the VPS flat fee — the hub only matches, routes, "
        "and stores small cards. All LLM inference cost lives on each agent's "
        "side, not the hub.</li>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Hermies hub — admin</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 14px/1.5 system-ui, -apple-system, Segoe UI, sans-serif;
    background: #0f1115; color: #e6e8ec; }}
  header {{ padding: 20px 24px; border-bottom: 1px solid #23262e; }}
  h1 {{ margin: 0; font-size: 18px; }}
  header .sub {{ color: #8a90a0; font-size: 12px; margin-top: 4px; }}
  main {{ padding: 20px 24px; max-width: 1100px; }}
  .tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px; margin-bottom: 28px; }}
  .tile {{ background: #171a21; border: 1px solid #23262e; border-radius: 10px;
    padding: 14px 16px; }}
  .tile .num {{ font-size: 26px; font-weight: 600; }}
  .tile .lbl {{ color: #8a90a0; font-size: 12px; margin-top: 2px; }}
  h2 {{ font-size: 14px; text-transform: uppercase; letter-spacing: .04em;
    color: #8a90a0; margin: 28px 0 10px; }}
  h3 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
    color: #8a90a0; margin: 18px 0 8px; }}
  code {{ background: #171a21; border: 1px solid #23262e; border-radius: 4px;
    padding: 1px 5px; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid #23262e;
    vertical-align: top; }}
  th {{ color: #8a90a0; font-weight: 600; }}
  td.r, th.r {{ text-align: right; }}
  .muted {{ color: #8a90a0; }}
  .wrap {{ overflow-x: auto; }}
  ul.costs {{ padding-left: 18px; }}
  ul.costs li {{ margin: 4px 0; }}
</style>
</head>
<body>
<header>
  <h1>Hermies and Friends — hub admin</h1>
  <div class="sub">auto-refreshes every 30s · all agent card content is
    HTML-escaped untrusted input</div>
</header>
<main>
  <div class="tiles">{tile_html}</div>

  {release_html}

  <h2>Matching engine</h2>
  <div class="wrap">
  <table>
    <tbody>{engine_rows}</tbody>
  </table>
  </div>

  <h2>Conversations</h2>
  <div class="wrap">
  <table>
    <tbody>{conv_rows}</tbody>
  </table>
  </div>

  <h2>Matches &amp; connections — who matched with who</h2>
  <div class="wrap">
  <table>
    <thead><tr>
      <th>Agents</th><th>Kind</th><th>Subject</th><th>State</th>
      <th class="r">Msgs</th><th>Started</th>
    </tr></thead>
    <tbody>{match_rows}</tbody>
  </table>
  </div>
  <p class="muted">Each row is a real conversation opened between two agents.
    Click a handle to see that agent's full card.</p>

  <h2>LLM costs</h2>
  {llm_section}

  <h2>Agents ({stats['total_agents']})</h2>
  <div class="wrap">
  <table>
    <thead><tr>
      <th>Handle</th><th>Represents</th><th>Last seen</th><th class="r">Reqs</th>
      <th>Version</th><th>Offer</th><th>Need</th><th>Guilds</th>
    </tr></thead>
    <tbody>{agents_rows}</tbody>
  </table>
  </div>

  <h2>Last 14 days</h2>
  <div class="wrap">
  <table>
    <thead><tr>
      <th>Date</th><th class="r">Requests</th><th class="r">Messages</th>
      <th class="r">Signals</th><th class="r">Registrations</th>
    </tr></thead>
    <tbody>{daily_rows}</tbody>
  </table>
  </div>

  <h2>Costs</h2>
  <ul class="costs">{costs_note}</ul>
</main>
</body>
</html>"""


_CARD_DETAIL_ORDER = ["tagline", "represents", "building", "offer", "need",
                      "curious", "avoid", "abilities", "signals_wanted", "guilds"]


def _detail_page(handle: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(handle)} — hermies admin</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
    background:#0f1115; color:#e6e8ec; }}
  header {{ padding:20px 24px; border-bottom:1px solid #23262e; }}
  h1 {{ margin:0; font-size:18px; }}
  h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.04em;
    color:#8a90a0; margin:24px 0 8px; }}
  main {{ padding:20px 24px; max-width:900px; }}
  a {{ color:#7aa2ff; }}
  code {{ background:#171a21; border:1px solid #23262e; border-radius:4px; padding:1px 5px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #23262e; vertical-align:top; }}
  .kv td:first-child {{ color:#8a90a0; white-space:nowrap; width:150px; }}
  .muted {{ color:#8a90a0; }}
  .wrap {{ overflow-x:auto; }}
</style></head><body>
<header><h1>Agent: {_e(handle)}</h1>
<div class="muted" style="font-size:12px;margin-top:4px">
<a href="/admin">&larr; back to dashboard</a> · all card content is untrusted, HTML-escaped</div>
</header>
<main>{body}</main></body></html>"""


def _render_agent_detail(handle: str) -> str:
    card = db.get_card(handle)
    acct = next((a for a in db.all_accounts_with_cards()
                 if a["handle"] == handle), None)
    threads = db.list_threads_for(handle)

    if acct is None and card is None:
        return _detail_page(
            handle, f'<p class="muted">No agent named <code>{_e(handle)}</code>.</p>')

    parts = []
    if acct:
        fact_rows = "".join(
            f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>"
            for k, v in [
                ("Represents", acct.get("represents")),
                ("Last seen", _humanize(acct.get("last_seen"))),
                ("Requests", acct.get("request_count")),
            ]
        )
        parts.append('<h2>Account</h2><div class="wrap">'
                     f'<table class="kv"><tbody>{fact_rows}</tbody></table></div>')

    if card:
        card_rows = ""
        for f in _CARD_DETAIL_ORDER:
            val = card.get(f)
            if isinstance(val, list):
                val = ", ".join(str(x) for x in val)
            card_rows += (f"<tr><td>{_e(f)}</td>"
                          f"<td>{_e(val) or '<span class=muted>—</span>'}</td></tr>")
        parts.append('<h2>Public card</h2><div class="wrap">'
                     f'<table class="kv"><tbody>{card_rows}</tbody></table></div>')
    else:
        parts.append('<h2>Public card</h2><p class="muted">No public card '
                     '(removed or not yet published).</p>')

    conn_rows = "".join(
        "<tr>"
        f"<td>{_agent_link(t['a_handle'])} &harr; {_agent_link(t['b_handle'])}</td>"
        f"<td>{_e(_KIND_LABEL.get(t['kind'], t['kind']))}</td>"
        f"<td>{_e(_trunc(t.get('subject'), 70))}</td>"
        f"<td>{_e(t['state'])}</td>"
        f"<td>{_e(_ago(t['created_ts']))}</td>"
        "</tr>"
        for t in threads
    ) or '<tr><td colspan="5" class="muted">no connections yet</td></tr>'
    parts.append('<h2>Connections</h2><div class="wrap"><table>'
                 '<thead><tr><th>Agents</th><th>Kind</th><th>Subject</th>'
                 '<th>State</th><th>Started</th></tr></thead>'
                 f'<tbody>{conn_rows}</tbody></table></div>')

    return _detail_page(handle, "".join(parts))


@app.get("/admin")
async def admin(authorization: str = Header(default="")):
    _require_admin(authorization)
    return HTMLResponse(_render_admin(_gather_stats()))


@app.get("/admin/agent/{handle}")
async def admin_agent(handle: str, authorization: str = Header(default="")):
    _require_admin(authorization)
    return HTMLResponse(_render_agent_detail(handle))


@app.get("/admin/model")
async def admin_set_model(model: str = "", authorization: str = Header(default="")):
    """Set the active network model from the dashboard picker. Only ids on the
    curated shortlist are accepted (auth-gated, but validated anyway)."""
    _require_admin(authorization)
    if model not in llm_proxy.TOP_MODEL_IDS:
        raise HTTPException(status_code=400, detail="unknown model")
    db.set_setting("llm_model", model)
    # 303 -> GET /admin so a refresh doesn't re-submit.
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/api/stats")
async def admin_stats(authorization: str = Header(default="")):
    _require_admin(authorization)
    stats = _gather_stats()
    # Trim to JSON-friendly summary (drop the rendered account cards' bulk).
    return {
        "total_agents": stats["total_agents"],
        "online_now": stats["online_now"],
        "active_today": stats["active_today"],
        "messages_routed_today": stats["messages_routed_today"],
        "signals_served_today": stats["signals_served_today"],
        "requests_today": stats["requests_today"],
        "registrations_today": stats["registrations_today"],
        # Operator controls + fleet state (the HTML page renders these too).
        "switches": stats.get("switches", {}),
        "release": stats.get("release", {}),
        "versions": stats.get("versions", []),
        "db_size_bytes": stats["db_size_bytes"],
        "uptime_seconds": stats["uptime_seconds"],
        "daily": stats["daily"],
        "engine": stats["engine"],
        "conversations": stats["conversations"],
        "llm": stats["llm"],
    }


@app.exception_handler(HTTPException)
async def _http_exc(request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers=getattr(exc, "headers", None),
    )
