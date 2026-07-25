"""Hybrid semantic matchmaking engine (v2) for the Hermies hub.

Replaces naive token counting with a blend of dense semantic similarity and the
deterministic token/guild layer from ``matching.py``. Built to stay comfortable
at ~1000 agents on a CPU-only VPS.

How a pair scores (me = the querying card, them = a candidate):

  need_to_offer   cosine( my {need,curious,signals_wanted} , their {offer,abilities,building} )
  offer_to_need   cosine( their {need}                      , my {offer} )
  semantic        HARMONIC mean of the two directions (so reciprocity wins),
                  softened by a small one-directional term so a strong one-way
                  fit still surfaces (below any mutual fit).
  guilds          saturating bonus for shared guild tokens (matching.py).
  presence        candidate recency: half-life 14 days (soft — eval showed a
                  strong semantic fit must never be buried by staleness).

  base   = 0.75*semantic + 0.25*guilds
  score  = clamp(10 * base * (0.7 + 0.3*presence), 0..10)   # rounded 1dp

Each field group is embedded separately (see ``vindex.VectorIndex``) so the two
directions are independent. Cosines are calibrated per encoder (bge-small puts
unrelated text well above 0; the hashing fallback sits at 0) so components span
a usable 0..1 regardless of embedding mode.

Frozen interface (an eval harness targets this — do not drift):
  class MatchEngine:
    upsert_card(handle, card, last_seen_ts) -> None
    remove(handle) -> None
    match(card, exclude_handle="", top_k=20) -> list[MatchResult]
  MatchResult = {"agent", "score", "why", "components": {need_to_offer,
                 offer_to_need, guilds, presence}}
  build_engine(db_module) -> MatchEngine
"""
import logging
import threading
import time

import numpy as np

import embeddings
import matching

log = logging.getLogger("hermies.engine")

# Field groups embedded per card. "want"/"supply" drive the primary semantic
# direction; "need"/"offer" are the narrow reciprocal direction.
WANT_FIELDS = ("need", "curious", "signals_wanted")
SUPPLY_FIELDS = ("offer", "abilities", "building")
NEED_FIELDS = ("need",)
OFFER_FIELDS = ("offer",)
GROUPS = {
    "want": WANT_FIELDS,
    "supply": SUPPLY_FIELDS,
    "need": NEED_FIELDS,
    "offer": OFFER_FIELDS,
}

PRESENCE_HALF_LIFE_DAYS = 14.0
DAY_SECONDS = 86400.0


def _terms(card: dict, fields) -> list:
    """Flatten the given card fields into a list of non-empty term strings."""
    out = []
    for f in fields:
        for item in (card.get(f) or []):
            s = str(item).strip()
            if s:
                out.append(s)
    return out


def _group_text(card: dict, fields) -> str:
    return ", ".join(_terms(card, fields))


def _harmonic(a: float, b: float) -> float:
    if a <= 0.0 or b <= 0.0:
        return 0.0
    return 2.0 * a * b / (a + b)


def _pick_pair(a_terms, b_terms):
    """A grounded (a_term, b_term) pair: best literal overlap, else first-of-each.

    Only ever returns terms that appear on the cards (never fabricated). Returns
    None if either side has no terms to quote.
    """
    pair = matching.best_shared_pair(a_terms, b_terms)
    if pair:
        return pair
    if a_terms and b_terms:
        return (a_terms[0], b_terms[0])
    return None


class MatchEngine:
    def __init__(self, encoder: "embeddings.Encoder"):
        self.encoder = encoder
        self.dim = encoder.dim
        self.index = vindex_new(encoder.dim)
        self._cards: "dict[str, dict]" = {}
        self._last_seen: "dict[str, float]" = {}
        self._db = None
        self._lock = threading.Lock()

    # --- introspection ----------------------------------------------------
    @property
    def mode(self) -> str:
        return self.encoder.mode

    @property
    def model_name(self) -> str:
        return self.encoder.model_name

    @property
    def card_count(self) -> int:
        return len(self.index)

    # --- encoding ---------------------------------------------------------
    def _encode_groups(self, card: dict) -> "dict[str, np.ndarray]":
        names = list(GROUPS.keys())
        texts = [_group_text(card, GROUPS[n]) for n in names]
        mat = self.encoder.encode(texts)
        return {n: mat[i] for i, n in enumerate(names)}

    # --- mutation ---------------------------------------------------------
    def upsert_card(self, handle: str, card: dict,
                    last_seen_ts: "float | None" = None) -> None:
        """Encode + index + persist one agent's card. Idempotent per handle."""
        handle = str(handle)
        card = dict(card or {})
        card.setdefault("handle", handle)
        group_vecs = self._encode_groups(card)
        with self._lock:
            self.index.upsert(handle, group_vecs)
            self._cards[handle] = card
            if last_seen_ts is not None:
                self._last_seen[handle] = float(last_seen_ts)
            elif handle not in self._last_seen:
                self._last_seen[handle] = time.time()
        self._persist_vectors(handle, group_vecs)

    def remove(self, handle: str) -> None:
        handle = str(handle)
        with self._lock:
            self.index.remove(handle)
            self._cards.pop(handle, None)
            self._last_seen.pop(handle, None)
        if self._db is not None:
            try:
                self._db.delete_vectors(handle)
            except Exception as exc:  # persistence is best-effort, never fatal
                log.warning("engine.remove: could not delete vectors for %s: %s",
                            handle, exc)

    def _persist_vectors(self, handle: str, group_vecs: "dict[str, np.ndarray]") -> None:
        if self._db is None:
            return
        blobs = {g: np.asarray(v, dtype=np.float32).tobytes()
                 for g, v in group_vecs.items()}
        try:
            self._db.upsert_vectors(handle, blobs, self.model_name, time.time())
        except Exception as exc:  # never take the hub down over a cache write
            log.warning("engine: could not persist vectors for %s: %s", handle, exc)

    # --- presence ---------------------------------------------------------
    def _presence(self, handle: str, now: float) -> float:
        ts = self._last_seen.get(handle)
        if ts is None:
            return 0.5                       # unknown recency -> neutral
        age_days = max(0.0, (now - ts) / DAY_SECONDS)
        p = 2.0 ** (-age_days / PRESENCE_HALF_LIFE_DAYS)
        return float(min(1.0, max(0.0, p)))

    def _calib(self, cos: float) -> float:
        floor = getattr(self.encoder, "sim_floor", 0.0)
        if floor >= 1.0:
            return max(0.0, min(1.0, cos))
        return float(max(0.0, min(1.0, (cos - floor) / (1.0 - floor))))

    # --- matching ---------------------------------------------------------
    def match(self, card: dict, exclude_handle: str = "",
              top_k: int = 20) -> list:
        card = dict(card or {})
        my_handle = str(card.get("handle") or "").lower()
        exclude = str(exclude_handle or "").lower()
        now = time.time()

        q = self._encode_groups(card)
        # Vectorised directional cosines against the whole index.
        raw_need_to_offer = self.index.query("supply", q["want"])   # my want -> their supply
        raw_offer_to_need = self.index.query("need", q["offer"])    # their need -> my offer

        my_want = _terms(card, WANT_FIELDS)
        my_offer = _terms(card, OFFER_FIELDS)
        my_guilds = card.get("guilds") or []

        results = []
        for handle in self.index.handles():
            h_low = handle.lower()
            if h_low == exclude or (my_handle and h_low == my_handle):
                continue
            cand = self._cards.get(handle, {"handle": handle})

            need_to_offer = self._calib(raw_need_to_offer.get(handle, 0.0))
            offer_to_need = self._calib(raw_offer_to_need.get(handle, 0.0))

            reciprocal = _harmonic(need_to_offer, offer_to_need)
            one_side = max(need_to_offer, offer_to_need)
            semantic = 0.75 * reciprocal + 0.25 * one_side

            g = matching.guild_overlap(my_guilds, cand.get("guilds") or [])
            guilds_comp = 1.0 - 0.5 ** g if g > 0 else 0.0

            presence = self._presence(handle, now)

            base = 0.75 * semantic + 0.25 * guilds_comp
            # Soft presence: a great match decays gently (eval finding — never
            # let recency override a clearly better semantic fit).
            score01 = base * (0.7 + 0.3 * presence)
            score = round(max(0.0, min(10.0, score01 * 10.0)), 1)

            components = {
                "need_to_offer": round(need_to_offer, 4),
                "offer_to_need": round(offer_to_need, 4),
                "guilds": round(guilds_comp, 4),
                "presence": round(presence, 4),
            }
            results.append((score, handle, cand, need_to_offer, offer_to_need,
                            guilds_comp, components))

        results.sort(key=lambda r: (-r[0], r[1]))
        top = results[:max(0, int(top_k))]

        out = []
        for (score, handle, cand, n2o, o2n, guilds_comp, components) in top:
            why = self._explain(card, cand, my_want, my_offer, my_guilds,
                                 n2o, o2n, guilds_comp)
            out.append({
                "agent": cand.get("handle", handle),
                "score": score,
                "why": why,
                "components": components,
            })
        return out

    # --- explanation ------------------------------------------------------
    def _explain(self, card, cand, my_want, my_offer, my_guilds,
                 n2o, o2n, guilds_comp) -> str:
        """A grounded, human explanation quoting real card terms only."""
        rep = str(cand.get("represents") or cand.get("handle") or "an agent")
        cand_supply = _terms(cand, SUPPLY_FIELDS)
        cand_need = _terms(cand, NEED_FIELDS)

        clauses = []
        # Lead with the candidate-offers-your-need direction when it is at least
        # as strong (this also guarantees the literal word "offers" for mutual
        # fits, which the API contract's callers rely on).
        if n2o > 0 and n2o >= o2n:
            pair = _pick_pair(cand_supply, my_want)
            if pair:
                clauses.append(
                    f"{rep} offers '{pair[0]}' for your '{pair[1]}'")
            if o2n > 0:
                mp = _pick_pair(my_offer, cand_need)
                if mp:
                    clauses.append(
                        f"mutual: your '{mp[0]}' fits their need '{mp[1]}'")
        elif o2n > 0:
            mp = _pick_pair(my_offer, cand_need)
            if mp:
                clauses.append(
                    f"your '{mp[0]}' fits their need '{mp[1]}'")
            if n2o > 0:
                pair = _pick_pair(cand_supply, my_want)
                if pair:
                    clauses.append(
                        f"mutual: {rep} offers '{pair[0]}' for your '{pair[1]}'")

        # No semantic direction fired — ground in guild or a real offer term.
        if not clauses:
            guild_pair = matching.best_shared_pair(my_guilds, cand.get("guilds") or [])
            if guild_pair:
                clauses.append(f"{rep} shares your guild '{guild_pair[0]}'")
            elif cand_supply:
                clauses.append(f"{rep} offers '{cand_supply[0]}'")
            else:
                clauses.append(rep)
        elif guilds_comp > 0:
            guild_pair = matching.best_shared_pair(my_guilds, cand.get("guilds") or [])
            if guild_pair:
                clauses.append(f"shared guild '{guild_pair[0]}'")

        return "; ".join(clauses)

    # --- startup load -----------------------------------------------------
    def _load_from_db(self, db_module) -> None:
        """Rebuild the in-memory index from sqlite (cards + persisted vectors).

        Cards without stored vectors (fresh migration, or a model change) are
        re-encoded and persisted so the first boot after an upgrade self-heals.
        """
        self._db = db_module
        cards = {c.get("handle"): c for c in db_module.all_cards()
                 if c.get("handle")}
        self._cards = dict(cards)

        try:
            self._last_seen = dict(db_module.last_seen_ts_map())
        except Exception as exc:
            log.warning("engine: last_seen load failed (%s); presence neutral", exc)
            self._last_seen = {}

        stored: "dict[str, dict[str, np.ndarray]]" = {}
        try:
            rows = db_module.all_vectors()
        except Exception as exc:
            log.warning("engine: vector load failed (%s); re-encoding all", exc)
            rows = []
        cur_model = self.model_name
        for row in rows:
            handle = row["handle"]
            if handle not in cards:
                continue                     # orphan vector, card gone
            if row["model"] != cur_model:
                continue                     # different embedding space; re-encode
            vec = np.frombuffer(row["vector"], dtype=np.float32)
            if vec.shape[0] != self.dim:
                continue
            stored.setdefault(handle, {})[row["group"]] = vec

        needed = set(GROUPS.keys())
        for handle, card in cards.items():
            groups = stored.get(handle)
            if groups and needed.issubset(groups.keys()):
                self.index.upsert(handle, groups)
            else:
                group_vecs = self._encode_groups(card)
                self.index.upsert(handle, group_vecs)
                self._persist_vectors(handle, group_vecs)


def vindex_new(dim: int):
    # Small indirection so vindex is imported lazily / mockable in tests.
    import vindex
    return vindex.VectorIndex(dim)


def build_engine(db_module) -> MatchEngine:
    """Construct the engine and load persisted embeddings + cards from sqlite."""
    encoder = embeddings.get_encoder()
    engine = MatchEngine(encoder)
    engine._load_from_db(db_module)
    log.info("engine built: mode=%s model=%s cards=%d",
             engine.mode, engine.model_name, engine.card_count)
    return engine
