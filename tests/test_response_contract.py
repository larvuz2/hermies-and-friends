"""The response contract, which is the gate every delivered sentence passes.

The property being pinned throughout: a claim cannot exist without a source
that actually exists. Everything else in the response sprint rests on that —
if a model can invent `turn:9` for a four-turn conversation and have it
accepted, the audit trail is decorative.
"""
import pytest

from hermix import response as R


def _sources(turns=4, ring1=2):
    return R.source_map(ring1=list(range(ring1)), turns=turns,
                        system_facts=("no_reply",))


def _finding(**kw):
    base = dict(
        response_type="finding",
        finding_id="a1b2c3",
        counterpart={"handle": "mira-herald", "display": "Mira"},
        user_relevance={"summary": "Her tooling covers your timing work.",
                        "source_ids": ["ring1:0"]},
        claims=[R.claim("She is open to a small collaboration.",
                        "counterpart_claim", ["turn:4"])],
        uncertainties=["Budget was not discussed."],
        next_actions=[R.action("ask_budget"), R.action("dismiss")],
    )
    base.update(kw)
    return R.packet(**base)


# --------------------------------------------------------------------------- #
# The core rule
# --------------------------------------------------------------------------- #
def test_a_well_formed_finding_validates():
    assert R.validate(_finding(), known_sources=_sources()) == []


def test_a_claim_without_a_source_is_rejected():
    p = _finding(claims=[R.claim("She is interested.", "counterpart_claim", [])])
    problems = R.validate(p, known_sources=_sources())
    assert any("no source" in x for x in problems), problems


def test_only_a_system_fact_may_be_unsourced():
    """We computed it, so there is nothing to cite."""
    p = _finding(claims=[R.claim("Their agent never replied.", "system_fact", [])])
    assert R.validate(p, known_sources=_sources()) == []


def test_a_claim_citing_a_turn_that_does_not_exist_is_rejected():
    """The likeliest shape of a fabricated claim: plausible text, invented
    citation. Without known_sources this would sail through."""
    p = _finding(claims=[R.claim("She quoted 4k.", "counterpart_claim", ["turn:9"])])
    problems = R.validate(p, known_sources=_sources(turns=4))
    assert any("does not exist" in x for x in problems), problems


def test_a_malformed_source_is_rejected():
    p = _finding(claims=[R.claim("x", "counterpart_claim", ["vibes"])])
    assert any("malformed" in x for x in R.validate(p, known_sources=_sources()))


def test_ring0_can_never_be_a_delivered_source():
    """Ring 0 may inform judgement locally; it may never be the stated reason."""
    p = _finding(claims=[R.claim("He is behind on rent.", "counterpart_claim",
                                 ["ring0:2"])])
    problems = R.validate(p, known_sources=_sources() | {"ring0:2"})
    assert any("Ring 0" in x or "malformed" in x for x in problems), problems


def test_an_unknown_evidence_state_is_rejected():
    p = _finding(claims=[R.claim("x", "definitely_true", ["turn:1"])])
    assert any("evidence_state" in x for x in R.validate(p, known_sources=_sources()))


# --------------------------------------------------------------------------- #
# Privacy sentinels
# --------------------------------------------------------------------------- #
def test_a_private_value_anywhere_in_the_packet_fails():
    p = _finding(user_relevance={"summary": "Because you owe Telefonica 40k.",
                                 "source_ids": ["ring1:0"]})
    problems = R.validate(p, known_sources=_sources(),
                          forbidden_strings=["Telefonica"])
    assert any("leaked" in x for x in problems), problems


def test_sentinel_scanning_reaches_nested_fields():
    p = _finding(claims=[R.claim("Nadia can help.", "counterpart_claim", ["turn:2"])])
    problems = R.validate(p, known_sources=_sources(), forbidden_strings=["Nadia"])
    assert any("leaked" in x for x in problems), problems


def test_contact_details_cannot_ride_on_an_ordinary_finding():
    p = _finding(contact={"email": "alex@example.com"})
    problems = R.validate(p, known_sources=_sources())
    assert any("contact" in x for x in problems), problems


def test_contact_is_legal_on_a_reveal():
    p = R.packet("reveal_request",
                 counterpart={"handle": "mira-herald"},
                 contact={"name": "Alex Chen", "email": "alex@example.com"},
                 next_actions=[R.action("approve_reveal"), R.action("decline_reveal")])
    assert R.validate(p, known_sources=_sources()) == []


def test_a_reveal_preview_may_never_imply_it_already_happened():
    p = R.packet("reveal_request",
                 contact={"email": "alex@example.com"},
                 system={"released": True},
                 next_actions=[R.action("approve_reveal")])
    assert any("never imply" in x for x in R.validate(p, known_sources=_sources()))


# --------------------------------------------------------------------------- #
# Actions must be real capabilities
# --------------------------------------------------------------------------- #
def test_an_invented_action_is_rejected():
    p = _finding(next_actions=[R.action("book_a_meeting")])
    assert any("not a real capability" in x
               for x in R.validate(p, known_sources=_sources()))


def test_a_finding_with_no_available_action_is_rejected():
    p = _finding(next_actions=[R.action("ask_budget", available=False)])
    assert any("no available next action" in x
               for x in R.validate(p, known_sources=_sources()))


# --------------------------------------------------------------------------- #
# Attribution policy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state,hedge", [
    ("profile_only", True),
    ("counterpart_claim", True),
    ("unknown", True),
    ("conversation_established", False),
    ("independently_verified", False),
    ("system_fact", False),
])
def test_which_states_must_be_attributed(state, hedge):
    """A counterpart's assertion is never stated as fact; something the two
    agents worked out together can be."""
    assert R.needs_attribution(state) is hedge


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "Your score was 8.4", "the hub said", "opened a thread_id",
    "the envoy replied", "a good match for you", "ran the pipeline",
])
def test_machinery_vocabulary_is_detected(text):
    assert R.banned_terms_in(text), text


@pytest.mark.parametrize("text", [
    "She is open to a small collaboration this month.",
    "I could not get an answer, so I would rather not guess.",
    "Her workflow accepts the same export format you use.",
    "A watchmaker in Lisbon who restores marine clocks.",
    "The hubbub about their launch has died down.",
])
def test_ordinary_english_is_not_flagged(text):
    """Substring matching would fire on watchmaker/hubbub — word boundaries matter."""
    assert R.banned_terms_in(text) == [], text


# --------------------------------------------------------------------------- #
# Fail-closed behaviour
# --------------------------------------------------------------------------- #
def test_ensure_valid_raises_rather_than_returning_something_sendable():
    with pytest.raises(R.PacketError):
        R.ensure_valid(_finding(claims=[R.claim("x", "counterpart_claim", [])]),
                       known_sources=_sources())


def test_source_map_describes_exactly_what_exists():
    ids = R.source_map(ring1=["a", "b"], turns=3, system_facts=("no_reply",))
    assert ids == {"card:ours", "card:theirs", "ring1:0", "ring1:1",
                   "turn:1", "turn:2", "turn:3", "system:no_reply"}


def test_a_packet_records_its_contract_version():
    """Telemetry has to be able to attribute a regression to a version."""
    assert _finding()["contract_version"] == R.CONTRACT_VERSION
