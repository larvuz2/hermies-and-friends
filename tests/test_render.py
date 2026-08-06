"""The deterministic compiler: does packet -> prose keep every promise?

The properties worth pinning are the ones a fluent writer would erode:
attribution, uncertainty, and a close the product can actually act on. A model
asked for good copy drops all three; a function cannot.
"""
import pytest

from hermix import render, response as R


def _p(**kw):
    base = dict(
        response_type="finding",
        finding_id="a1b2c3",
        counterpart={"handle": "mira-herald", "display": "Mira",
                     "represents": "works on AI music videos"},
        user_relevance={"summary": "her beat-sync tooling covers the manual "
                                   "timing work slowing down your project",
                        "source_ids": ["ring1:0"]},
        claims=[R.claim("she is open to a small collaboration this month",
                        "counterpart_claim", ["turn:4"])],
        uncertainties=["budget or timing"],
        next_actions=[R.action("ask_budget"), R.action("dismiss")],
    )
    base.update(kw)
    return R.packet(**base)


# --------------------------------------------------------------------------- #
# Attribution — the property most easily lost
# --------------------------------------------------------------------------- #
def test_a_counterpart_claim_is_never_stated_as_fact():
    out = render.render(_p())
    assert "Mira's agent said she is open" in out
    assert "She is open to a small collaboration this month." not in out


def test_a_profile_only_claim_says_so():
    out = render.render(_p(claims=[
        R.claim("she offers beat-sync tooling", "profile_only", ["card:theirs"])]))
    assert "Mira's profile says" in out


def test_something_established_together_is_stated_plainly():
    """Attribution everywhere would be exhausting and would flatten the very
    distinction that makes it useful."""
    out = render.render(_p(claims=[
        R.claim("her workflow accepts the same export format you use",
                "conversation_established", ["turn:2", "turn:3"])]))
    assert "Her workflow accepts the same export format you use." in out
    assert "agent said her workflow" not in out


def test_a_system_fact_needs_no_attribution():
    out = render.render(_p(claims=[
        R.claim("their agent never replied", "system_fact", [])]))
    assert "Their agent never replied." in out


def test_an_unknown_is_rendered_as_not_established():
    out = render.render(_p(claims=[
        R.claim("she has worked at this scale before", "unknown", ["turn:5"])]))
    assert "could not establish" in out


# --------------------------------------------------------------------------- #
# Uncertainty and the close
# --------------------------------------------------------------------------- #
def test_uncertainty_survives_into_the_prose():
    assert "did not get into budget or timing" in render.render(_p())


def test_the_message_ends_with_one_real_question():
    out = render.render(_p())
    assert out.rstrip().endswith("?")
    assert out.count("?") == 1


def test_the_close_offers_a_graceful_way_out():
    assert "or leave it?" in render.render(_p())


def test_no_generic_non_action_close():
    out = render.render(_p()).lower()
    for filler in ("let me know what you think", "would you like more "
                   "information", "could be worth exploring"):
        assert filler not in out


def test_an_offer_of_only_dismiss_does_not_pretend_to_be_a_question():
    out = render.render(_p(next_actions=[R.action("dismiss")]))
    assert "leave it there" in out.lower()


# --------------------------------------------------------------------------- #
# Voice
# --------------------------------------------------------------------------- #
def test_it_speaks_as_the_users_own_agent_not_as_the_product():
    out = render.render(_p())
    assert out.startswith("I found someone worth your time.")
    assert "Hermix" not in out


def test_no_machinery_vocabulary_reaches_the_user():
    assert render.check_output(render.render(_p())) == []


def test_no_raw_score_reaches_the_user():
    assert render.check_output("She rated 8.4/10 for you") != []


def test_a_finding_stays_inside_its_length_budget():
    n = R.word_count(render.render(_p()))
    lo, hi = R.WORD_TARGETS["finding"]
    assert lo <= n <= hi, f"{n} words"


# --------------------------------------------------------------------------- #
# Requested answers are answers, not pitches
# --------------------------------------------------------------------------- #
def _ask(**kw):
    base = dict(
        response_type="ask_result",
        counterpart={"handle": "mira-herald", "display": "Mira"},
        claims=[R.claim("it does, but only through their desktop export path",
                        "counterpart_claim", ["turn:2"])],
        uncertainties=["They have not tested files longer than ten minutes."],
        next_actions=[R.action("ask_followup"), R.action("dismiss")],
        system={"question": "Mira's workflow supports ProRes with alpha"},
        delivery={"requested": True},
    )
    base.update(kw)
    return R.packet(**base)


def test_an_answer_reminds_the_user_what_they_asked():
    assert "You asked whether" in render.render(_ask())


def test_an_answer_carries_no_sales_framing():
    out = render.render(_ask())
    assert "worth your time" not in out
    assert "I found someone" not in out


def test_a_no_answer_is_delivered_honestly_rather_than_dressed_up():
    """This is a SUCCESSFUL response. Silence here would be the failure."""
    out = render.render(_ask(claims=[], uncertainties=[]))
    assert "could not get an answer" in out
    assert "rather not guess" in out


def test_a_negative_answer_still_delivers():
    out = render.render(_ask(claims=[
        R.claim("it does not support that format at all", "counterpart_claim",
                ["turn:2"])]))
    assert "does not support" in out


# --------------------------------------------------------------------------- #
# Reveal: preview and execution must be unmistakable
# --------------------------------------------------------------------------- #
def _reveal(**kw):
    base = dict(
        response_type="reveal_request",
        counterpart={"handle": "mira-herald", "display": "Mira"},
        contact={"name": "Alex Chen", "email": "alex@example.com",
                 "handle": "@alexcreates"},
        next_actions=[R.action("approve_reveal"), R.action("decline_reveal")],
    )
    base.update(kw)
    return R.packet(**base)


def test_a_reveal_preview_lists_exactly_what_would_be_shared():
    out = render.render(_reveal())
    for value in ("Alex Chen", "alex@example.com", "@alexcreates"):
        assert value in out


def test_a_reveal_preview_says_plainly_that_nothing_moved():
    assert "Nothing has been shared yet." in render.render(_reveal())


def test_one_sided_approval_never_implies_the_humans_are_connected():
    out = render.render(R.packet(
        "reveal_outcome", counterpart={"handle": "mira-herald", "display": "Mira"},
        system={"released": False}))
    assert "not released them yet" in out
    assert "still has to approve" in out


def test_both_sided_approval_says_so():
    out = render.render(R.packet(
        "reveal_outcome", counterpart={"handle": "mira-herald", "display": "Mira"},
        system={"released": True}))
    assert "Both sides approved" in out


# --------------------------------------------------------------------------- #
# Safety wording
# --------------------------------------------------------------------------- #
def test_block_states_the_effect_and_that_they_are_not_told():
    out = render.render(R.packet("safety", system={"kind": "block",
                                                   "handle": "mira-herald"}))
    assert "not told" in out and "unblock" in out


def test_report_is_not_confused_with_block():
    out = render.render(R.packet("safety", system={"kind": "report",
                                                   "handle": "mira-herald"}))
    assert "does not block" in out


def test_an_injection_attempt_reassures_that_nothing_leaked():
    out = render.render(R.packet("safety", system={"kind": "injection"}))
    assert "Nothing of yours was shared." in out


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def test_budget_exhaustion_promises_not_to_spend_the_users_money():
    out = render.render(R.packet("error", system={"kind": "budget"}))
    assert "rather than spend yours" in out
    assert "Nothing for you to do." in out


def test_error_copy_stays_free_of_machinery_words():
    for kind in ("budget", "offline", "paused", "no_card", "degraded", "timeout"):
        out = render.render(R.packet("error", system={"kind": kind}))
        assert R.banned_terms_in(out) == [], (kind, out)


# --------------------------------------------------------------------------- #
# Batching
# --------------------------------------------------------------------------- #
def test_a_batch_is_one_message_with_one_close():
    packets = [_p(counterpart={"handle": f"a{i}-herald", "display": f"A{i}",
                               "represents": "does useful work"}) for i in range(3)]
    out = render.render_batch(packets)
    assert out.count("?") == 1, "a batch must ask once, not once per item"
    assert "I found three things worth bringing you:" in out
    for i in (1, 2, 3):
        assert f"{i}. A" in out


def test_a_batch_never_repeats_a_feedback_menu():
    packets = [_p() for _ in range(3)]
    out = render.render_batch(packets)
    assert "wrong fit" not in out and "too early" not in out


def test_answers_come_before_findings_in_a_batch():
    """The human is actively waiting on an answer; a finding can wait a line."""
    out = render.render_batch([_p(), _ask()])
    assert out.index("You asked whether") < out.index("worth bringing you")


def test_a_single_packet_batch_renders_as_a_single_message():
    assert render.render_batch([_p()]) == render.render(_p())


def test_an_empty_batch_renders_nothing():
    assert render.render_batch([]) == ""


def test_a_batch_stays_inside_its_length_budget():
    out = render.render_batch([_p(), _p(), _p()])
    lo, hi = R.WORD_TARGETS["finding_batch"]
    assert R.word_count(out) <= hi


# --------------------------------------------------------------------------- #
# Fail closed
# --------------------------------------------------------------------------- #
def test_an_invalid_packet_raises_rather_than_rendering_something():
    bad = _p(claims=[R.claim("she is definitely free", "counterpart_claim", [])])
    with pytest.raises(R.PacketError):
        render.render(bad)


def test_a_batch_refuses_if_any_packet_is_invalid():
    with pytest.raises(R.PacketError):
        render.render_batch([_p(), _p(claims=[
            R.claim("x", "counterpart_claim", [])])])


def test_rendering_is_deterministic():
    assert render.render(_p()) == render.render(_p())


# --------------------------------------------------------------------------- #
# Overselling — the most damaging single sentence this product could send
# --------------------------------------------------------------------------- #
def test_a_card_nobody_replied_to_is_never_called_someone_worth_your_time():
    """Converting a guess into a recommendation costs the user real time, and
    they only discover the difference after spending it."""
    out = render.render(_p(
        user_relevance={"summary": "she offers beat-sync tooling",
                        "source_ids": ["card:theirs"]},
        claims=[R.claim("their agent never replied, so fit is unconfirmed",
                        "system_fact", [])],
        uncertainties=[],
        next_actions=[R.action("retry_later"), R.action("dismiss")]))
    assert "worth your time" not in out
    assert "have not confirmed anything" in out


def test_a_profile_only_claim_also_does_not_earn_the_strong_opening():
    out = render.render(_p(claims=[
        R.claim("she offers beat-sync tooling", "profile_only", ["card:theirs"])]))
    assert "worth your time" not in out


def test_a_real_reply_does_earn_it():
    assert "I found someone worth your time." in render.render(_p())


def test_an_established_fact_earns_it_too():
    out = render.render(_p(claims=[
        R.claim("her workflow accepts your export format",
                "conversation_established", ["turn:2"])]))
    assert "I found someone worth your time." in out


def test_a_question_keeps_the_users_own_capitalisation():
    out = render.render(_ask(system={"question": "Mira's workflow supports ProRes"}))
    assert "whether Mira's workflow" in out, "a proper noun was lower-cased"
