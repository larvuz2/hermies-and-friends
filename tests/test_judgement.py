"""Structured judgement: what survives when the model misbehaves.

Every test here is a way a model can produce something plausible and wrong. The
question is never "did it parse" but "did anything unsupported reach a human".
The fail direction is always toward NOT interrupting.
"""
import json

import pytest

from hermix import judgement as J, response as R


SOURCES = R.source_map(ring1=["a"], turns=4, system_facts=("no_reply",))


def _raw(**kw):
    base = {
        "verdict": "notify",
        "user_relevance": {"text": "her tooling covers your timing work",
                           "source_ids": ["ring1:0"]},
        "claims": [{"text": "she is open to a small collaboration",
                    "evidence_state": "counterpart_claim",
                    "source_ids": ["turn:4"]}],
        "uncertainties": ["budget was not discussed"],
        "next_action_ids": ["ask_budget"],
        "reason": "concrete mutual benefit, they replied",
    }
    base.update(kw)
    return json.dumps(base)


# --------------------------------------------------------------------------- #
# The happy path still works
# --------------------------------------------------------------------------- #
def test_a_well_formed_judgement_survives_intact():
    out = J.parse(_raw(), SOURCES)
    assert out["verdict"] == "notify"
    assert len(out["claims"]) == 1
    assert out["claims"][0]["source_ids"] == ["turn:4"]
    assert out["dropped"] == []


def test_json_wrapped_in_markdown_fences_is_handled():
    out = J.parse("```json\n" + _raw() + "\n```", SOURCES)
    assert out["verdict"] == "notify"


def test_the_packet_it_builds_validates():
    out = J.parse(_raw(), SOURCES)
    p = J.to_packet(out, counterpart={"handle": "mira-herald", "display": "Mira"})
    assert R.validate(p, known_sources=SOURCES) == []


# --------------------------------------------------------------------------- #
# Fabricated citations — the likeliest shape of a made-up claim
# --------------------------------------------------------------------------- #
def test_a_claim_citing_a_turn_that_never_happened_is_dropped():
    out = J.parse(_raw(claims=[{"text": "she quoted four thousand euros",
                                "evidence_state": "counterpart_claim",
                                "source_ids": ["turn:9"]}]), SOURCES)
    assert out["claims"] == []
    assert any("non-existent source" in why for _, why in out["dropped"])


def test_dropping_the_only_claim_downgrades_notify_to_watch():
    """Interrupting on the strength of evidence we just discarded is precisely
    the failure this module exists to prevent."""
    out = J.parse(_raw(claims=[{"text": "x", "evidence_state": "counterpart_claim",
                                "source_ids": ["turn:99"]}]), SOURCES)
    assert out["verdict"] == "watch"


def test_a_claim_with_no_source_is_dropped():
    out = J.parse(_raw(claims=[{"text": "she is definitely free",
                                "evidence_state": "counterpart_claim",
                                "source_ids": []}]), SOURCES)
    assert out["claims"] == []
    assert out["verdict"] == "watch"


def test_a_partially_valid_citation_list_drops_the_whole_claim():
    """Keeping the good half would leave a claim that reads as better sourced
    than it is."""
    out = J.parse(_raw(claims=[{"text": "she is open",
                                "evidence_state": "counterpart_claim",
                                "source_ids": ["turn:2", "turn:77"]}]), SOURCES)
    assert out["claims"] == []


def test_ring0_is_not_a_usable_source():
    out = J.parse(_raw(claims=[{"text": "he is short of money",
                                "evidence_state": "counterpart_claim",
                                "source_ids": ["ring0:1"]}]), SOURCES)
    assert out["claims"] == []


# --------------------------------------------------------------------------- #
# Claiming capabilities we do not have
# --------------------------------------------------------------------------- #
def test_independently_verified_is_refused():
    """We never verify anything against a third source. A model asserting we
    did is claiming a capability the product does not have."""
    out = J.parse(_raw(claims=[{"text": "her credentials check out",
                                "evidence_state": "independently_verified",
                                "source_ids": ["turn:2"]}]), SOURCES)
    assert out["claims"] == []
    assert any("evidence_state" in why for _, why in out["dropped"])


def test_an_invented_next_action_is_dropped():
    out = J.parse(_raw(next_action_ids=["book_a_flight"]), SOURCES)
    assert out["next_action_ids"] == []
    assert out["verdict"] == "watch", "notify with no real action must downgrade"


def test_an_unknown_evidence_state_is_dropped():
    out = J.parse(_raw(claims=[{"text": "x", "evidence_state": "obviously_true",
                                "source_ids": ["turn:1"]}]), SOURCES)
    assert out["claims"] == []


# --------------------------------------------------------------------------- #
# Malformed output
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["", "not json at all", "{", None, 42,
                                 '{"verdict": "notify"}'])
def test_garbage_never_produces_notify(raw):
    assert J.parse(raw, SOURCES)["verdict"] in ("watch", "drop")


def test_an_off_menu_verdict_becomes_watch():
    assert J.parse(_raw(verdict="SHOUT"), SOURCES)["verdict"] == "watch"


def test_notify_without_grounded_relevance_downgrades():
    out = J.parse(_raw(user_relevance={"text": "seems good", "source_ids": []}),
                  SOURCES)
    assert out["verdict"] == "watch"


def test_drop_is_never_upgraded():
    assert J.parse(_raw(verdict="drop"), SOURCES)["verdict"] == "drop"


# --------------------------------------------------------------------------- #
# What was discarded is recorded, not hidden
# --------------------------------------------------------------------------- #
def test_dropped_records_what_was_discarded_and_why():
    """Otherwise "the model behaved" and "we silently deleted its output" look
    identical in telemetry."""
    out = J.parse(_raw(claims=[
        {"text": "invented", "evidence_state": "counterpart_claim",
         "source_ids": ["turn:88"]},
        {"text": "she is open", "evidence_state": "counterpart_claim",
         "source_ids": ["turn:3"]}]), SOURCES)
    assert len(out["claims"]) == 1
    assert len(out["dropped"]) == 1
    assert "invented" in out["dropped"][0][0]


def test_the_prompt_version_travels_with_the_result():
    assert J.parse(_raw(), SOURCES)["prompt_version"] == J.JUDGE_PROMPT_VERSION


# --------------------------------------------------------------------------- #
# Transcript numbering — claims can only cite what exists
# --------------------------------------------------------------------------- #
def test_transcript_numbering_is_one_based_and_labels_each_side():
    out = J.number_transcript([
        {"from": "us", "text": "hello"},
        {"from": "them", "text": "hi there"},
    ])
    assert out.splitlines()[0].startswith("[1] US:")
    assert out.splitlines()[1].startswith("[2] THEM:")


def test_numbering_lines_up_with_the_source_map():
    lines = [{"from": "us", "text": "a"}, {"from": "them", "text": "b"},
             {"from": "us", "text": "c"}]
    numbered = J.number_transcript(lines)
    sources = R.source_map(turns=len(lines))
    for n in range(1, len(lines) + 1):
        assert f"[{n}]" in numbered
        assert f"turn:{n}" in sources
    assert "turn:4" not in sources


def test_untrusted_transcript_text_is_cleaned():
    out = J.number_transcript([{"from": "them", "text": "x" * 5000}])
    assert len(out) < 1000


def test_the_judge_prompt_forbids_writing_copy():
    """The whole design rests on the judge NOT being asked for persuasion."""
    assert "Do NOT write marketing copy" in J.JUDGE_SYSTEM
    assert "auditable" in J.JUDGE_SYSTEM
    assert "Never invent a turn number" in J.JUDGE_SYSTEM


def test_the_findings_prompt_requires_turn_citations():
    assert "[turn:N]" in J.FINDINGS_SYSTEM
    assert "is NOT" in J.FINDINGS_SYSTEM      # "They said X" is NOT "X is true"
