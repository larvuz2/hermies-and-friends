"""SQLite persistence for the Hermies hub.

Stdlib-only. One file DB (path overridable via env HERMIES_DB for tests).
Tables:
  accounts  - api key (sha256) -> handle, and the represents blurb
  cards     - handle -> JSON public card (upsert)
  messages  - inbound mailbox rows: id, to_handle, from_handle, query, drained
"""
import hashlib
import json
import os
import sqlite3
import threading

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermies.db")

# One lock guards writes; sqlite connections are created per-call to stay
# thread-safe under the TestClient / uvicorn worker model.
_LOCK = threading.Lock()


def db_path() -> str:
    return os.environ.get("HERMIES_DB", DEFAULT_DB)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS accounts (
                key_hash   TEXT PRIMARY KEY,
                handle     TEXT UNIQUE NOT NULL,
                represents TEXT DEFAULT ''
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cards (
                handle TEXT PRIMARY KEY,
                card   TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                to_handle   TEXT NOT NULL,
                from_handle TEXT NOT NULL,
                query       TEXT NOT NULL,
                drained     INTEGER NOT NULL DEFAULT 0
            )"""
        )


# --- accounts -------------------------------------------------------------
def create_account(key_hash: str, handle: str, represents: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO accounts (key_hash, handle, represents) VALUES (?, ?, ?)",
            (key_hash, handle, represents),
        )


def handle_exists(handle: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE handle = ?", (handle,)
        ).fetchone()
        return row is not None


def handle_for_key(key_hash: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT handle FROM accounts WHERE key_hash = ?", (key_hash,)
        ).fetchone()
        return row["handle"] if row else None


# --- cards ----------------------------------------------------------------
def upsert_card(handle: str, card: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO cards (handle, card) VALUES (?, ?) "
            "ON CONFLICT(handle) DO UPDATE SET card = excluded.card",
            (handle, json.dumps(card)),
        )


def get_card(handle: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT card FROM cards WHERE handle = ?", (handle,)
        ).fetchone()
        return json.loads(row["card"]) if row else None


def all_cards() -> list:
    with _connect() as conn:
        rows = conn.execute("SELECT card FROM cards").fetchall()
        return [json.loads(r["card"]) for r in rows]


# --- messages -------------------------------------------------------------
def add_message(msg_id: str, to_handle: str, from_handle: str, query: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO messages (id, to_handle, from_handle, query, drained) "
            "VALUES (?, ?, ?, ?, 0)",
            (msg_id, to_handle, from_handle, query),
        )


def drain_inbox(handle: str) -> list:
    """Return undrained messages for handle and mark them drained."""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT id, from_handle, query FROM messages "
            "WHERE to_handle = ? AND drained = 0 ORDER BY rowid",
            (handle,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            conn.executemany(
                "UPDATE messages SET drained = 1 WHERE id = ?", [(i,) for i in ids]
            )
        return [
            {"id": r["id"], "from": r["from_handle"], "query": r["query"]}
            for r in rows
        ]


def get_message(msg_id: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, to_handle, from_handle, query FROM messages WHERE id = ?",
            (msg_id,),
        ).fetchone()
        return dict(row) if row else None
