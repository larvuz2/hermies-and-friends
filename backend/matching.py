"""Deterministic, stdlib-only matching engine for the Hermix hub.

Score for a pair (me -> them):
  * my (need + curious + signals_wanted)  vs  their (offer + abilities)
  * their need                            vs  my (offer)
  * shared guilds

Matching is case-insensitive and TOKENIZED: every term is compared both as a
whole phrase and split into word tokens, so "3d worlds" matches "3d". Self
matches (same handle) are excluded. Signals sort by score desc, then handle
for a stable order.
"""
import re

_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokens(term: str) -> set:
    """Whole normalized phrase plus its word tokens."""
    t = str(term).lower().strip()
    if not t:
        return set()
    out = {t}
    for tok in _SPLIT.split(t):
        if tok:
            out.add(tok)
    return out


def _termset(items) -> set:
    """Flatten a list of terms into one token set."""
    out = set()
    for item in (items or []):
        out |= _tokens(item)
    return out


def _overlap(a_items, b_items) -> int:
    """Number of terms in a_items that share at least one token with b_items."""
    b_tokens = _termset(b_items)
    if not b_tokens:
        return 0
    hits = 0
    for item in (a_items or []):
        if _tokens(item) & b_tokens:
            hits += 1
    return hits


# --- public helpers reused by the semantic engine (engine.py) -------------
# engine.py leans on these for the deterministic, stdlib token/guild layer:
# they stay meaningful in fallback (pseudo-embedding) mode and provide the
# literal, grounded term pairs that the "why" string quotes.
def guild_overlap(a_guilds, b_guilds) -> int:
    """Count of a-guild terms sharing at least one token with b-guilds."""
    return _overlap(a_guilds, b_guilds)


def best_shared_pair(a_items, b_items):
    """Best literal (a_term, b_term) pair sharing the most tokens, or None.

    Used to ground the "why" string in real card terms. Prefers the pair with
    the largest token intersection so multi-word matches beat incidental ones.
    """
    best = None
    best_n = 0
    for a in (a_items or []):
        a_tok = _tokens(a)
        if not a_tok:
            continue
        for b in (b_items or []):
            n = len(a_tok & _tokens(b))
            if n > best_n:
                best_n = n
                best = (str(a), str(b))
    return best


def score_pair(me: dict, them: dict) -> int:
    """Directional affinity score of `me` toward `them`."""
    my_want = (me.get("need") or []) + (me.get("curious") or []) + \
        (me.get("signals_wanted") or [])
    their_supply = (them.get("offer") or []) + (them.get("abilities") or [])

    score = _overlap(my_want, their_supply)
    score += _overlap(them.get("need"), me.get("offer"))
    score += _overlap(me.get("guilds"), them.get("guilds"))
    return score


def _why(them: dict) -> str:
    represents = them.get("represents") or them.get("handle") or "an agent"
    top = [str(o) for o in (them.get("offer") or [])[:3]]
    return f"{represents} — offers {', '.join(top)}"


def match_signals(card: dict, others: list) -> list:
    """Build sorted match signals for `card` against every card in `others`.

    Excludes self (same handle). Only positive scores are returned.
    """
    my_handle = str(card.get("handle") or "").lower()
    signals = []
    for other in others:
        other_handle = str(other.get("handle") or "").lower()
        if other_handle and other_handle == my_handle:
            continue
        score = score_pair(card, other)
        if score > 0:
            signals.append({
                "kind": "match",
                "agent": other.get("handle"),
                "why": _why(other),
                "score": score,
            })
    signals.sort(key=lambda s: (-s["score"], str(s["agent"])))
    return signals
