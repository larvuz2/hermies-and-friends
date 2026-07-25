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
import pathlib

log = logging.getLogger("hermies")

# The five behavioral skills that steer the agent's Hermies behavior. They are
# opt-in explicit loads (register_skill does NOT add them to <available_skills>);
# the agent resolves them as hermies:<name> when a situation calls for one.
_BEHAVIORAL_SKILLS = [
    ("hermies-context",
     "World model for the Hermies network — entities, rings, public vs private."),
    ("hermies-voice",
     "How to talk to your human about Hermies — tone, framing, hard bans."),
    ("hermies-onboarding",
     "The one-time onboarding ritual: build the dossier, draft/publish the card."),
    ("hermies-envoy-protocol",
     "Rules for talking to other agents — digs, asks, reveals, rings, defense."),
    ("hermies-delivery",
     "When to speak vs stay quiet — the worth-it bar, intents, follow-ups."),
]


def register(ctx):
    import time
    from . import profile, tools, commands, service, matchmaker, dossier, _config
    from .client import HermiesClient, make_transport

    card = profile.load_card()
    client = HermiesClient(make_transport())

    def llm(system: str, user: str) -> str:
        """Constrained LLM call for the envoy. Adapter over ctx.llm — the envoy
        never receives anything but the card-derived system prompt.

        Real API (agent/plugin_llm.py::PluginLlm.complete): synchronous,
        takes OpenAI-shaped ``messages`` and returns a PluginLlmCompleteResult
        whose text is on ``.text``. See docs/HERMES-API-GROUND-TRUTH.md §5."""
        result = ctx.llm.complete(messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        return result.text

    def inject(content: str, role: str = "user"):
        try:
            ctx.inject_message(content, role=role)
        except Exception as e:  # gateway mode / not available — degrade quietly
            log.debug("inject_message unavailable: %s", e)

    # --- commands ---
    ctx.register_command(
        "hermies",
        commands.make_handler(client, card, llm),
        "Manage your Hermies profile, discover agents, see matches & decisions",
    )

    # --- tools (private agent works the network agentically) ---
    for spec in tools.build(client, card, llm):
        ctx.register_tool(
            name=spec["name"],
            toolset="hermies",
            schema=spec["schema"],
            handler=spec["handler"],
            description=spec["description"],
        )

    # --- approval gate for skill installs AND contact reveals ---
    ctx.register_hook("pre_tool_call", commands.install_gate)

    # --- behavioral skills (opt-in explicit loads) ---
    if hasattr(ctx, "register_skill"):
        skill_root = pathlib.Path(__file__).resolve().parent / "skills"
        for sname, sdesc in _BEHAVIORAL_SKILLS:
            try:
                ctx.register_skill(sname, skill_root / sname / "SKILL.md", sdesc)
            except Exception as e:  # a packaging hiccup must not kill the plugin
                log.debug("register_skill(%s) skipped: %s", sname, e)

    # --- first-run bootstrap: point the agent at onboarding, once ---
    # inject_message is the safest surface (no-op in gateway mode, never raises —
    # see docs/HERMES-API-GROUND-TRUTH.md §6). We say it ONCE, only while the
    # dossier does not exist / is not onboarded; the onboarding skill drives the
    # actual consented setup with the human.
    try:
        if not dossier.is_onboarded():
            inject(
                "Hermies is installed but not set up yet. Next time you talk "
                "with your human, run the hermies:hermies-onboarding skill "
                "together to build their private dossier and public card before "
                "doing anything else on the network.",
                role="user",
            )
    except Exception as e:
        log.debug("onboarding bootstrap skipped: %s", e)

    # --- the notification path: prefer the blessed cron scheduler ---
    # The cron job calls hermies_matchmake a few times a day and relays the
    # result only when it is not the silent marker. If cron is unavailable
    # (older Hermes / tests), fall back to running matchmake inside the daemon
    # loop and surfacing results via /hermies matches (degraded mode).
    cron_ok = matchmaker.ensure_cron()
    fallback = None
    if not cron_ok:
        def fallback():
            return matchmaker.run_and_persist(client, card, llm, time.time)

    # --- background envoy + signals loop (frequent, silent) ---
    service.start(client, card, inject, llm, matchmake=fallback,
                  match_interval=_config.match_every_hours() * 3600)

    mode = "live" if _live() else "offline/mock"
    notify = "cron" if cron_ok else "daemon-fallback"
    log.info("hermies registered (%s, notify=%s) for handle=%s",
             mode, notify, card.public_dict().get("handle") or "<unset>")


def _live() -> bool:
    from . import _config
    return _config.is_live()
