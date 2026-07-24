"""`/hermies` slash-command handlers and the skill-install approval gate."""
import json

from . import profile, service, sanitize


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

        return (f"Unknown subcommand '{sub}'. Try: status | profile | discover | "
                "signals | search <q> | skills")

    return handler


def install_gate(**kwargs):
    """pre_tool_call hook: never let a skill be installed from the network
    without explicit human approval.

    NOTE: confirm the exact pre_tool_call return contract against Hermes source
    before shipping. Here we DENY hermies_install_skill unless the call carries
    an `approved=True` param (which the /hermies UI / an inject_message prompt
    would set after the human says yes).
    """
    tool = kwargs.get("tool_name") or kwargs.get("name")
    params = kwargs.get("params") or kwargs.get("arguments") or {}
    if tool == "hermies_install_skill" and not params.get("approved"):
        return {
            "allow": False,
            "reason": "Installing a network skill requires human approval. "
                      "Confirm in chat first, then retry with approved=true.",
        }
    return None  # allow everything else
