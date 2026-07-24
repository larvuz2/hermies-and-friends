"""Hermies and Friends — Hermes plugin entry point.

The plugin is the "embassy": it connects the local Hermes agent to the Hermies
agent-to-agent network while keeping the private agent behind the envoy
membrane (see envoy.py). Everything is wired from register(ctx).

NOTE: the exact ctx.* signatures below follow the documented Hermes plugin API.
Confirm register_command / register_hook / inject_message / ctx.llm signatures
against your Hermes version and adjust the thin adapters if they differ — the
plugin's own logic lives in the sibling modules and is API-independent.
"""
import logging

log = logging.getLogger("hermies")


def register(ctx):
    from . import profile, tools, commands, service
    from .client import HermiesClient, make_transport

    card = profile.load_card()
    client = HermiesClient(make_transport())

    def llm(system: str, user: str) -> str:
        """Constrained LLM call for the envoy. Adapter over ctx.llm — the envoy
        never receives anything but the card-derived system prompt."""
        try:
            return ctx.llm.complete(system=system, user=user)
        except TypeError:
            return ctx.llm.complete(f"{system}\n\n{user}")

    def inject(content: str, role: str = "user"):
        try:
            ctx.inject_message(content, role=role)
        except Exception as e:  # gateway mode / not available — degrade quietly
            log.debug("inject_message unavailable: %s", e)

    # --- commands ---
    ctx.register_command(
        "hermies",
        commands.make_handler(client, card, llm),
        "Manage your Hermies public profile, discover agents, and see signals",
    )

    # --- tools (private agent works the network agentically) ---
    for spec in tools.build(client, card):
        ctx.register_tool(
            name=spec["name"],
            toolset="hermies",
            schema=spec["schema"],
            handler=spec["handler"],
            description=spec["description"],
        )

    # --- approval gate for network skill installs ---
    ctx.register_hook("pre_tool_call", commands.install_gate)

    # --- background envoy + signals loop ---
    service.start(client, card, inject, llm)

    mode = "live" if _live() else "offline/mock"
    log.info("hermies registered (%s) for handle=%s", mode, card.public_dict().get("handle") or "<unset>")


def _live() -> bool:
    from . import _config
    return _config.is_live()
