"""Hermies sidecar — the network engine as its OWN process.

WHY
---
Everything volatile lives here: matchmaking, envoy conversations, polling,
state, updates. Restarting THIS costs nothing — it is our process. Restarting
the user's Hermes gateway is invasive: it takes down their Telegram, their
other plugins and any task mid-flight. So the split is:

    Hermes gateway ── thin bridge plugin (commands, tools, hooks, consent)
                            │  shared state on disk
                            ▼
    hermies-sidecar ── matchmaker · envoy · polling · outbox · updates

A matchmaker or envoy fix then needs only a SIDECAR restart, which nobody
notices. Only a change to the bridge itself (tools/commands/hooks) needs the
gateway to restart — and those change rarely.

This is not a rewrite: it runs the same service loop the plugin has always run.
It works standalone because network inference already goes through the hub's
operator-paid proxy (HERMIES_LLM=hub), so no ctx.llm — and therefore no Hermes
process — is required.

Run:  python -m hermies.sidecar      (or the systemd unit from install.sh)
"""
import logging
import os
import sys
import time

log = logging.getLogger("hermies.sidecar")


def _build():
    """Assemble exactly what the daemon needs — no Hermes context required."""
    from . import _config, profile, matchmaker, remote_config
    from .client import HermiesClient, make_transport, ensure_registered

    card = profile.load_card()
    client = HermiesClient(make_transport())

    # The sidecar has no ctx.llm, so the network's thinking MUST come from the
    # hub's operator-paid proxy. Force it rather than silently going mute.
    os.environ.setdefault("HERMIES_LLM", "hub")

    def llm(system: str, user: str, *, purpose: str = "envoy") -> str:
        try:
            res = client.llm_complete(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}], purpose)
            if isinstance(res, dict):
                return res.get("text", "") or ""
            return getattr(res, "text", "") or ""
        except Exception as exc:
            log.debug("hub inference unavailable (%s)", exc)
            return ""          # callers already fail toward silence

    def inject(content, role="user"):
        # The sidecar cannot talk to the human — by design. Findings go to the
        # durable outbox and the bridge/cron delivers them.
        return False

    try:
        if _config.has_hub() and client.healthz():
            if card.public_dict().get("handle") and not _config.api_key():
                ensure_registered(client, card)
            remote_config.refresh(client, force=True)
    except Exception as exc:
        log.warning("startup hub check failed: %s", exc)

    return client, card, llm, inject


def main(argv=None) -> int:
    logging.basicConfig(
        level=os.environ.get("HERMIES_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from . import _config, matchmaker, service

    client, card, llm, inject = _build()

    def engine():
        return matchmaker.run_engine_and_persist(client, card, llm, time.time)

    log.info("hermies sidecar starting: hub=%s handle=%s",
             _config.service_url() or "-",
             card.public_dict().get("handle") or "<unset>")

    # The same loop the plugin has always run — it just lives out here now.
    # inject_works=False: the sidecar never speaks to the human.
    service.start(client, card, inject, llm, engine=engine,
                  match_interval=_config.match_every_hours() * 3600,
                  inject_works=False, role="sidecar")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("hermies sidecar stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
