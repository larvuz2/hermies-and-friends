"""Telemetry must be able to spot a quality regression without ever carrying
the material the membrane exists to protect.

The failure mode being guarded is the reasonable-sounding one: "log the message
too, so we can see what went wrong". A delivered message quotes the counterpart
and describes the human's own situation — shipping it centrally would put on the
hub exactly what the product promises never gets there.
"""
import pytest

from hermix import render, response as R, telemetry


def _packet():
    return R.packet(
        "finding",
        counterpart={"handle": "mira-herald", "display": "Mira",
                     "represents": "works on AI music videos"},
        user_relevance={"summary": "her tooling covers your timing work",
                        "source_ids": ["ring1:0"]},
        claims=[R.claim("she is open to a collaboration", "counterpart_claim",
                        ["turn:4"]),
                R.claim("her workflow accepts your format",
                        "conversation_established", ["turn:2"])],
        uncertainties=["budget"],
        next_actions=[R.action("ask_budget"), R.action("dismiss")],
    )


def test_an_event_records_shape_and_outcome():
    e = telemetry.record(_packet(), rendered=render.render(_packet()))
    assert e["response_type"] == "finding"
    assert e["claim_count"] == 2
    assert e["uncertainty_count"] == 1
    assert e["word_count"] > 0
    assert e["evidence_states"] == {"counterpart_claim": 1,
                                    "conversation_established": 1}
    assert e["action_ids"] == ["ask_budget", "dismiss"]


def test_no_delivered_prose_is_stored():
    """The whole message is used to COUNT words and then discarded."""
    prose = render.render(_packet())
    e = telemetry.record(_packet(), rendered=prose)
    blob = repr(e)
    for fragment in ("Mira", "timing work", "collaboration", "music videos"):
        assert fragment not in blob, f"{fragment!r} reached telemetry"


def test_no_counterpart_identity_is_stored():
    e = telemetry.record(_packet(), rendered="x")
    assert "mira-herald" not in repr(e)
    assert "counterpart" not in e


def test_contact_details_can_never_ride_along():
    p = R.packet("reveal_request",
                 counterpart={"handle": "mira-herald"},
                 contact={"name": "Alex Chen", "email": "alex@example.com"},
                 next_actions=[R.action("approve_reveal")])
    e = telemetry.record(p, rendered=render.render(p))
    assert "alex@example.com" not in repr(e)
    assert "Alex Chen" not in repr(e)


def test_unknown_fields_are_dropped_rather_than_passed_through():
    e = telemetry.record(_packet(), rendered="x")
    assert set(e) <= set(telemetry.ALLOWED_FIELDS)


def test_versions_travel_so_a_regression_can_be_attributed():
    """Otherwise "quality dropped last week" is unattributable to a change."""
    e = telemetry.record(_packet(), rendered="x", prompt_version="2.0.0")
    assert e["compiler_version"] == render.COMPILER_VERSION
    assert e["contract_version"] == R.CONTRACT_VERSION
    assert e["prompt_version"] == "2.0.0"


def test_the_safety_check_catches_free_text_in_an_allowed_field():
    """A future caller could put prose into a permitted key; the allowlist
    alone would not notice."""
    e = telemetry.record(_packet(), rendered="x")
    e["feedback"] = "she said her rate is four thousand"
    assert telemetry.is_safe(e), "free text in feedback was not caught"


def test_a_clean_event_is_reported_safe():
    assert telemetry.is_safe(telemetry.record(_packet(), rendered="x")) == []


@pytest.mark.parametrize("category", ["useful", "wrong_fit", "too_early",
                                      "spam", None])
def test_feedback_categories_are_accepted(category):
    e = telemetry.record(_packet(), rendered="x", feedback=category)
    assert telemetry.is_safe(e) == []


def test_a_forbidden_key_is_reported_even_if_someone_adds_it():
    e = telemetry.record(_packet(), rendered="x")
    e["transcript"] = "..."
    assert any("transcript" in p for p in telemetry.is_safe(e))


def test_judge_drops_are_counted_so_silent_deletion_is_visible():
    """"The model behaved" and "we threw half its output away" must not look
    identical in the data."""
    e = telemetry.record(_packet(), rendered="x", judge_dropped=3)
    assert e["judge_dropped"] == 3
