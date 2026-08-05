#!/usr/bin/env python3
"""Post-deploy gate: is the hub running the product we advertise?

The hub deliberately stays UP when the real embedding model cannot load, and
falls back to hashing n-grams. That is the right availability choice — a network
that answers badly beats a network that is dark — but it is a materially
different product:

    real (fastembed)   recall@10 0.90   cross-vocabulary 6/8
    fallback (hashing) recall@10 0.77   cross-vocabulary 2/8

A deploy script that only checks HTTP 200 cannot tell these apart, so a silent
quality failure ships and nobody finds out until users report that the network
"doesn't really find anything". This asserts both:

  1. /healthz says engine=fastembed  (the model actually loaded)
  2. a live semantic query connects two cards that share NO words

Check 2 matters on its own: the model can load and still be misconfigured, or
indexed against an empty corpus. Only an end-to-end query proves the thing the
README promises.

Usage:
    python3 smoke.py [BASE_URL]          # default http://127.0.0.1:8787

Exit codes:  0 pass · 1 fail (deploy should abort) · 2 could not run the check
Stdlib only, so it runs on a fresh VPS with no pip install.
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
TIMEOUT = 20

# Deliberately zero shared words. A hashing n-gram embedder cannot connect
# these; a real semantic model does. That gap IS the product.
CARD_A = {
    "handle": "",
    "represents": "a game studio building three-dimensional environments",
    "offer": ["three-dimensional environment construction", "real-time scenery"],
    "need": ["a composer for interactive scores"],
}
CARD_B = {
    "handle": "",
    "represents": "a freelance 3d worlds artist",
    "offer": ["3d worlds", "level art"],
    "need": ["studios hiring for scenery work"],
}

PASS, FAIL, ERROR = 0, 1, 2
_cleanup = []          # (handle, key) to withdraw before exit


def _req(path, payload=None, key=None):
    url = f"{BASE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode() or "{}")


def _say(ok, msg):
    print(f"  {'PASS' if ok else 'FAIL'}  {msg}")
    return ok


def _withdraw():
    for handle, key in _cleanup:
        try:
            _req("/v1/profile/remove", {}, key)
        except Exception as e:                                  # noqa: BLE001
            print(f"  warn: could not withdraw {handle}: {e}", file=sys.stderr)


def wait_for_hub(attempts=30, delay=2):
    """A freshly restarted hub builds its index before it serves."""
    for i in range(attempts):
        try:
            return _req("/healthz")
        except Exception:                                       # noqa: BLE001
            if i == 0:
                print(f"  waiting for {BASE} ...")
            time.sleep(delay)
    return None


def main():
    print(f"==> Hermix hub smoke check: {BASE}")

    health = wait_for_hub()
    if health is None:
        print(f"  ERROR  hub never became reachable at {BASE}", file=sys.stderr)
        return ERROR

    mode = health.get("engine", "?")
    ok_mode = _say(mode == "fastembed",
                   f"embedding engine = {mode} (model={health.get('model', '?')})")
    if not ok_mode:
        print("\n  The hub is UP but running fallback embeddings. Cross-vocabulary\n"
              "  matching will not work and recall drops ~0.13. Usually the model\n"
              "  was never downloaded on this box. Fix:\n"
              "     sudo -u hermix /opt/hermix/venv/bin/python -c \\\n"
              "       \"from fastembed import TextEmbedding; TextEmbedding()\"\n"
              "     systemctl restart hermix\n"
              "  Then re-run this check.", file=sys.stderr)
        return FAIL

    # --- live semantic canary ---------------------------------------------
    stamp = str(int(time.time()))
    try:
        for card, tag in ((CARD_A, "a"), (CARD_B, "b")):
            handle = f"smoke-{tag}-{stamp}"
            reg = _req("/v1/register",
                       {"handle": handle, "represents": card["represents"]})
            key = reg["api_key"]
            _cleanup.append((handle, key))
            card = dict(card, handle=handle)
            _req("/v1/profile", {"card": card}, key)
            card["_key"], card["_handle"] = key, handle

        a_handle, a_key = _cleanup[0]
        b_handle = _cleanup[1][0]
        card_a = dict(CARD_A, handle=a_handle)

        signals = _req("/v1/discover", {"card": card_a}, a_key).get("signals") or []
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        print(f"  ERROR  canary setup failed: HTTP {e.code} {body}", file=sys.stderr)
        _withdraw()
        return ERROR
    except Exception as e:                                      # noqa: BLE001
        print(f"  ERROR  canary setup failed: {e}", file=sys.stderr)
        _withdraw()
        return ERROR

    found = [s.get("agent") for s in signals]
    ok_canary = _say(
        b_handle in found,
        f"semantic canary: 'three-dimensional environments' -> '3d worlds' "
        f"({'connected' if b_handle in found else 'NOT connected'})")

    _withdraw()

    if not ok_canary:
        print(f"\n  The model loaded but the query did not connect two cards that\n"
              f"  share no vocabulary. Returned: {found or '[]'}\n"
              f"  Check the index built over existing cards and that the match\n"
              f"  floor is not set too high.", file=sys.stderr)
        return FAIL

    print(f"  PASS  hub is serving the advertised product "
          f"({health.get('indexed_cards', '?')} cards indexed)")
    return PASS


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _withdraw()
        sys.exit(ERROR)
