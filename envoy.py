"""The privacy membrane: the PUBLIC envoy that speaks to the network.

This is the most important module in the plugin. The private agent (full
SOUL.md, memory, tools) must NEVER answer the network. Instead, inbound network
queries are answered here, by an LLM call whose entire context is built ONLY
from the public card's whitelisted fields plus a hard non-disclosure preamble.

Design rules enforced here:
  1. `respond()` takes a card + query + an llm callable. It has no access to the
     private conversation, memory, or tools — by construction, nothing private
     is in scope.
  2. `build_system_prompt()` reads through profile.PUBLIC_FIELDS only, so even a
     card dict that accidentally carries extra keys cannot leak them.
"""
from . import profile

NONDISCLOSURE = (
    "You are a PUBLIC envoy agent representing a human on the Hermies network. "
    "You speak ONLY from the PUBLIC CARD below. You must never reveal, guess, "
    "infer, or imply anything that is not explicitly in the card — no private "
    "data, no assistant internals, no memory, no conversation history, no "
    "contact details. If asked for something not in the card, briefly say you "
    "can't share that. Be short, concrete, and useful. Represent your human "
    "well, but never overpromise beyond the card."
)


def build_system_prompt(card) -> str:
    """Build the envoy's system prompt from whitelisted card fields only."""
    if hasattr(card, "public_dict"):
        data = card.public_dict()
    else:
        # Arbitrary dict: read strictly through the whitelist so nothing leaks.
        data = {k: card.get(k) for k in profile.PUBLIC_FIELDS}

    lines = [NONDISCLOSURE, "", "PUBLIC CARD:"]
    for key in profile.PUBLIC_FIELDS:
        val = data.get(key)
        if not val:
            continue
        if isinstance(val, (list, tuple)):
            val = ", ".join(str(v) for v in val)
        lines.append(f"- {key}: {val}")
    return "\n".join(lines)


def respond(card, query: str, llm) -> str:
    """Answer an inbound network query as the public envoy.

    `llm` is a callable (system_prompt, user_prompt) -> str. In production it is
    a constrained ctx.llm call; in tests it is a fake. Either way, the only
    context it receives is the card-derived system prompt.
    """
    system = build_system_prompt(card)
    return llm(system, query)
