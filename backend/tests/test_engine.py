"""Unit tests for the semantic matching engine (engine.py + vindex + embeddings).

These MUST run without fastembed installed: every test forces the deterministic
hashing fallback encoder via HERMIX_FORCE_FALLBACK_EMBED=1, so no model is
downloaded and results are reproducible.
"""
import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DAY = 86400.0


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh sqlite DB + fallback encoder; returns (engine_mod, db_mod)."""
    monkeypatch.setenv("HERMIX_FORCE_FALLBACK_EMBED", "1")
    db_file = tmp_path / f"engine-{uuid.uuid4().hex}.db"
    monkeypatch.setenv("HERMIX_DB", str(db_file))
    for mod in ("matching", "vindex", "embeddings", "db", "engine"):
        sys.modules.pop(mod, None)
    import embeddings
    embeddings.reset_encoder_cache()
    import db as db_mod
    import engine as engine_mod
    db_mod.init_db()
    return engine_mod, db_mod


def _new_engine(env):
    engine_mod, db_mod = env
    return engine_mod.build_engine(db_mod), db_mod


def _score_of(results, agent):
    for r in results:
        if r["agent"] == agent:
            return r["score"]
    return None


# --- encoder mode ---------------------------------------------------------
def test_fallback_encoder_active(env):
    eng, _ = _new_engine(env)
    assert eng.mode == "fallback"
    assert eng.model_name == "hash-ngram-v1"


# --- semantic-ish matching via the fallback encoder -----------------------
def test_fallback_semantic_matching(env):
    eng, _ = _new_engine(env)
    now = time.time()
    # Related supply (shares "music" + char-grams with the query need).
    eng.upsert_card("bex", {"handle": "bex", "offer": ["music visualizers"],
                            "need": ["ai film"]}, now)
    # Unrelated.
    eng.upsert_card("zed", {"handle": "zed", "offer": ["quantum physics"],
                            "need": ["basketball"]}, now)

    q = {"handle": "me", "need": ["music visuals"], "offer": ["ai film"]}
    results = eng.match(q)
    assert results[0]["agent"] == "bex"
    assert _score_of(results, "bex") > 0
    # The unrelated agent must score strictly below the related one.
    assert _score_of(results, "bex") > (_score_of(results, "zed") or 0)


# --- reciprocity beats a one-sided fit ------------------------------------
def test_reciprocity_beats_one_sided(env):
    eng, _ = _new_engine(env)
    now = time.time()
    # Mutual: offers what I need AND needs what I offer.
    eng.upsert_card("mutual", {"handle": "mutual", "offer": ["3d modeling"],
                               "need": ["sound design"]}, now)
    # One-sided: offers what I need, but needs something I do not offer.
    eng.upsert_card("oneway", {"handle": "oneway", "offer": ["3d modeling"],
                               "need": ["marketing"]}, now)

    q = {"handle": "me", "need": ["3d modeling"], "offer": ["sound design"]}
    results = eng.match(q)
    s_mutual = _score_of(results, "mutual")
    s_oneway = _score_of(results, "oneway")
    assert s_mutual > s_oneway > 0
    # Reciprocity should give the harmonic core a real edge, not a hair.
    assert s_mutual >= s_oneway * 1.5


# --- presence decay -------------------------------------------------------
def test_presence_decay(env):
    eng, _ = _new_engine(env)
    now = time.time()
    card = {"offer": ["ai video"], "need": ["music visuals"]}
    eng.upsert_card("fresh", {**card, "handle": "fresh"}, now)
    eng.upsert_card("stale", {**card, "handle": "stale"}, now - 30 * DAY)

    q = {"handle": "me", "need": ["ai video"], "offer": ["music visuals"]}
    results = eng.match(q)
    s_fresh = _score_of(results, "fresh")
    s_stale = _score_of(results, "stale")
    assert s_fresh > s_stale                       # recent ranks higher
    comp = {r["agent"]: r["components"]["presence"] for r in results}
    assert comp["fresh"] > comp["stale"]
    # 30d unseen -> meaningful decay (half-life 14d => 2^(-30/14) ~= 0.23).
    # Softened per eval finding: staleness must never bury a strong fit.
    assert comp["stale"] < 0.35


# --- guild bonus ----------------------------------------------------------
def test_guild_bonus(env):
    eng, _ = _new_engine(env)
    now = time.time()
    # Same (near-zero) semantic content; only guild membership differs.
    eng.upsert_card("guilded", {"handle": "guilded", "offer": ["woodworking"],
                                "guilds": ["ai-film"]}, now)
    eng.upsert_card("nonguild", {"handle": "nonguild", "offer": ["woodworking"],
                                 "guilds": ["cooking"]}, now)

    q = {"handle": "me", "need": ["astrophysics"], "offer": ["poetry"],
         "guilds": ["ai-film"]}
    results = eng.match(q)
    assert _score_of(results, "guilded") > _score_of(results, "nonguild")
    comp = {r["agent"]: r["components"]["guilds"] for r in results}
    assert comp["guilded"] > 0
    assert comp["nonguild"] == 0


# --- score + component bounds ---------------------------------------------
def test_score_and_component_bounds(env):
    eng, _ = _new_engine(env)
    now = time.time()
    cards = [
        {"handle": "a", "offer": ["ai video", "3d"], "need": ["music"], "guilds": ["x"]},
        {"handle": "b", "offer": ["music"], "need": ["ai video"], "guilds": ["x"]},
        {"handle": "c", "offer": ["gardening"], "need": ["law"]},
    ]
    for c in cards:
        eng.upsert_card(c["handle"], c, now)
    q = {"handle": "me", "need": ["music", "ai video"], "offer": ["ai video"],
         "guilds": ["x"]}
    for r in eng.match(q):
        assert 0.0 <= r["score"] <= 10.0
        for k in ("need_to_offer", "offer_to_need", "guilds", "presence"):
            assert 0.0 <= r["components"][k] <= 1.0


# --- explanation quotes real card terms -----------------------------------
def test_why_contains_real_terms(env):
    eng, _ = _new_engine(env)
    now = time.time()
    eng.upsert_card("bex", {"handle": "bex", "represents": "a visuals producer",
                            "offer": ["music visualizers"], "need": ["ai film"]}, now)
    q = {"handle": "me", "need": ["music visuals"], "offer": ["ai film"]}
    why = eng.match(q)[0]["why"]
    # Grounded: the candidate's real offer term and my real need term appear,
    # and the API-contract keyword "offers" is present for a supply->need fit.
    assert "music visualizers" in why           # real term from their card
    assert "music visuals" in why               # real term from my card
    assert "offers" in why


def test_why_never_fabricates_empty_side(env):
    eng, _ = _new_engine(env)
    now = time.time()
    # Candidate matches only by guild; there is no offer/need overlap to quote.
    eng.upsert_card("g", {"handle": "g", "represents": "guild friend",
                          "offer": ["glassblowing"], "guilds": ["ai-film"]}, now)
    q = {"handle": "me", "need": ["rocketry"], "offer": ["ballet"],
         "guilds": ["ai-film"]}
    why = eng.match(q)[0]["why"]
    assert "ai-film" in why                       # the real shared guild
    # No invented offer/need pairing sneaks in.
    assert "rocketry" not in why or "glassblowing" in why


# --- persistence round-trip ----------------------------------------------
def test_persistence_roundtrip(env):
    engine_mod, db_mod = env
    a = {"handle": "aria", "offer": ["ai video"], "need": ["music visuals"]}
    b = {"handle": "bex", "offer": ["music visuals"], "need": ["ai video"]}
    # Cards + accounts must exist so a rebuild reloads content and presence.
    # Presence is sourced from accounts.last_seen (not frozen into vectors), so
    # seed a stable last_seen to make scores reproducible across rebuilds.
    for h, card in (("aria", a), ("bex", b)):
        db_mod.create_account(db_mod.hash_key("k-" + h), h, "")
        db_mod.upsert_card(h, card)
        db_mod.touch_account(h)

    q = {"handle": "me", "need": ["music visuals"], "offer": ["ai video"]}

    # First build re-encodes the (vector-less) cards and persists their vectors.
    eng1 = engine_mod.build_engine(db_mod)
    # Exercise the upsert path too (keeps loaded presence with last_seen=None).
    eng1.upsert_card("aria", a)
    first = {r["agent"]: r["score"] for r in eng1.match(q)}
    assert len(db_mod.all_vectors()) == 2 * 4     # 2 agents x 4 field groups

    # Rebuild a brand-new engine purely from sqlite; identical scores.
    eng2 = engine_mod.build_engine(db_mod)
    assert eng2.card_count == 2
    second = {r["agent"]: r["score"] for r in eng2.match(q)}
    assert first == second


# --- migration from a pre-vector DB (cards present, no card_vectors rows) --
def test_migration_reencodes_missing_vectors(env):
    engine_mod, db_mod = env
    # Simulate an upgraded deployment: cards already stored, but the semantic
    # index has never been built, so card_vectors is empty.
    db_mod.upsert_card("legacy", {"handle": "legacy", "offer": ["3d worlds"],
                                  "need": ["shaders"]})
    db_mod.upsert_card("peer", {"handle": "peer", "offer": ["shaders"],
                                "need": ["3d worlds"]})
    assert db_mod.all_vectors() == []

    eng = engine_mod.build_engine(db_mod)          # should re-encode + persist
    assert eng.card_count == 2
    assert len(db_mod.all_vectors()) == 2 * 4      # self-healed on boot

    q = {"handle": "me", "need": ["3d worlds"], "offer": ["shaders"]}
    results = eng.match(q)
    assert any(r["agent"] == "peer" for r in results)


# --- remove ---------------------------------------------------------------
def test_remove_drops_from_index_and_db(env):
    eng, db_mod = _new_engine(env)
    now = time.time()
    eng.upsert_card("gone", {"handle": "gone", "offer": ["x"], "need": ["y"]}, now)
    assert eng.card_count == 1
    eng.remove("gone")
    assert eng.card_count == 0
    assert db_mod.all_vectors() == []
