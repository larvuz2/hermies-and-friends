"""Reference *fallback-mode* match engine implementing the FROZEN interface.

Used by the harness ONLY when ``backend/engine.py`` does not yet exist, so the
quality harness can be validated end-to-end and so we can prove the fallback
gates are actually *satisfiable* by a competent token-based matcher (not a
naive one). When the real engine lands, the harness imports it instead and this
file is ignored.

Frozen interface
----------------
    build_engine(db_module) -> MatchEngine
    MatchEngine.upsert_card(handle, card, last_seen_ts)
    MatchEngine.match(card, exclude_handle="", top_k=20) -> list of
        {"agent", "score" (0..10), "why",
         "components": {"need_to_offer", "offer_to_need", "guilds", "presence"}}

Design (deliberately representative of a real fallback encoder)
--------------------------------------------------------------
* Bag-of-words over tokenized card fields, IDF-weighted, compared with cosine
  similarity. Cosine's length normalization is what makes keyword-stuffed spam
  *fail*: a huge stuffed term set inflates the vector norm, so its cosine to any
  real card stays small. Common buzzwords also carry low IDF.
* Directional field routing mirrors ``backend/matching.py``:
    need_to_offer = cosine( my {need, curious, signals_wanted},
                            their {offer, abilities} )
    offer_to_need = cosine( my {offer, abilities},
                            their {need, signals_wanted} )
    guilds        = Jaccard( my guilds, their guilds )
    presence      = recency of their last_seen_ts vs the engine's newest card
* Final score in 0..10 = 10 * saturating( weighted sum of components ).

This is token-based, so it CANNOT bridge cross-vocabulary pairs ("3d worlds" vs
"three-dimensional environments") — that is the expected fallback limitation the
gates encode. ``mode == "fallback"`` advertises that to the harness.
"""
import math
import re

mode = "fallback"  # harness reads this to pick the gate set

_SPLIT = re.compile(r"[^a-z0-9]+")

# Generic connective words carry no matching signal; dropping them keeps cosine
# focused on content terms (and stops "looking for a partner" style boilerplate
# from linking unrelated cards).
_STOP = frozenset("""
a an the and or for to of with in on at as is are be it this that these those
my me you your our their we they them us i he she his her its who whom whose
want wants wanting looking need needs needing offer offers offering open help
someone people person anyone anybody around near steady regular most every all
more some any into from by about over under out up down so then than also just
new good great other another via per each own doing done get got make making
""".split())


def _tokens(term):
    out = set()
    for tok in _SPLIT.split(str(term).lower()):
        if tok and len(tok) > 2 and tok not in _STOP:
            out.add(tok)
    return out


def _field_tokens(card, fields):
    out = set()
    for f in fields:
        val = card.get(f) or []
        if isinstance(val, str):
            val = [val]
        for item in val:
            out |= _tokens(item)
    return out


_ALL_FIELDS = ("tagline", "represents", "building", "offer", "need", "curious",
               "avoid", "abilities", "signals_wanted")
_WANT = ("need", "curious", "signals_wanted")
_SUPPLY = ("offer", "abilities")

# Component weights (also reported in CALIBRATION.md as recommended defaults).
_W_N2O = 0.40
_W_O2N = 0.30
_W_GUILD = 0.20
_W_PRESENCE = 0.10
# Gain on the cosine/Jaccard similarities before the saturating map. Cosine
# values between real cards are small; this lifts a genuine match toward the
# top of the 0..10 band while unrelated cards stay near the floor.
_GAIN = 3.2
# Corpus-derived stopwording: a token appearing in more than this fraction of
# all cards carries no discriminative signal and is dropped from matching. This
# is the load-bearing spam defense: keyword-stuffed cards pile onto hot generic
# terms ("growth", "ai", "seo", "startup"), which land above this cap and get
# zeroed, so spam cannot ride shared buzzwords into a real card's top-5.
_DF_CAP = 0.06


def _cosine(a_tokens, b_tokens, idf):
    if not a_tokens or not b_tokens:
        return 0.0
    inter = a_tokens & b_tokens
    if not inter:
        return 0.0
    dot = sum(idf.get(t, 1.0) ** 2 for t in inter)
    na = math.sqrt(sum(idf.get(t, 1.0) ** 2 for t in a_tokens))
    nb = math.sqrt(sum(idf.get(t, 1.0) ** 2 for t in b_tokens))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _jaccard(a, b):
    sa, sb = set(a or []), set(b or [])
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _saturate(x):
    # Smooth, monotonic 0->0, larger->~1. Keeps scores inside 0..10 and gives a
    # gentle separation between weak and strong similarity.
    return 1.0 - math.exp(-_GAIN * x)


class MatchEngine:
    def __init__(self, db_module):
        self.db = db_module
        if db_module is not None and hasattr(db_module, "init_db"):
            db_module.init_db()
        self._last_seen = {}          # handle -> ts
        self._idf = None              # token -> idf (lazy, invalidated on write)
        self._newest_ts = None

    # -- writes ------------------------------------------------------------
    def upsert_card(self, handle, card, last_seen_ts):
        card = dict(card)
        card["handle"] = handle
        if self.db is not None and hasattr(self.db, "upsert_card"):
            self.db.upsert_card(handle, card)
        self._last_seen[handle] = last_seen_ts
        self._idf = None              # corpus changed; rebuild IDF on next read

    # -- internals ---------------------------------------------------------
    def _all_cards(self):
        if self.db is not None and hasattr(self.db, "all_cards"):
            return self.db.all_cards()
        return []

    def _ensure_idf(self):
        if self._idf is not None:
            return
        cards = self._all_cards()
        n = len(cards) or 1
        df = {}
        for c in cards:
            for t in _field_tokens(c, _ALL_FIELDS):
                df[t] = df.get(t, 0) + 1
        cap = _DF_CAP * n
        self._idf = {
            t: (0.0 if d > cap else math.log((n + 1) / (d + 1)) + 1.0)
            for t, d in df.items()
        }
        seens = [v for v in self._last_seen.values() if v is not None]
        self._newest_ts = max(seens) if seens else None

    def _presence(self, handle):
        ts = self._last_seen.get(handle)
        if ts is None or self._newest_ts is None:
            return 0.5
        age_days = max(0.0, (self._newest_ts - ts) / 86400.0)
        # Full credit if seen within ~2 days of the freshest card, decaying to 0
        # over ~30 days of staleness.
        return max(0.0, 1.0 - max(0.0, age_days - 2.0) / 30.0)

    # -- reads -------------------------------------------------------------
    def match(self, card, exclude_handle="", top_k=20):
        self._ensure_idf()
        idf = self._idf
        my_handle = str(card.get("handle") or "")
        my_want = _field_tokens(card, _WANT)
        my_supply = _field_tokens(card, _SUPPLY)
        my_guilds = card.get("guilds") or []

        results = []
        for other in self._all_cards():
            oh = str(other.get("handle") or "")
            if not oh:
                continue
            if oh == exclude_handle or oh == my_handle:
                continue  # self-exclusion
            their_supply = _field_tokens(other, _SUPPLY)
            their_want = _field_tokens(other, _WANT)

            n2o = _cosine(my_want, their_supply, idf)
            o2n = _cosine(my_supply, their_want, idf)
            guild = _jaccard(my_guilds, other.get("guilds"))
            presence = self._presence(oh)

            c_n2o = _saturate(n2o)
            c_o2n = _saturate(o2n)
            c_guild = _saturate(guild)
            # presence only matters when there is *some* topical fit; otherwise a
            # fresh-but-irrelevant card would score on recency alone.
            topical = max(c_n2o, c_o2n, c_guild)
            c_presence = presence * topical

            raw = (_W_N2O * c_n2o + _W_O2N * c_o2n +
                   _W_GUILD * c_guild + _W_PRESENCE * c_presence)
            score = round(10.0 * raw, 3)
            if score <= 0.0:
                continue
            results.append({
                "agent": other.get("handle"),
                "score": score,
                "why": self._why(other),
                "components": {
                    "need_to_offer": round(c_n2o, 3),
                    "offer_to_need": round(c_o2n, 3),
                    "guilds": round(c_guild, 3),
                    "presence": round(c_presence, 3),
                },
            })
        results.sort(key=lambda r: (-r["score"], str(r["agent"])))
        return results[:top_k]

    @staticmethod
    def _why(other):
        rep = other.get("represents") or other.get("handle") or "an agent"
        top = [str(o) for o in (other.get("offer") or [])[:3]]
        if top:
            return f"{rep} — offers {', '.join(top)}"
        return str(rep)


def build_engine(db_module):
    return MatchEngine(db_module)
