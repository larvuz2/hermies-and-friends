"""Background service: the two directions of the membrane.

  OUTWARD (network -> envoy): poll the mailbox for inbound queries, answer each
  as the PUBLIC envoy (card-only), and post the reply back to the hub.

  INWARD (network -> human): poll for signals/matches and inject a short digest
  into the human's chat via ctx.inject_message.

`run_once` is pure and synchronous for tests. `start` runs it on a daemon
thread with simple polling (v1 — swap for SSE/websocket later).
"""
import threading
import time

from . import envoy, sanitize


def run_once(client, card, inject, llm) -> dict:
    """One poll cycle. Returns a summary dict (handy for tests/logging)."""
    handled, signals = 0, []

    # OUTWARD: answer inbound network queries as the envoy (card-only context).
    handle = card.public_dict().get("handle") or ""
    for msg in client.list_inbound(handle):
        reply = envoy.respond(card, msg.get("query", ""), llm)
        client.post_reply(msg["id"], reply)
        handled += 1

    # INWARD: pull signals and surface a digest to the human.
    signals = client.list_signals(handle)
    if signals:
        inject(_format_digest(signals), "user")

    return {"handled": handled, "signals": len(signals)}


def _format_digest(signals) -> str:
    # Sanitize EVERY signal before any of its strings reach the human's chat.
    # Network content is untrusted: strip control/zero-width chars, collapse
    # line breaks, drop unknown keys, cap length.
    top = [sanitize.clean_signal(s) for s in (signals or [])[:5]]
    lines = ["🕊️  Hermies signals for you:"]
    for s in top:
        if s.get("kind") == "match":
            lines.append(f"  • match: @{s.get('agent', '')} — {s.get('why', '')}")
        else:
            lines.append(f"  • {s.get('kind') or 'signal'}: {s.get('why', '')}")
    lines.append("Reply `/hermies discover` to explore, or ask me to reach out.")
    return "\n".join(lines)


def start(client, card, inject, llm, interval: int = 90, matchmake=None,
          match_interval: int = None):
    """Spawn the daemon poll loop. No-op-safe: exceptions are swallowed so a
    backend hiccup never takes down the host agent.

    FREQUENT + SILENT work (drain inbound, envoy auto-replies) runs every
    ``interval`` seconds. ``inject_message`` is a no-op in gateway mode, so this
    loop is NOT the notification path — that is the cron job (see matchmaker).

    ``matchmake`` is only supplied in the DEGRADED fallback (no cron available):
    a callable ``() -> str`` run every ``match_interval`` seconds; a non-SILENT
    result is best-effort injected (works in CLI, silently dropped in gateway,
    where the human reads it via /hermies matches instead)."""
    from . import matchmaker
    if match_interval is None:
        match_interval = 4 * 3600
    state = {"last_match": 0.0}

    def _loop():
        while True:
            try:
                run_once(client, card, inject, llm)
            except Exception:
                pass
            if matchmake is not None:
                now = time.time()
                if now - state["last_match"] >= match_interval:
                    state["last_match"] = now
                    try:
                        result = matchmake()
                        if result and result != matchmaker.SILENT:
                            inject(result, "user")
                    except Exception:
                        pass
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="hermies-service", daemon=True)
    t.start()
    return t
