"""`/hermies` slash-command handlers and the skill-install approval gate."""
import datetime
import json

from . import profile, service, sanitize, matchmaker


def _ts(epoch) -> str:
    try:
        return datetime.datetime.utcfromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def make_handler(client, card, llm):
    """Return a handler for `/hermies <sub> [args]`."""

    def handler(args: str = "", **kwargs) -> str:
        parts = (args or "").strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1] if len(parts) > 1 else ""

        if sub in ("", "status"):
            pub = card.public_dict()
            if card.is_empty():
                return ("No public profile yet. Set one with:\n"
                        "  /hermies profile {\"handle\": \"gus-herald\", "
                        "\"represents\": \"a creative technologist in AI film\", "
                        "\"offer\": [\"ai video\"], \"need\": [\"collaborators\"]}")
            return "Your PUBLIC card:\n" + json.dumps(pub, indent=2)

        if sub == "profile":
            if not rest:
                return json.dumps(card.public_dict(), indent=2)
            try:
                patch = json.loads(rest)
            except json.JSONDecodeError as e:
                return f"Could not parse JSON: {e}"
            for k, v in patch.items():
                if k in profile.PUBLIC_FIELDS:
                    setattr(card, k, v)
            profile.save_card(card)
            client.publish_profile(card.public_dict())
            return "Updated & published your PUBLIC card:\n" + json.dumps(card.public_dict(), indent=2)

        if sub == "discover":
            signals = client.discover(card.public_dict())
            if not signals:
                return "No matches yet. Flesh out your `need`/`offer`/`guilds` and try again."
            return service._format_digest(signals)

        if sub == "signals":
            handle = card.public_dict().get("handle", "")
            signals = client.list_signals(handle)
            return service._format_digest(signals) if signals else "No signals right now."

        if sub == "search":
            agents = client.search_agents(rest)
            if not agents:
                return "No agents found."
            # Untrusted network content: render sanitized values only.
            return "\n".join(
                f"  • @{sanitize.clean_text(a.get('handle', ''))} — "
                f"{sanitize.clean_text(a.get('represents', ''))}"
                for a in agents)

        if sub == "skills":
            skills = client.browse_skills(rest)
            # Untrusted network content: render sanitized values only.
            return "Available skills:\n" + "\n".join(
                f"  • {sanitize.clean_text(s.get('name', ''))} — "
                f"{sanitize.clean_text(s.get('description', ''))}"
                for s in skills)

        if sub == "matches":
            return _matches_view(matchmaker.load_state())

        if sub == "log":
            return _log_view(matchmaker.load_state())

        if sub == "card":
            state = matchmaker.load_state()
            if rest.strip().lower() == "apply":
                return _card_apply(state, card, client)
            return _card_view(state, card)

        return (f"Unknown subcommand '{sub}'. Try: status | profile | discover | "
                "signals | search <q> | skills | matches | log | card | card apply")

    return handler


def _matches_view(state) -> str:
    """Pending notifications the budget hasn't delivered yet, plus recent
    verdicts. All candidate content already passed sanitize on the way in."""
    lines = []
    queue = state.get("queue") or []
    if queue:
        lines.append("Pending (queued for the next quiet slot):")
        for it in queue:
            lines.append(f"  • @{it.get('handle', '')} — {it.get('pitch', '')}")
    else:
        lines.append("Nothing queued right now.")
    seen = state.get("seen") or {}
    if seen:
        recent = sorted(seen.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:8]
        lines.append("")
        lines.append("Recent verdicts:")
        for handle, rec in recent:
            lines.append(f"  • @{handle}: {rec.get('verdict', '?')} "
                         f"({_ts(rec.get('ts', 0))})")
    return "\n".join(lines)


def _log_view(state) -> str:
    entries = (state.get("log") or [])[-20:]
    if not entries:
        return "No matchmaker activity yet."
    lines = ["Last matchmaker decisions:"]
    for e in entries:
        note = e.get("note", "")
        note = f" — {note}" if note else ""
        lines.append(f"  {_ts(e.get('ts', 0))}  @{e.get('handle', '')}  "
                     f"[{e.get('verdict', '')}]{note}")
    return "\n".join(lines)


def _card_view(state, card) -> str:
    proposal = state.get("card_proposal")
    current = card.public_dict()
    if not proposal or not proposal.get("proposed"):
        return ("Current PUBLIC card:\n" + json.dumps(current, indent=2) +
                "\n\n(No refreshed proposal pending.)")
    proposed = proposal["proposed"]
    lines = [f"Proposed card refresh (from {_ts(proposal.get('ts', 0))}). "
             "The matchmaker only sharpens wording of what you already have — "
             "review and run `/hermies card apply` to accept.", ""]
    for k in profile.PUBLIC_FIELDS:
        if k not in proposed:
            continue
        cur, new = current.get(k), proposed.get(k)
        if cur != new:
            lines.append(f"  {k}:")
            lines.append(f"    - now: {json.dumps(cur, ensure_ascii=False)}")
            lines.append(f"    + new: {json.dumps(new, ensure_ascii=False)}")
    if len(lines) == 2:
        lines.append("  (No differences from your current card.)")
    return "\n".join(lines)


def _card_apply(state, card, client) -> str:
    proposal = state.get("card_proposal")
    if not proposal or not proposal.get("proposed"):
        return "Nothing to apply — no proposed card refresh pending."
    proposed = proposal["proposed"]
    applied = []
    for k, v in proposed.items():
        if k in profile.PUBLIC_FIELDS:
            setattr(card, k, v)
            applied.append(k)
    profile.save_card(card)
    client.publish_profile(card.public_dict())
    state["card_proposal"] = None
    matchmaker.save_state(state)
    return ("Applied & republished the refreshed card (fields: "
            + ", ".join(applied) + "):\n" + json.dumps(card.public_dict(), indent=2))


def install_gate(**kwargs):
    """pre_tool_call hook: never let a skill be installed from the network
    without explicit human approval.

    Return contract verified against Hermes source
    (hermes_cli/plugins.py::_get_pre_tool_call_directive_details, see
    docs/HERMES-API-GROUND-TRUTH.md §4): the hook is invoked kwargs-only with
    ``tool_name`` and the argument dict ``args``. To BLOCK a call, return
    ``{"action": "block", "message": "<reason>"}`` (the message becomes the
    tool result the model sees); return ``None`` to allow. There is no
    ``allow``/``reason`` shape.

    We DENY hermies_install_skill unless the call carries ``approved=True``
    (which the /hermies UI / an inject_message prompt would set after the human
    says yes).
    """
    tool = kwargs.get("tool_name")
    args = kwargs.get("args") or {}
    if tool == "hermies_install_skill" and not args.get("approved"):
        return {
            "action": "block",
            "message": "Installing a network skill requires human approval. "
                       "Confirm in chat first, then retry with approved=true.",
        }
    return None  # allow everything else
