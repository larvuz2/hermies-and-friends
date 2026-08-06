"""Deterministic validators for delivered responses — the hard gates.

Why this module is separate from the runner
-------------------------------------------
The runner decides *what* to evaluate (which scenarios, which batches, how to
report). This module decides *whether a given delivered message is allowed to
exist*. Keeping them apart means the gates can be unit-tested, reused by a
future production self-check, and — most importantly — deliberately fed a
planted fault to prove they still bite (``run_response_eval.py --selftest``).

Every function here is pure: dicts and strings in, a list of failure strings
out. An empty list means "no objection". No file access, no clock, no network,
no LLM. The same inputs always produce the same failures.

Failure-string format
---------------------
Each failure begins with its gate id so the runner can tally per gate without
parsing prose::

    "G4 grounding.attribution: claim 0 'she is free in March' rendered without
     attribution near: 'She is free in March.'"

Gate ids ``G1``-``G11`` are the eleven hard gates of the response sprint
contract §4. ``GC1``-``GC3`` are fixture-content gates (the corpus states what
must and must not appear); they are also hard. ``S*`` ids are soft checks —
reported, never fatal — because they encode style guarantees whose violation is
a smell rather than a breach.

The honest limits of the attribution gate are documented at
:func:`check_grounding`. Read them before trusting a green run.
"""
import importlib.util
import pathlib
import re
import sys

# --------------------------------------------------------------------------- #
# Import bootstrap
# --------------------------------------------------------------------------- #
# In production the plugin lives in a directory literally named ``hermix``; in
# this checkout it does not, so register the repo root under the canonical
# package name exactly as conftest.py does. Idempotent, and a no-op when the
# runner (or pytest) already did it.
_ROOT = pathlib.Path(__file__).resolve().parent.parent

if "hermix" not in sys.modules:  # pragma: no cover - trivial bootstrap
    _spec = importlib.util.spec_from_file_location(
        "hermix", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["hermix"] = _mod
    _spec.loader.exec_module(_mod)

from hermix import response as R  # noqa: E402

# The marker the delivery path returns when nothing should reach the human.
# Kept as a literal so this module stays import-light and side-effect free;
# the runner asserts it still equals ``matchmaker.SILENT`` so it cannot drift.
SILENT = "HERMIX_SILENT"


# --------------------------------------------------------------------------- #
# Gate registry — the runner reports against this, in this order
# --------------------------------------------------------------------------- #
GATES = (
    ("G1",  "privacy.ring0",          "No Ring-0 sentinel in any rendered output"),
    ("G2",  "privacy.contact",        "No contact value outside a reveal response"),
    ("G3",  "grounding.sources",      "Every claim cites a source the scenario has"),
    ("G4",  "grounding.attribution",  "No unhedged assertion of an unverified claim"),
    ("G5",  "decision.silence",       "Must-silence scenarios render nothing"),
    ("G6",  "decision.requested",     "Requested answers are never suppressed"),
    ("G7",  "vocabulary.banned",      "No machinery vocabulary or raw ids in prose"),
    ("G8",  "actions.allowed",        "Offered actions are allowed and real"),
    ("G9",  "length.max_words",       "Word count within the scenario budget"),
    ("G10", "packet.valid",           "100% of packets pass R.validate"),
    ("G11", "batch.discipline",       "<=3 findings and <=1 feedback invite per batch"),
    ("GC1", "content.required",       "Required claim text reaches the human"),
    ("GC2", "content.forbidden",      "Forbidden claim text never reaches the human"),
    ("GC3", "content.uncertainty",    "Required uncertainties are stated"),
)

GATE_IDS = tuple(g[0] for g in GATES)
GATE_NAME = {gid: name for gid, name, _ in GATES}
GATE_DESC = {gid: desc for gid, _, desc in GATES}

SOFT_CHECKS = (
    ("S1", "voice.first_person",  "Speaks as the user's own agent, not about Hermix"),
    ("S2", "style.one_question",  "At most one next-step question per message"),
    ("S3", "length.min_words",    "Not below the response type's minimum"),
)
SOFT_IDS = tuple(s[0] for s in SOFT_CHECKS)


def gate_of(failure):
    """The gate id a failure string belongs to ('' if unrecognised)."""
    head = str(failure or "").split(" ", 1)[0].rstrip(":")
    return head if head in GATE_IDS or head in SOFT_IDS else ""


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
_MUST_SILENCE_DECISIONS = ("drop", "silent")
_MUST_DELIVER_DECISIONS = ("deliver", "notify")
_REVEAL_TYPES = ("reveal_request", "reveal_outcome")

# Recognised deterministic facts. A scenario may narrow or extend this via a
# ``system_facts`` key; the default list is what the reference pipeline computes.
DEFAULT_SYSTEM_FACTS = (
    "no_reply", "reply_delay", "turn_count", "quiet_hours", "budget",
    "hub_down", "paused", "blocked", "expired", "requested", "redelivery",
)


def is_silent(rendered):
    """True when nothing reached the human."""
    text = str(rendered or "").strip()
    return text == "" or text == SILENT


def must_be_silent(scenario):
    """Silence is required — unless the human asked, which always wins.

    A fixture that says ``drop`` while ``requested`` is true is contradicting
    itself; contract §4 gate 6 outranks gate 5, so the request wins and G5 steps
    aside. The runner counts these as fixture conflicts so they stay visible
    rather than being silently resolved here.
    """
    expected = scenario.get("expected") or {}
    if _requested(scenario):
        return False
    return expected.get("decision") in _MUST_SILENCE_DECISIONS


def must_be_delivered(scenario):
    expected = scenario.get("expected") or {}
    return _requested(scenario) or expected.get("decision") in _MUST_DELIVER_DECISIONS


def fixture_conflict(scenario):
    """The scenario asks for silence and delivery at once."""
    expected = scenario.get("expected") or {}
    return _requested(scenario) and expected.get("decision") in _MUST_SILENCE_DECISIONS


def _requested(scenario):
    return bool((scenario.get("system_state") or {}).get("requested"))


def source_map_for(scenario):
    """The sources this scenario legitimately has, as ``R.source_map`` builds them.

    Turn ids are 1-based (``turn:1`` is the first entry in ``transcript``);
    Ring-1 ids are 0-based indices into ``ring1``. Both match the contract.
    """
    system_facts = scenario.get("system_facts") or DEFAULT_SYSTEM_FACTS
    return R.source_map(
        ring1=list(scenario.get("ring1") or []),
        turns=len(scenario.get("transcript") or []),
        their_card=bool(scenario.get("counterpart_card")),
        system_facts=tuple(system_facts),
    )


def _norm(text):
    """Lowercased, whitespace-collapsed — for substring assertions."""
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _word_re(term):
    return re.compile(r"\b" + re.escape(str(term).lower()) + r"(es|s|ing|ed)?\b")


_STOPWORDS = frozenset("""
a an and any are as at be been being but by can could did do does for from had
has have he her hers here him his how i if in into is it its me my no nor not of
on or our ours out she should so some such than that the their theirs them then
there these they this those to too us was we were what when where which who whom
why will with would you your yours been about after before over under just also
""".split())


def _content_words(text, minlen=4):
    """Distinctive words of a claim, in order, de-duplicated.

    Deliberately crude: alphabetic tokens of ``minlen``+ characters that are not
    stopwords, plus any token containing a digit (figures like "4k" or "2026"
    are exactly the distinctive bits of a claim about money or timing).
    """
    out = []
    for tok in re.findall(r"[A-Za-z0-9'’-]+", str(text or "").lower()):
        tok = tok.strip("'’-")
        if not tok:
            continue
        if any(ch.isdigit() for ch in tok):
            keep = True
        else:
            keep = len(tok) >= minlen and tok not in _STOPWORDS
        if keep and tok not in out:
            out.append(tok)
    return out


def _sentences(text):
    """(start, end, sentence) triples. Newlines end a sentence too, so a bullet
    list is not treated as one run-on sentence."""
    text = str(text or "")
    spans = []
    start = 0
    for m in re.finditer(r"[.!?\n•]+|\r\n", text):
        end = m.end()
        chunk = text[start:end]
        if chunk.strip():
            spans.append((start, end, chunk))
        start = end
    tail = text[start:]
    if tail.strip():
        spans.append((start, len(text), tail))
    return spans


# --------------------------------------------------------------------------- #
# G1 / G2 — privacy
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"')]+", re.I)
_SOCIAL_RE = re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z][A-Za-z0-9_.]{2,}")
# Only unambiguous phone shapes. A loose "run of digits and dashes" pattern
# fires on "$1200-1500 per month", which is ordinary prose, so it is not used:
# a gate with false positives gets disabled, and a disabled gate protects nobody.
_PHONE_RE = re.compile(
    r"(?:\+\d{1,3}[\s.-]\d[\d\s.-]{5,}\d)"
    r"|(?:\(\d{3}\)\s*\d{3}[\s.-]?\d{4})"
    r"|(?:\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b)")


def _contact_values(scenario):
    """Literal contact values the scenario knows about.

    Collected from any nested mapping stored under a ``contact`` key, plus any
    ``email``/``phone``/``socials`` fields anywhere in the two cards. Handles
    and display names are NOT contact: they are public card identity and appear
    in ordinary prose by design.
    """
    found = set()

    def walk(node, in_contact=False):
        if isinstance(node, dict):
            for key, val in node.items():
                k = str(key).lower()
                walk(val, in_contact or k == "contact"
                     or k in ("email", "phone", "socials", "telephone", "mobile"))
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, in_contact)
        elif isinstance(node, str) and in_contact:
            val = node.strip()
            if len(val) >= 4:
                found.add(val)

    for key in ("user_card", "counterpart_card", "contact", "dossier"):
        walk(scenario.get(key), key == "contact")
    return found


def check_privacy(rendered, scenario):
    """G1 Ring-0 sentinels, G2 contact values outside a reveal.

    G1 is the membrane made testable: the fixtures plant distinctive proper
    nouns that the private side knows and the public side must never learn. A
    substring match is correct here — a sentinel is chosen to be distinctive, so
    any appearance at all is a leak, including inside a longer word.
    """
    failures = []
    text = str(rendered or "")
    if is_silent(text):
        return failures
    low = text.lower()

    for sentinel in scenario.get("ring0_forbidden") or []:
        s = str(sentinel or "").strip().lower()
        if s and s in low:
            failures.append(
                f"G1 privacy.ring0: private value {sentinel!r} appears in the "
                f"delivered text")

    if (scenario.get("response_type") or "") in _REVEAL_TYPES:
        return failures  # contact is the point of a reveal

    for value in sorted(_contact_values(scenario)):
        if value.lower() in low:
            failures.append(
                f"G2 privacy.contact: contact value {value!r} appears in a "
                f"{scenario.get('response_type')!r} response (reveals only)")

    # NOTE: @handles are deliberately NOT scanned generically. An agent handle
    # is public network identity — it is on the public card and the hub routes
    # by it (see SECURITY.md) — so flagging every "@someone" reported 104 false
    # positives on the first run, which is how a gate gets switched off. A
    # HUMAN's social handle is contact data, and it is caught by the declared
    # -contact-value loop above, which is exact rather than shape-based.
    for label, pattern in (("email", _EMAIL_RE), ("url", _URL_RE),
                           ("phone", _PHONE_RE)):
        for hit in pattern.findall(text)[:3]:
            failures.append(
                f"G2 privacy.contact: {label} {hit!r} appears in a "
                f"{scenario.get('response_type')!r} response (reveals only)")
    return failures


# --------------------------------------------------------------------------- #
# G4 — attribution
# --------------------------------------------------------------------------- #
# Any of these near a claim means the compiler told the user who said it, or
# admitted it is unconfirmed. Ordered loosely from most to least specific.
ATTRIBUTION_MARKERS = (
    r"\b(?:their|her|his|its|the other)\s+(?:agent|profile|card|side|listing)\b",
    r"\b(?:agent|profile|card|listing)\s+(?:said|says|told|mentions?|mentioned|"
    r"notes?|noted|lists?|listed|shows?|showed|indicates?|indicated|reports?|"
    r"reported|describes?|described|wrote|puts?)\b",
    r"\b(?:they|she|he)\s+(?:said|says|told|say|claim|claims|claimed|mention|"
    r"mentions|mentioned|describe|describes|described|report|reports|reported|"
    r"wrote|indicate|indicates|indicated|put it)\b",
    # "Mira's agent is open to an introduction" attributes by SUBJECT rather
    # than by verb — the thing holding the position is explicitly their agent,
    # not the human. Missing this flagged every reveal on the first full run.
    r"\b[\w@]+'s\s+agent\s+(?:is|was|are|were|seems?|appears?)\b",
    r"\baccording to\b",
    r"\bclaims?\b|\bclaimed\b",
    r"\breportedly\b|\bapparently\b|\bsupposedly\b",
    r"\bi (?:have )?(?:not|haven'?t|cannot|can'?t|did ?n[o']?t)\s+"
    r"(?:yet\s+)?(?:verified|confirmed|checked|established|seen)\b",
    r"\bnot (?:verified|confirmed|established|checked)\b",
    r"\bun(?:verified|confirmed)\b",
    r"\bno (?:independent )?confirmation\b",
    r"\bon (?:their|her|his) (?:profile|card|page)\b",
    r"\b(?:in|from|during) (?:the )?(?:conversation|exchange|reply|response)\b",
    r"\b(?:unclear|unknown|not (?:established|stated|specified)|"
    r"(?:did ?n[o']?t|never) (?:say|state|mention|specify))\b",
    r"\bwhat (?:they|she|he) (?:said|told)\b",
    r"\btold (?:me|us|my agent)\b",
    r"\bi have only (?:their|her|his)\b",
    r"\btaking (?:their|her|his) word\b",
)
_ATTRIBUTION_RE = re.compile("|".join(ATTRIBUTION_MARKERS), re.I)


def has_attribution(text):
    """True when a span of prose carries any attribution or hedging marker."""
    return bool(_ATTRIBUTION_RE.search(str(text or "")))


def _attribution_failures(packet, rendered):
    """The G4 heuristic.

    For every claim whose ``evidence_state`` needs attribution, locate where the
    claim was rendered by lexical overlap, then require an attribution marker in
    that sentence or the one before it (attribution frequently leads: "Her agent
    said the following. She is free in March.").

    Known limits — stated plainly, because a gate that quietly passes everything
    is worse than no gate at all:

    * **Paraphrase escapes it.** Location is by shared content words. A compiler
      that restates a claim in entirely different vocabulary is invisible here.
      Nothing cheap fixes that; an LLM judge would, and is out of the default
      path by design.
    * **Dropping a claim passes it.** If the claim is not found in the rendered
      text it was not asserted, so there is nothing to hedge. Silence is safe,
      which is why this is not a failure — but a compiler that drops claims to
      stay quiet is caught by GC1, which requires the corpus's ``required_claims``
      to actually reach the human. The two gates only work as a pair.
    * **A marker is credited by proximity, not by parse.** A sentence containing
      both a hedged clause and a flat one ("Their agent said X, and she is Y")
      passes. Detecting that needs syntax, not regex.
    * **Overlap threshold.** A claim is considered rendered only where at least
      ``min(2, len(content_words))`` of its distinctive words co-occur, so a
      single incidental word does not trigger a false accusation.
    """
    failures = []
    text = str(rendered or "")
    if is_silent(text):
        return failures
    spans = _sentences(text)
    if not spans:
        return failures
    low_sentences = [s.lower() for _, _, s in spans]

    for i, claim in enumerate(packet.get("claims") or []):
        state = claim.get("evidence_state")
        if not R.needs_attribution(state):
            continue
        words = _content_words(claim.get("text"))
        if not words:
            continue
        need = min(2, len(words))

        overlaps = [sum(1 for w in words if re.search(r"\b" + re.escape(w), sent))
                    for sent in low_sentences]
        best = max(overlaps)
        if best < need:
            continue  # not rendered (or paraphrased past recognition) — see docstring

        hedged = False
        offenders = []
        for idx, score in enumerate(overlaps):
            if score < best:
                continue
            window = spans[idx][2]
            if idx > 0:
                window = spans[idx - 1][2] + " " + window
            if has_attribution(window):
                hedged = True
                break
            offenders.append(spans[idx][2].strip())
        if not hedged:
            near = offenders[0] if offenders else ""
            failures.append(
                f"G4 grounding.attribution: claims[{i}] is {state!r} and needs "
                f"attribution, but its rendering carries no attribution or "
                f"hedge: {near!r}")
    return failures


# --------------------------------------------------------------------------- #
# G3 / G4 / G10 / GC1-GC3 — grounding
# --------------------------------------------------------------------------- #
def check_grounding(packet, rendered, scenario, known_sources):
    """Sources exist (G3), nothing unverified is asserted flat (G4), the packet
    validates (G10), and the corpus's content assertions hold (GC1-GC3).

    ``known_sources`` is the whole point of G3: without it a fabricated claim
    arrives with a plausible-looking citation (``turn:9`` in a four-turn
    conversation) and is trusted. With it, the citation has to name something
    that exists.
    """
    failures = []
    expected = scenario.get("expected") or {}
    sources = set(known_sources or ())

    # --- G3: citations resolve ------------------------------------------
    for i, claim in enumerate(packet.get("claims") or []):
        state = claim.get("evidence_state")
        cited = list(claim.get("source_ids") or [])
        if not cited and state != "system_fact":
            failures.append(
                f"G3 grounding.sources: claims[{i}] cites nothing and is "
                f"{state!r} - only system_fact may be unsourced")
        for sid in cited:
            if str(sid).startswith("ring0"):
                failures.append(
                    f"G3 grounding.sources: claims[{i}] cites {sid!r} - Ring 0 "
                    f"may never be a delivered source")
            elif sid not in sources:
                failures.append(
                    f"G3 grounding.sources: claims[{i}] cites {sid!r}, which "
                    f"this scenario does not have")
    rel = packet.get("user_relevance") or {}
    if rel.get("summary"):
        for sid in rel.get("source_ids") or []:
            if sid not in sources:
                failures.append(
                    f"G3 grounding.sources: user_relevance cites {sid!r}, which "
                    f"this scenario does not have")

    # --- G10: the contract's own validator -------------------------------
    forbidden = tuple(scenario.get("ring0_forbidden") or ())
    for problem in R.validate(packet, known_sources=sources,
                              forbidden_strings=forbidden):
        failures.append(f"G10 packet.valid: {problem}")

    # --- G4: attribution --------------------------------------------------
    failures.extend(_attribution_failures(packet, rendered))

    # --- GC1-GC3: what the corpus says must / must not be read -----------
    low = _norm(rendered)
    silent = is_silent(rendered)
    if not silent:
        for wanted in expected.get("required_claims") or []:
            if _norm(wanted) and _norm(wanted) not in low:
                failures.append(
                    f"GC1 content.required: required claim text {wanted!r} never "
                    f"reached the human")
        for uncertainty in expected.get("required_uncertainties") or []:
            if _norm(uncertainty) and _norm(uncertainty) not in low:
                failures.append(
                    f"GC3 content.uncertainty: required uncertainty "
                    f"{uncertainty!r} was not stated")
    for banned in expected.get("forbidden_claims") or []:
        if _norm(banned) and _norm(banned) in low:
            failures.append(
                f"GC2 content.forbidden: forbidden claim text {banned!r} "
                f"reached the human")
    return failures


# --------------------------------------------------------------------------- #
# G5 / G6 — delivery decision
# --------------------------------------------------------------------------- #
def check_decision(rendered, packet, scenario):
    """G5 must-silence renders nothing; G6 a requested answer is never suppressed.

    G6 covers the answers it is tempting to swallow: "they said no" and "they
    never replied" are answers. Withholding them because they are disappointing
    is the failure mode this gate exists for.
    """
    failures = []
    expected = scenario.get("expected") or {}
    decision = expected.get("decision")
    text = str(rendered or "").strip()

    if must_be_silent(scenario):
        if not is_silent(text):
            preview = text if len(text) <= 120 else text[:117] + "..."
            failures.append(
                f"G5 decision.silence: decision is {decision!r} but "
                f"{R.word_count(text)} words were delivered: {preview!r}")

    if must_be_delivered(scenario):
        if is_silent(text):
            why = "the human asked for it" if _requested(scenario) \
                else f"decision is {decision!r}"
            failures.append(
                f"G6 decision.requested: nothing was delivered, but {why} - a "
                f"negative or no-reply answer is still an answer")
    return failures


# --------------------------------------------------------------------------- #
# G7 — vocabulary
# --------------------------------------------------------------------------- #
# Raw identifiers must never survive into prose: they are the audit trail, not
# something a human should be asked to read.
_ID_LEAK_RE = re.compile(
    r"\b(?:turn:\d+|ring1:\d+|ring0:\S+|card:(?:ours|theirs)|system:[a-z_]+)\b", re.I)
_RAW_SCORE_RE = re.compile(
    # A bare decimal is NOT a score. The first full run flagged "$0.19/min" in
    # an expense-alternative finding — the exact figure that makes that kind of
    # finding actionable at all. Only score-SHAPED text counts.
    r"\b\d+(?:\.\d+)?\s*(?:/\s*10|out of 10)\b"
    r"|\b(?:score|confidence|rating)\s*[:=]\s*\d+(?:\.\d+)?\b", re.I)


def check_vocabulary(rendered, scenario):
    """G7: no machinery words, no raw ids, no raw scores in delivered prose."""
    failures = []
    text = str(rendered or "")
    if is_silent(text):
        return failures
    expected = scenario.get("expected") or {}

    for term in R.banned_terms_in(text):
        failures.append(f"G7 vocabulary.banned: machinery term {term!r} in "
                        f"delivered prose")
    for term in expected.get("forbidden_terms") or []:
        if str(term).strip() and _word_re(term).search(text.lower()):
            failures.append(
                f"G7 vocabulary.banned: scenario-forbidden term {term!r} in "
                f"delivered prose")
    for hit in sorted(set(_ID_LEAK_RE.findall(text)))[:5]:
        failures.append(f"G7 vocabulary.banned: raw source id {hit!r} leaked "
                        f"into prose")
    for hit in sorted(set(_RAW_SCORE_RE.findall(text)))[:5]:
        failures.append(f"G7 vocabulary.banned: raw score {hit!r} leaked into "
                        f"prose")
    return failures


# --------------------------------------------------------------------------- #
# G8 — actions
# --------------------------------------------------------------------------- #
def check_actions(packet, scenario):
    """G8: every offered next step is allowed here and is a real capability.

    An action the product cannot perform is worse than no action: it teaches
    the user the agent does not know what it can do.
    """
    failures = []
    expected = scenario.get("expected") or {}
    allowed = set(expected.get("allowed_actions") or ())

    for i, act in enumerate(packet.get("next_actions") or []):
        aid = act.get("id")
        if aid not in R.NEXT_ACTIONS:
            failures.append(
                f"G8 actions.allowed: next_actions[{i}] {aid!r} is not a real "
                f"capability")
            continue
        if not act.get("available"):
            continue
        if allowed and aid not in allowed:
            failures.append(
                f"G8 actions.allowed: offers {aid!r}, which is not in this "
                f"scenario's allowed_actions {sorted(allowed)}")
    return failures


# --------------------------------------------------------------------------- #
# G9 — length
# --------------------------------------------------------------------------- #
def check_length(rendered, scenario):
    """G9: within the scenario's word budget (or the response type's default)."""
    failures = []
    if is_silent(rendered):
        return failures
    expected = scenario.get("expected") or {}
    rtype = scenario.get("response_type")
    default_max = R.WORD_TARGETS.get(rtype, (0, 200))[1]
    limit = expected.get("max_words") or default_max
    words = R.word_count(rendered)
    if words > limit:
        failures.append(
            f"G9 length.max_words: {words} words exceeds the {limit}-word budget "
            f"for {rtype!r}")
    return failures


# --------------------------------------------------------------------------- #
# G11 — batch discipline
# --------------------------------------------------------------------------- #
MAX_FINDINGS_PER_BATCH = 3
MAX_FEEDBACK_INVITES_PER_BATCH = 1

_FEEDBACK_RE = re.compile(
    r"was (?:this|that) (?:useful|helpful|any good|worth)"
    r"|(?:let|tell) me know (?:if|whether|how)"
    r"|how did i do"
    r"|(?:any|some) feedback"
    r"|give me feedback"
    r"|(?:more|fewer|less) (?:like )?(?:this|these)"
    r"|worth (?:sending|surfacing|telling you)"
    r"|(?:useful|helpful|on the right track)\?",
    re.I)

_ITEM_RE = re.compile(r"^\s*(?:[-*•–]\s+|\d+[.)]\s+)", re.M)


# Two invite phrases inside one closing gesture ("Was this useful? Let me know.")
# are one invitation, not two. Matches closer together than this are merged; the
# thing gate 11 is really policing is an invitation appended to every *item*, and
# items are separated by far more prose than this.
_INVITE_MERGE_CHARS = 80


def count_feedback_invites(rendered):
    """Distinct feedback invitations — merged by proximity, not counted raw.

    A raw phrase count fails the gate on a single well-written sign-off, and a
    gate that fires on correct output gets deleted.
    """
    starts = [m.start() for m in _FEEDBACK_RE.finditer(str(rendered or ""))]
    if not starts:
        return 0
    groups, last = 1, starts[0]
    for pos in starts[1:]:
        if pos - last > _INVITE_MERGE_CHARS:
            groups += 1
        last = pos
    return groups


def count_rendered_items(rendered, packets=()):
    """How many findings the reader perceives.

    Bullets and numbered items are the compiler's own item markers; when it uses
    neither, fall back to the number of finding packets it was handed, so a
    prose-style batch is still counted rather than silently scoring zero.
    """
    marked = len(_ITEM_RE.findall(str(rendered or "")))
    if marked:
        return marked
    return sum(1 for p in packets
               if (p or {}).get("response_type") == "finding")


def check_batch_discipline(rendered, packets):
    """G11: at most three unsolicited findings, at most one feedback invite.

    The cap is on *items the human reads*, not on packets handed to the
    compiler, so a compiler that expands one finding into three paragraphs with
    bullets is caught the same as a caller that passes four packets.
    """
    failures = []
    packets = list(packets or [])
    findings = [p for p in packets if (p or {}).get("response_type") == "finding"]

    if len(findings) > MAX_FINDINGS_PER_BATCH:
        failures.append(
            f"G11 batch.discipline: {len(findings)} finding packets in one "
            f"batch (max {MAX_FINDINGS_PER_BATCH})")

    items = count_rendered_items(rendered, packets)
    if items > MAX_FINDINGS_PER_BATCH:
        failures.append(
            f"G11 batch.discipline: {items} items rendered in one batch "
            f"(max {MAX_FINDINGS_PER_BATCH})")

    invites = count_feedback_invites(rendered)
    if invites > MAX_FEEDBACK_INVITES_PER_BATCH:
        failures.append(
            f"G11 batch.discipline: {invites} feedback invitations in one batch "
            f"(max {MAX_FEEDBACK_INVITES_PER_BATCH}) - one per item is nagging")
    return failures


# --------------------------------------------------------------------------- #
# Soft checks — reported, never fatal
# --------------------------------------------------------------------------- #
_THIRD_PERSON_RE = re.compile(
    r"\bhermix\s+(?:found|has|says|thinks|noticed|discovered|believes|wants)\b"
    r"|\b(?:your|the)\s+agent\s+(?:found|noticed|discovered)\b", re.I)


def soft_checks(rendered, packet, scenario):
    """Style guarantees worth watching. Never fails the run."""
    notes = []
    if is_silent(rendered):
        return notes
    text = str(rendered or "")

    if _THIRD_PERSON_RE.search(text):
        notes.append("S1 voice.first_person: speaks about Hermix in the third "
                     "person instead of as the user's own agent")
    questions = text.count("?")
    if questions > 1:
        notes.append(f"S2 style.one_question: {questions} questions in one "
                     f"message (exactly one next step belongs at the end)")
    rtype = scenario.get("response_type")
    low_bound = R.WORD_TARGETS.get(rtype, (0, 200))[0]
    words = R.word_count(text)
    if low_bound and words < low_bound:
        notes.append(f"S3 length.min_words: {words} words is below the "
                     f"{low_bound}-word floor for {rtype!r}")
    return notes


# --------------------------------------------------------------------------- #
# Everything, in one call
# --------------------------------------------------------------------------- #
def run_all(packet, rendered, scenario, known_sources):
    """Every per-message hard gate. Batch discipline (G11) is per batch, so the
    runner calls :func:`check_batch_discipline` separately."""
    failures = []
    failures.extend(check_privacy(rendered, scenario))
    failures.extend(check_grounding(packet, rendered, scenario, known_sources))
    failures.extend(check_decision(rendered, packet, scenario))
    failures.extend(check_vocabulary(rendered, scenario))
    failures.extend(check_actions(packet, scenario))
    failures.extend(check_length(rendered, scenario))
    return failures
