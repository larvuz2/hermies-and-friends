"""Response-quality harness for Hermix.

Runs every scenario in the deterministic response corpus through the packet
contract and the prose compiler, then applies the eleven hard gates of the
response sprint (docs/RESPONSE-SPRINT-CONTRACT.md §4) to what the human would
actually read:

    G1  no Ring-0 sentinel in delivered text
    G2  no contact value outside a reveal
    G3  every claim cites a source the scenario actually has
    G4  nothing unverified is asserted flat
    G5  must-silence scenarios render nothing
    G6  a requested answer is never suppressed (including "no" and "no reply")
    G7  no machinery vocabulary, raw ids or raw scores in prose
    G8  every offered action is allowed here and is a real capability
    G9  word count within the scenario's budget
    G10 100% of packets pass R.validate
    G11 <=3 findings and <=1 feedback invitation per batch

plus three content gates the corpus itself asserts (GC1 required claim text
reaches the human, GC2 forbidden text never does, GC3 required uncertainties are
stated) and a handful of non-fatal style checks.

Two paths, and the difference matters
-------------------------------------
**Reference path (default).** ``build_packet_from_scenario`` assembles a packet
deterministically from the fixture's ``expected`` block. It grounds each
required claim in whichever real transcript turn, Ring-1 fact or card field
lexically supports it, so citations point at things that exist. This exercises
the *gates and the compiler* without a model in the loop — it is what makes the
harness runnable in CI, offline, in milliseconds.

**Production path.** The real judge emits the packet; everything downstream is
identical. Swapping it in means replacing ``build_packet_from_scenario`` with
the judge call. The reference path deliberately cannot fail G3, G8 or G10 in
interesting ways — it builds well-formed packets by construction. What it does
test hard is the compiler (G1, G2, G4, G7, G9, G11) and the delivery decision
(G5, G6). Do not read a green reference run as evidence about the judge.

``--selftest`` plants three faults and requires the harness to catch all three.
It needs neither the corpus nor the compiler, so it works before either lands.

Usage::

    python evals/run_response_eval.py            # --fast (a 1-in-3 subset)
    python evals/run_response_eval.py --full
    python evals/run_response_eval.py --selftest
    python evals/run_response_eval.py --full --json out.json --md out.md
"""
import argparse
import hashlib
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent

# Register the repo root as the ``hermix`` package (conftest.py does the same for
# pytest; this script runs standalone).
if "hermix" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "hermix", REPO / "__init__.py", submodule_search_locations=[str(REPO)])
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["hermix"] = _mod
    _spec.loader.exec_module(_mod)

sys.path.insert(0, str(HERE))

from hermix import response as R          # noqa: E402
import response_gates as G                # noqa: E402  (local module, evals/)

DEFAULT_JSON = HERE / "RESPONSE-REPORT.json"
DEFAULT_MD = HERE / "RESPONSE-REPORT.md"

# Batches are assembled from consecutive finding packets. Three is the ceiling
# the contract sets, so batches are built AT the ceiling: a compiler that adds a
# fourth item or a second feedback invite has nowhere to hide.
BATCH_SIZE = 3
MAX_BATCHES_FAST = 8


# --------------------------------------------------------------------------- #
# Optional dependencies — both are written by other hands in this sprint
# --------------------------------------------------------------------------- #
def load_corpus():
    """evals/response_corpus.py, or None with a reason."""
    if not (HERE / "response_corpus.py").exists():
        return None, "evals/response_corpus.py does not exist yet"
    try:
        import response_corpus
        return response_corpus, ""
    except Exception as exc:
        return None, f"evals/response_corpus.py failed to import ({exc!r})"


def load_render():
    """hermix/render.py, or None with a reason."""
    if not (REPO / "render.py").exists():
        return None, "hermix/render.py does not exist yet"
    try:
        from hermix import render
        return render, ""
    except Exception as exc:
        return None, f"hermix/render.py failed to import ({exc!r})"


# --------------------------------------------------------------------------- #
# Reference packet construction
# --------------------------------------------------------------------------- #
def _strings_in(node):
    """Every string inside a nested structure."""
    out = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            out.extend(_strings_in(v))
    elif isinstance(node, (list, tuple)):
        for v in node:
            out.extend(_strings_in(v))
    return out


def _pick_source(text, scenario):
    """Ground a claim in whatever the scenario actually contains.

    Returns ``(evidence_state, source_ids)``. Selection is by lexical overlap
    with the distinctive words of the claim, scanning transcript turns first
    (most specific), then approved Ring-1 facts, then the counterpart's public
    card. Ties go to the earliest candidate, so the result is stable.

    The evidence state follows from where it was found, which is the whole
    point: something a counterpart's agent said is a ``counterpart_claim`` and
    must be attributed; something only their card asserts is ``profile_only``;
    something both sides worked out is ``conversation_established``.
    """
    words = set(G._content_words(text))
    best_overlap, best_state, best_sid = 0, None, None

    for n, turn in enumerate(scenario.get("transcript") or [], start=1):
        overlap = len(words & set(G._content_words(turn.get("text"))))
        if overlap > best_overlap:
            state = ("counterpart_claim" if turn.get("from") == "them"
                     else "conversation_established")
            best_overlap, best_state, best_sid = overlap, state, f"turn:{n}"

    for i, fact in enumerate(scenario.get("ring1") or []):
        overlap = len(words & set(G._content_words(fact)))
        if overlap > best_overlap:
            best_overlap, best_state, best_sid = (
                overlap, "independently_verified", f"ring1:{i}")

    if scenario.get("counterpart_card"):
        card = " ".join(_strings_in(scenario["counterpart_card"]))
        overlap = len(words & set(G._content_words(card)))
        if overlap > best_overlap:
            best_overlap, best_state, best_sid = overlap, "profile_only", "card:theirs"

    if best_overlap:
        return best_state, [best_sid]

    # Nothing in the scenario lexically supports the text. Attribute it to the
    # last thing the counterpart said if they said anything; if they never
    # replied, it can only be something we computed.
    them = [n for n, t in enumerate(scenario.get("transcript") or [], start=1)
            if t.get("from") == "them"]
    if them:
        return "counterpart_claim", [f"turn:{them[-1]}"]
    return "system_fact", []


def _finding_id(scenario):
    """Stable across runs and machines — ``hash()`` is not."""
    return hashlib.sha1(str(scenario.get("id", "")).encode("utf-8")).hexdigest()[:8]


def build_packet_from_scenario(scenario):
    """A deterministic *reference* packet for one fixture.

    THIS IS THE REFERENCE PATH. In production the judge produces the packet and
    this function does not run; the fixture's ``expected`` block is the
    specification the judge is measured against, not its input. Building the
    packet from ``expected`` makes the compiler and the gates testable today,
    before a model is wired in, and keeps CI offline and instant.

    Consequences worth stating out loud: claims here are guaranteed to be
    grounded (G3), actions are drawn from ``allowed_actions`` (G8), and the
    packet is well-formed (G10). Those three gates are therefore near-vacuous on
    this path — they earn their keep against the judge. The compiler gates are
    fully live.
    """
    expected = scenario.get("expected") or {}
    rtype = scenario.get("response_type")
    card = scenario.get("counterpart_card") or {}

    claims = []
    for text in expected.get("required_claims") or []:
        text = str(text).strip()
        if not text:
            continue
        state, sources = _pick_source(text, scenario)
        claims.append(R.claim(text, state, sources))

    # Types that must say something even when the fixture asserts no substring:
    # an ask with no answer is still an answer ("they never replied").
    if not claims and rtype in ("ask_result", "finding", "checkin"):
        if not scenario.get("transcript"):
            claims.append(R.claim("Their agent has not replied yet.",
                                  "system_fact", []))
        else:
            last = (scenario["transcript"][-1] or {}).get("text", "")
            state, sources = _pick_source(last, scenario)
            claims.append(R.claim(str(last).strip() or "Nothing new was established.",
                                  state, sources))

    # Relevance is grounded in an approved Ring-1 fact when one exists. Skipped
    # if that fact happens to contain machinery vocabulary — the reference path
    # should never be the reason G7 fails, or the gate stops meaning anything.
    relevance = {}
    ring1 = list(scenario.get("ring1") or [])
    if ring1 and not R.banned_terms_in(ring1[0]):
        relevance = {"summary": str(ring1[0]).strip(), "source_ids": ["ring1:0"]}

    actions = [R.action(a) for a in (expected.get("allowed_actions") or [])
               if a in R.NEXT_ACTIONS]

    contact = {}
    if rtype == "reveal_request":
        # A preview names the FIELDS that would be shared, never the values.
        contact = dict(scenario.get("contact") or
                       {"fields": "your name and email address"})

    system = {}
    if rtype in ("error", "safety"):
        system = {"reason": str(scenario.get("category") or "")}

    state = scenario.get("system_state") or {}
    return R.packet(
        rtype,
        finding_id=_finding_id(scenario),
        counterpart={"handle": card.get("handle", ""),
                     "display": card.get("display", card.get("name", ""))},
        user_relevance=relevance,
        claims=claims,
        uncertainties=list(expected.get("required_uncertainties") or []),
        next_actions=actions,
        delivery={"requested": bool(state.get("requested")),
                  "redelivery": bool(state.get("redelivery")),
                  "batch_position": 1, "batch_size": 1},
        contact=contact,
        system=system,
    )


def render_for_scenario(scenario, packet, render_mod):
    """What the human would read. Silence is a delivery decision, not a render.

    The compiler is never asked to render a message that must not be sent — the
    matchmaker decides that upstream and returns the SILENT marker. Modelling it
    the same way here keeps G5 honest: it tests the decision, not the prose.
    """
    if G.must_be_silent(scenario):
        return G.SILENT
    return render_mod.render(packet)


# --------------------------------------------------------------------------- #
# Scenario selection
# --------------------------------------------------------------------------- #
def select(scenarios, fast=True):
    """--full is everything; --fast is a deterministic 1-in-3 stride per category.

    Striding within a category (rather than taking a prefix) keeps every
    category represented and keeps whatever ordering the corpus author used to
    vary difficulty from collapsing into "all the easy ones".
    """
    ordered = sorted(scenarios, key=lambda s: (s.get("category", ""), s.get("id", "")))
    if not fast:
        return ordered
    picked, seen = [], {}
    for sc in ordered:
        cat = sc.get("category", "")
        idx = seen.get(cat, 0)
        seen[cat] = idx + 1
        if idx % 3 == 0:
            picked.append(sc)
    return picked


def build_batches(records, limit=None):
    """Group consecutive rendered findings into batches at the contract ceiling."""
    findings = [r for r in records
                if r["response_type"] == "finding" and not G.is_silent(r["rendered"])]
    batches = [findings[i:i + BATCH_SIZE]
               for i in range(0, len(findings), BATCH_SIZE)]
    batches = [b for b in batches if len(b) == BATCH_SIZE]
    if limit is not None:
        batches = batches[:limit]
    return batches


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def evaluate_scenario(scenario, render_mod):
    packet, rendered, failures = None, "", []
    try:
        packet = build_packet_from_scenario(scenario)
    except Exception as exc:
        failures.append(f"ERR packet build raised {exc!r}")
    if packet is not None:
        try:
            rendered = render_for_scenario(scenario, packet, render_mod)
        except Exception as exc:
            failures.append(f"ERR render raised {exc!r}")
            rendered = ""
        failures.extend(G.run_all(packet, rendered, scenario,
                                  G.source_map_for(scenario)))
    soft = G.soft_checks(rendered, packet or {}, scenario) if packet else []
    return {
        "id": scenario.get("id"),
        "category": scenario.get("category"),
        "response_type": scenario.get("response_type"),
        "decision": (scenario.get("expected") or {}).get("decision"),
        "requested": bool((scenario.get("system_state") or {}).get("requested")),
        "silent": G.is_silent(rendered),
        "words": R.word_count(rendered) if not G.is_silent(rendered) else 0,
        "rendered": rendered,
        "packet": packet,
        "failures": failures,
        "soft": soft,
        "fixture_conflict": G.fixture_conflict(scenario),
    }


def evaluate_batch(batch, render_mod):
    """G11 plus the privacy/vocabulary/length gates that apply to batch prose."""
    packets = [r["packet"] for r in batch]
    ids = [r["id"] for r in batch]
    failures = []
    try:
        rendered = render_mod.render_batch(packets)
    except Exception as exc:
        return {"ids": ids, "rendered": "", "words": 0,
                "failures": [f"ERR render_batch raised {exc!r}"]}

    failures.extend(G.check_batch_discipline(rendered, packets))

    # A batch inherits every member's negative assertions.
    merged = {
        "response_type": "finding",
        "ring0_forbidden": [s for r in batch
                            for s in (r["scenario"].get("ring0_forbidden") or [])],
        "user_card": batch[0]["scenario"].get("user_card"),
        "counterpart_card": None,
        "expected": {"forbidden_terms": [t for r in batch
                                         for t in ((r["scenario"].get("expected") or {})
                                                   .get("forbidden_terms") or [])],
                     "max_words": R.WORD_TARGETS["finding_batch"][1]},
    }
    failures.extend(G.check_privacy(rendered, merged))
    failures.extend(G.check_vocabulary(rendered, merged))
    failures.extend(G.check_length(rendered, merged))
    return {"ids": ids, "rendered": rendered,
            "words": R.word_count(rendered), "failures": failures}


def tally(records, batch_results):
    per_gate = {gid: 0 for gid in G.GATE_IDS}
    per_gate_scenarios = {gid: [] for gid in G.GATE_IDS}
    other = []
    soft_counts = {sid: 0 for sid in G.SOFT_IDS}
    by_category = {}

    for rec in records:
        cat = by_category.setdefault(rec["category"], {"n": 0, "pass": 0, "fail": 0})
        cat["n"] += 1
        if rec["failures"]:
            cat["fail"] += 1
        else:
            cat["pass"] += 1
        for f in rec["failures"]:
            gid = G.gate_of(f)
            if gid in per_gate:
                per_gate[gid] += 1
                if rec["id"] not in per_gate_scenarios[gid]:
                    per_gate_scenarios[gid].append(rec["id"])
            else:
                other.append(f"{rec['id']}: {f}")
        for s in rec["soft"]:
            sid = G.gate_of(s)
            if sid in soft_counts:
                soft_counts[sid] += 1

    for br in batch_results:
        for f in br["failures"]:
            gid = G.gate_of(f)
            if gid in per_gate:
                per_gate[gid] += 1
                label = "+".join(br["ids"])
                if label not in per_gate_scenarios[gid]:
                    per_gate_scenarios[gid].append(label)
            else:
                other.append(f"batch[{'+'.join(br['ids'])}]: {f}")

    n_fail = sum(1 for r in records if r["failures"])
    return {
        "n_scenarios": len(records),
        "n_pass": len(records) - n_fail,
        "n_fail": n_fail,
        "n_batches": len(batch_results),
        "n_batch_fail": sum(1 for b in batch_results if b["failures"]),
        "per_gate": per_gate,
        "per_gate_scenarios": per_gate_scenarios,
        "other": other,
        "soft": soft_counts,
        "by_category": by_category,
        "fixture_conflicts": [r["id"] for r in records if r["fixture_conflict"]],
        "silent": sum(1 for r in records if r["silent"]),
        "delivered": sum(1 for r in records if not r["silent"]),
        "max_words": max([r["words"] for r in records] or [0]),
        "all_pass": n_fail == 0 and not other
                    and all(not b["failures"] for b in batch_results),
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def scorecard_lines(mode, corpus_note, render_note, cstats, m, records, batch_results):
    L = []
    L.append("=" * 74)
    L.append("  HERMIX RESPONSE QUALITY SCORECARD")
    L.append("=" * 74)
    L.append(f"  packet contract : {R.CONTRACT_VERSION}")
    L.append(f"  corpus          : {corpus_note}")
    L.append(f"  compiler        : {render_note}")
    L.append(f"  path            : reference (deterministic packet builder) - "
             f"the judge is NOT under test")
    L.append(f"  mode            : {mode}")
    if cstats:
        L.append(f"  corpus totals   : {cstats.get('total')} scenarios, "
                 f"{len(cstats.get('by_category') or {})} categories")
    L.append(f"  evaluated       : {m['n_scenarios']} scenarios "
             f"({m['delivered']} delivered, {m['silent']} silent), "
             f"{m['n_batches']} batches")
    L.append("-" * 74)
    L.append("  CATEGORY                          N   PASS   FAIL")
    L.append("-" * 74)
    for cat in sorted(m["by_category"]):
        c = m["by_category"][cat]
        L.append(f"  {cat:<30} {c['n']:>4} {c['pass']:>6} {c['fail']:>6}")
    L.append("-" * 74)
    L.append("  HARD GATES                                              FAILURES")
    L.append("-" * 74)
    for gid, name, desc in G.GATES:
        n = m["per_gate"][gid]
        tag = "PASS" if n == 0 else "FAIL"
        who = ""
        if n:
            sample = m["per_gate_scenarios"][gid][:3]
            who = "  e.g. " + ", ".join(sample)
        L.append(f"  [{tag}] {gid:<4} {name:<24} {desc[:26]:<26} {n:>4}{who}")
    if m["other"]:
        L.append("-" * 74)
        L.append(f"  [FAIL] ERR  uncategorised errors                            "
                 f"{len(m['other']):>4}")
        for line in m["other"][:5]:
            L.append(f"         {line[:66]}")
    L.append("-" * 74)
    L.append("  SOFT CHECKS (reported, never fatal)")
    L.append("-" * 74)
    for sid, name, desc in G.SOFT_CHECKS:
        L.append(f"  {sid:<4} {name:<24} {desc[:30]:<30} {m['soft'][sid]:>4}")
    if m["fixture_conflicts"]:
        L.append("-" * 74)
        L.append(f"  NOTE: {len(m['fixture_conflicts'])} fixture(s) ask for silence "
                 f"while marked requested;")
        L.append(f"        gate 6 outranks gate 5, so delivery wins: "
                 f"{', '.join(m['fixture_conflicts'][:4])}")
    L.append("-" * 74)
    L.append(f"  scenarios passing all gates : {m['n_pass']}/{m['n_scenarios']}")
    L.append(f"  batches passing G11         : "
             f"{m['n_batches'] - m['n_batch_fail']}/{m['n_batches']}")
    L.append(f"  longest delivered message   : {m['max_words']} words")
    L.append("-" * 74)
    L.append(f"  RESULT: {'ALL GATES PASS' if m['all_pass'] else 'GATE FAILURE'}")
    L.append("=" * 74)
    return L


def write_json(path, mode, corpus_note, render_note, m, records, batch_results):
    payload = {
        "contract_version": R.CONTRACT_VERSION,
        "mode": mode,
        "path": "reference",
        "corpus": corpus_note,
        "compiler": render_note,
        "summary": {k: m[k] for k in
                    ("n_scenarios", "n_pass", "n_fail", "n_batches",
                     "n_batch_fail", "silent", "delivered", "max_words",
                     "all_pass")},
        "per_gate": m["per_gate"],
        "soft": m["soft"],
        "by_category": m["by_category"],
        "fixture_conflicts": m["fixture_conflicts"],
        "other_errors": m["other"],
        "scenarios": [
            {"id": r["id"], "category": r["category"],
             "response_type": r["response_type"], "decision": r["decision"],
             "requested": r["requested"], "silent": r["silent"],
             "words": r["words"], "failures": r["failures"], "soft": r["soft"],
             "rendered": r["rendered"]}
            for r in records
        ],
        "batches": [{"ids": b["ids"], "words": b["words"],
                     "failures": b["failures"], "rendered": b["rendered"]}
                    for b in batch_results],
    }
    pathlib.Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                  encoding="utf-8")


def write_md(path, mode, corpus_note, render_note, cstats, m, records, batch_results):
    L = []
    L.append("# Hermix Response Quality Report\n")
    L.append(f"- **Overall:** {'ALL GATES PASS' if m['all_pass'] else 'GATE FAILURE'}")
    L.append(f"- **Mode:** `{mode}` — {m['n_scenarios']} scenarios, "
             f"{m['n_batches']} batches")
    L.append(f"- **Packet contract:** `{R.CONTRACT_VERSION}`")
    L.append(f"- **Corpus:** {corpus_note}")
    L.append(f"- **Compiler:** {render_note}")
    L.append("- **Path:** reference — packets are built deterministically from each "
             "fixture's `expected` block, not by the judge. G3/G8/G10 are therefore "
             "well-formed by construction and near-vacuous here; the compiler gates "
             "(G1, G2, G4, G7, G9, G11) and the delivery-decision gates (G5, G6) are "
             "fully live. A green run says nothing about judge quality.")
    L.append("- **Determinism:** no clock, no network, no model. Re-running "
             "reproduces this report byte for byte.\n")

    L.append("## Hard gates\n")
    L.append("| Gate | Name | Guarantee | Failures | Result |")
    L.append("| --- | --- | --- | ---: | --- |")
    for gid, name, desc in G.GATES:
        n = m["per_gate"][gid]
        L.append(f"| {gid} | `{name}` | {desc} | {n} | "
                 f"{'PASS' if n == 0 else 'FAIL'} |")
    if m["other"]:
        L.append(f"| ERR | `uncategorised` | Harness or compiler exceptions | "
                 f"{len(m['other'])} | FAIL |")
    L.append("")

    L.append("## By category\n")
    L.append("| Category | N | Pass | Fail |")
    L.append("| --- | ---: | ---: | ---: |")
    for cat in sorted(m["by_category"]):
        c = m["by_category"][cat]
        L.append(f"| `{cat}` | {c['n']} | {c['pass']} | {c['fail']} |")
    L.append("")

    L.append("## Soft checks (reported, never fatal)\n")
    L.append("| Check | Name | Occurrences |")
    L.append("| --- | --- | ---: |")
    for sid, name, _ in G.SOFT_CHECKS:
        L.append(f"| {sid} | `{name}` | {m['soft'][sid]} |")
    L.append("")

    failed = [r for r in records if r["failures"]]
    L.append(f"## Failing scenarios ({len(failed)})\n")
    if not failed:
        L.append("None.\n")
    else:
        for r in failed[:40]:
            L.append(f"### `{r['id']}` — {r['category']} / {r['response_type']} "
                     f"(decision `{r['decision']}`)\n")
            for f in r["failures"]:
                L.append(f"- {f}")
            L.append("")
            L.append("```")
            L.append(r["rendered"] or "(nothing delivered)")
            L.append("```\n")
        if len(failed) > 40:
            L.append(f"_...and {len(failed) - 40} more (see the JSON artifact)._\n")

    bad_batches = [b for b in batch_results if b["failures"]]
    L.append(f"## Failing batches ({len(bad_batches)}/{len(batch_results)})\n")
    if not bad_batches:
        L.append("None.\n")
    else:
        for b in bad_batches[:10]:
            L.append(f"### `{' + '.join(b['ids'])}`\n")
            for f in b["failures"]:
                L.append(f"- {f}")
            L.append("")

    L.append("## What this harness cannot tell you\n")
    L.append("- **G4 is lexical.** A claim is located in the prose by shared content "
             "words. A compiler that paraphrases a claim into entirely different "
             "vocabulary escapes the attribution check; a compiler that drops the "
             "claim escapes it too, and is caught only by GC1. The two gates work as "
             "a pair or not at all.")
    L.append("- **G2 detects shapes, not secrets.** Emails, URLs, social handles and "
             "unambiguous phone formats, plus whatever contact values the fixture "
             "declares. A contact value written in an unusual form is invisible to it. "
             "A loose digit-run pattern was tried and removed: it fired on `$1200-1500 "
             "per month`, and a gate with false positives gets switched off.")
    L.append("- **The judge is not evaluated.** See the path note above.")
    L.append("- **Batches are synthetic.** Findings are grouped three at a time in "
             "corpus order, which is the contract ceiling; real batching is the "
             "matchmaker's decision and is tested in `tests/test_matchmaker.py`.\n")
    pathlib.Path(path).write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Selftest — prove the gates still bite
# --------------------------------------------------------------------------- #
def _selftest_scenario(decision="notify", requested=False):
    return {
        "id": f"selftest_{decision}",
        "category": "strong_professional",
        "response_type": "finding",
        "user_card": {"handle": "you", "offer": "timing analysis"},
        "ring1": ["You want a hardware partner for the March pilot."],
        "ring0_forbidden": ["Telefonica"],
        "counterpart_card": {"handle": "mira-herald", "display": "Mira",
                             "offer": "sensor tooling"},
        "transcript": [
            {"from": "us", "text": "Do you have room for a small pilot?"},
            {"from": "them", "text": "She is free in March for a small pilot."},
        ],
        "standing_intent": None,
        "system_state": {"requested": requested, "redelivery": False,
                         "quiet_hours": False, "recent_interruptions": 0,
                         "replied": True},
        "expected": {
            "decision": decision,
            "required_claims": ["free in March"],
            "forbidden_claims": ["already agreed"],
            "required_uncertainties": [],
            "allowed_actions": ["ask_budget", "dismiss"],
            "forbidden_terms": [],
            "max_words": 90,
        },
    }


def _selftest_packet(scenario):
    return R.packet(
        "finding",
        finding_id="selftest",
        counterpart={"handle": "mira-herald", "display": "Mira"},
        user_relevance={"summary": "You want a hardware partner for the March pilot.",
                        "source_ids": ["ring1:0"]},
        claims=[R.claim("She is free in March for a small pilot.",
                        "counterpart_claim", ["turn:2"])],
        uncertainties=["Budget was not discussed."],
        next_actions=[R.action("ask_budget"), R.action("dismiss")],
        delivery={"requested": bool((scenario.get("system_state") or {}).get("requested"))},
    )


CLEAN_RENDER = (
    "I heard back about the March pilot. Her agent said she is free in March "
    "for a small pilot, though budget was not discussed. "
    "Want me to ask about budget and timing?"
)


def run_selftest():
    """Plant three faults; require the harness to catch each one.

    Each case is run twice — clean and faulted — because "the gate fired" only
    means something if the same gate stays quiet on correct output. A gate that
    fails everything catches every planted fault and is still worthless.

    Needs neither the corpus nor the compiler, so it runs today.
    """
    ok_scen = _selftest_scenario()
    ok_pkt = _selftest_packet(ok_scen)
    sources = G.source_map_for(ok_scen)

    silent_scen = _selftest_scenario(decision="drop")
    silent_pkt = _selftest_packet(silent_scen)

    cases = [
        {
            "name": "(a) Ring-0 sentinel in the rendered text",
            "gate": "G1",
            "scenario": ok_scen, "packet": ok_pkt, "sources": sources,
            "clean": CLEAN_RENDER,
            "faulted": CLEAN_RENDER + " It lines up with your Telefonica work.",
        },
        {
            "name": "(b) counterpart_claim rendered as a flat assertion",
            "gate": "G4",
            "scenario": ok_scen, "packet": ok_pkt, "sources": sources,
            "clean": CLEAN_RENDER,
            "faulted": ("I heard back about the March pilot. She is free in March "
                        "for a small pilot, though budget was not discussed. "
                        "Want me to ask about budget and timing?"),
        },
        {
            "name": "(c) must-silence scenario producing output",
            "gate": "G5",
            "scenario": silent_scen, "packet": silent_pkt,
            "sources": G.source_map_for(silent_scen),
            "clean": G.SILENT,
            "faulted": CLEAN_RENDER,
        },
    ]

    print("=" * 74)
    print("  HERMIX RESPONSE HARNESS SELFTEST")
    print("=" * 74)
    print("  Each fault is injected into otherwise-correct output. The harness")
    print("  must flag the faulted version on the named gate AND leave the clean")
    print("  version alone. Both halves are required.")
    print("-" * 74)

    all_ok = True
    for case in cases:
        clean_fail = G.run_all(case["packet"], case["clean"], case["scenario"],
                               case["sources"])
        faulted_fail = G.run_all(case["packet"], case["faulted"], case["scenario"],
                                 case["sources"])
        clean_gate = [f for f in clean_fail if G.gate_of(f) == case["gate"]]
        caught = [f for f in faulted_fail if G.gate_of(f) == case["gate"]]

        control_ok = not clean_gate
        caught_ok = bool(caught)
        passed = control_ok and caught_ok
        all_ok = all_ok and passed

        print(f"  [{'PASS' if passed else 'FAIL'}] {case['name']}")
        print(f"         expected gate      : {case['gate']} "
              f"{G.GATE_NAME[case['gate']]}")
        print(f"         clean output       : "
              f"{'quiet (correct)' if control_ok else 'FIRED - gate is trigger-happy'}")
        print(f"         faulted output     : "
              f"{'CAUGHT' if caught_ok else 'MISSED - fault slipped through'}")
        if caught:
            print(f"         failure            : {caught[0][:110]}")
        if clean_gate:
            print(f"         false positive     : {clean_gate[0][:110]}")
        other_clean = [f for f in clean_fail if G.gate_of(f) != case["gate"]]
        if other_clean:
            print(f"         note: clean output also tripped "
                  f"{sorted({G.gate_of(f) for f in other_clean})}")
        print("-" * 74)

    # A fourth, unnumbered check: the SILENT literal must still match the
    # delivery path's marker, or G5 and G6 are testing a string nobody uses.
    try:
        from hermix import matchmaker
        drift = matchmaker.SILENT != G.SILENT
        print(f"  [{'FAIL' if drift else 'PASS'}] SILENT marker matches "
              f"matchmaker.SILENT ({matchmaker.SILENT!r})")
        all_ok = all_ok and not drift
    except Exception as exc:
        print(f"  [WARN] could not import matchmaker to check the SILENT marker "
              f"({exc!r})")
    print("-" * 74)
    print(f"  RESULT: {'ALL 3 PLANTED FAULTS CAUGHT' if all_ok else 'SELFTEST FAILURE'}")
    print("=" * 74)
    return 0 if all_ok else 1


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Response-quality gates for Hermix.")
    ap.add_argument("--fast", action="store_true",
                    help="deterministic 1-in-3 subset per category (default)")
    ap.add_argument("--full", action="store_true", help="every scenario")
    ap.add_argument("--json", dest="json_path", default=str(DEFAULT_JSON),
                    help=f"JSON artifact path (default {DEFAULT_JSON.name})")
    ap.add_argument("--md", dest="md_path", default=str(DEFAULT_MD),
                    help=f"markdown report path (default {DEFAULT_MD.name})")
    ap.add_argument("--selftest", action="store_true",
                    help="inject three faults and require the harness to catch them")
    args = ap.parse_args(argv)

    if args.selftest:
        return run_selftest()

    fast = not args.full
    mode = "full" if args.full else "fast"

    corpus, corpus_err = load_corpus()
    render_mod, render_err = load_render()
    if corpus is None or render_mod is None:
        print("=" * 74)
        print("  HERMIX RESPONSE QUALITY - NOT YET AVAILABLE")
        print("=" * 74)
        if corpus is None:
            print(f"  corpus   : {corpus_err}")
        else:
            print(f"  corpus   : OK ({len(corpus.scenarios())} scenarios)")
        if render_mod is None:
            print(f"  compiler : {render_err}")
        else:
            print(f"  compiler : OK ({getattr(render_mod, 'COMPILER_VERSION', '?')})")
        print("-" * 74)
        print("  Both are being written in this sprint. The gates themselves are")
        print("  ready and provably working - run:")
        print("      python evals/run_response_eval.py --selftest")
        print("=" * 74)
        return 0

    corpus_note = (f"evals/response_corpus.py ({len(corpus.scenarios())} scenarios)")
    render_note = (f"hermix/render.py "
                   f"({getattr(render_mod, 'COMPILER_VERSION', 'no version')})")
    cstats = corpus.stats() if hasattr(corpus, "stats") else None

    chosen = select(corpus.scenarios(), fast=fast)
    records = []
    for scenario in chosen:
        rec = evaluate_scenario(scenario, render_mod)
        rec["scenario"] = scenario
        records.append(rec)

    batches = build_batches(records, limit=MAX_BATCHES_FAST if fast else None)
    batch_results = [evaluate_batch(b, render_mod) for b in batches]

    m = tally(records, batch_results)
    print("\n".join(scorecard_lines(mode, corpus_note, render_note, cstats, m,
                                    records, batch_results)))

    write_json(args.json_path, mode, corpus_note, render_note, m, records,
               batch_results)
    write_md(args.md_path, mode, corpus_note, render_note, cstats, m, records,
             batch_results)
    print(f"\n  json written   : {args.json_path}")
    print(f"  report written : {args.md_path}")
    return 0 if m["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
