"""Response-quality telemetry: shape and outcome only, never content.

The question this has to answer during the beta is "are the messages any good,
and is that getting better or worse?" — which needs almost none of what a
conventional analytics pipeline collects.

What we record is deliberately boring: response type, whether the human asked
for it, how long it was, which compiler and prompt versions produced it, and
what the human did next. That is enough to see a regression the day it lands
(word counts drift up, feedback goes negative after a compiler bump, requested
answers stop arriving) without any transcript, dossier line, contact detail or
delivered sentence leaving the machine.

The temptation to log "just the prose, so we can see what went wrong" is exactly
the thing to refuse. A delivered message quotes the counterpart and describes
the human's own situation; shipping it centrally would put on the hub precisely
the material the membrane exists to keep off it. When a message is wrong the
human can send it to us themselves, with their consent and their knowledge.

``record()`` returns the event rather than transmitting it. Whoever flushes it
decides, and there is a test that the event carries no free text.
"""
from . import render, response as R

SCHEMA_VERSION = "1.0.0"

# The only keys allowed to leave the machine. Anything not on this list is
# dropped by _strip(), so adding a field is a deliberate act rather than
# something that happens by accident when a caller passes extra kwargs.
ALLOWED_FIELDS = (
    "schema_version",
    "response_type",       # finding | ask_result | checkin | reveal_* | ...
    "requested",           # did the human ask for this?
    "redelivery",          # a retry of something that never landed
    "batch_size",          # how many findings rode along
    "word_count",          # length, for drift
    "claim_count",
    "uncertainty_count",
    "evidence_states",     # counts per state, e.g. {"counterpart_claim": 2}
    "action_ids",          # which capabilities were offered
    "delivered",           # did it reach the human
    "acknowledged",        # did the human see it
    "feedback",            # useful | wrong_fit | too_early | spam | None
    "intro_requested",
    "dismissed",
    "seconds_to_response",  # delivered -> human reacted
    "judge_dropped",       # claims the judge produced that we refused
    "contract_version",
    "compiler_version",
    "prompt_version",
)

# Never recorded, at any level. Present as an explicit list because a reviewer
# should be able to check the refusal rather than infer it from absence.
NEVER_RECORDED = (
    "transcript", "prose", "message", "text", "note", "findings_note",
    "dossier", "ring0", "ring1", "contact", "email", "handle", "counterpart",
    "question", "answer", "pitch", "evidence", "reason",
)


def _strip(event):
    return {k: v for k, v in event.items() if k in ALLOWED_FIELDS}


def record(packet, *, rendered="", delivered=True, acknowledged=False,
           feedback=None, intro_requested=False, dismissed=False,
           seconds_to_response=None, judge_dropped=0, batch_size=1,
           prompt_version=""):
    """One privacy-safe event describing a delivered response.

    ``rendered`` is used ONLY to count words; the text itself is never stored.
    """
    claims = (packet or {}).get("claims") or []
    states = {}
    for c in claims:
        s = c.get("evidence_state")
        if s:
            states[s] = states.get(s, 0) + 1

    delivery = (packet or {}).get("delivery") or {}
    event = {
        "schema_version": SCHEMA_VERSION,
        "response_type": (packet or {}).get("response_type"),
        "requested": bool(delivery.get("requested")),
        "redelivery": bool(delivery.get("redelivery")),
        "batch_size": int(batch_size),
        "word_count": R.word_count(rendered),
        "claim_count": len(claims),
        "uncertainty_count": len((packet or {}).get("uncertainties") or []),
        "evidence_states": states,
        "action_ids": sorted({a.get("id") for a in
                              ((packet or {}).get("next_actions") or [])
                              if a.get("id")}),
        "delivered": bool(delivered),
        "acknowledged": bool(acknowledged),
        "feedback": feedback,
        "intro_requested": bool(intro_requested),
        "dismissed": bool(dismissed),
        "seconds_to_response": seconds_to_response,
        "judge_dropped": int(judge_dropped),
        "contract_version": (packet or {}).get("contract_version",
                                               R.CONTRACT_VERSION),
        "compiler_version": render.COMPILER_VERSION,
        "prompt_version": prompt_version,
    }
    return _strip(event)


def is_safe(event):
    """Problems that would make this event unsafe to transmit. Empty = safe.

    Belt and braces on top of _strip: a future caller could put free text into
    an allowed field (a 'feedback' of "she said her rate is 4k"), and the
    allowlist alone would not catch it.
    """
    problems = []
    for key in event or {}:
        if key not in ALLOWED_FIELDS:
            problems.append(f"{key!r} is not an allowed telemetry field")
        if key in NEVER_RECORDED:
            problems.append(f"{key!r} is explicitly never recorded")
    for key, value in (event or {}).items():
        if isinstance(value, str) and key not in ("schema_version",
                                                  "response_type", "feedback",
                                                  "contract_version",
                                                  "compiler_version",
                                                  "prompt_version"):
            problems.append(f"{key!r} carries free text: {value[:40]!r}")
        if key == "feedback" and value not in (None, "useful", "wrong_fit",
                                               "too_early", "spam"):
            problems.append(f"feedback must be a category, got {value!r}")
    return problems
