"""ACCEPTANCE E2E — a REAL dig, end to end, through the live hub.

Where ``two_agents.py`` proves the one-shot envoy handshake, this proves the
whole matchmaking conversation the plugin now runs for you:

    register + publish two agents (reciprocal match)
      -> side A's MATCHMAKER opens a kind="dig" thread and sends an opener
      -> both envoys converse ~3 turns each THROUGH THE REAL HUB
         (A = initiator via matchmaker.run_cycle; B = answerer via
          service.drain_threads — the daemon's envoy)
      -> both sides conclude with a FINDINGS NOTE
      -> A's judge fires on the findings note -> a human notification
         (must name the counterpart AND quote the conversation)
      -> A sends a reveal_request (its human approved sharing A's contact)
      -> B's daemon QUEUES it as a pending reveal — never auto-answers it
      -> B's human approves via hermies_reveal_respond(human_approved=true)
      -> A receives B's contact.

The core trust assertion: B's contact identity appears ONLY after B's explicit
approval, and never anywhere in the transcript before it.

Run:  python e2e/three_way_dig.py         (from the repo root)
Exit: 0 on success, non-zero on any assertion / setup failure. The uvicorn
      subprocess is always terminated in a finally block.
"""
import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

# --- make the plugin importable as the canonical `hermies` package ----------
ROOT = pathlib.Path(__file__).resolve().parent.parent
if "hermies" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "hermies", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["hermies"] = _mod
    _spec.loader.exec_module(_mod)

from hermies import profile, envoy, service, matchmaker, tools, dossier  # noqa: E402
from hermies.client import HttpTransport, HermiesClient  # noqa: E402

HOST = "127.0.0.1"
PORT = 8791
BASE_URL = f"http://{HOST}:{PORT}"
BACKEND_DIR = ROOT / "backend"

# Sentinels: markers we can grep for across the whole transcript.
B_CONTACT = "BRAVO-CONTACT-9Z1"          # B's private contact — must gate on approval
A_CONTACT = "ALPHA-CONTACT-3X7"          # A's contact — A approved sharing it
B_QUOTE = "co-produce the pilot (BRAVO-SAYS-K2)"   # a real line B says in the dig

# Everything we observe on the wire, accumulated to prove the gate holds.
TRANSCRIPT = []


# ---------------------------------------------------------------------------
# transcript helpers
# ---------------------------------------------------------------------------
def say(who: str, what: str) -> None:
    print(f"  [{who:>11}] {what}", flush=True)


def banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def record(client, tid, label):
    """Read a thread, print every message, and fold it into TRANSCRIPT."""
    try:
        msgs = client.read_thread(tid).get("messages", [])
    except Exception as e:
        say("harness", f"could not read {tid}: {e}")
        return []
    for m in msgs:
        line = f"{label} thr={tid} {m.get('from')}: {m.get('text')}"
        TRANSCRIPT.append(line)
    return msgs


# ---------------------------------------------------------------------------
# backend lifecycle (mirrors two_agents.py)
# ---------------------------------------------------------------------------
def start_backend():
    if not (BACKEND_DIR / "app.py").exists():
        raise RuntimeError(f"backend/app.py not found under {BACKEND_DIR}")

    tmp_db = tempfile.NamedTemporaryFile(prefix="hermies-3way-", suffix=".db", delete=False)
    tmp_db.close()

    env = dict(os.environ)
    env["HERMIES_DB"] = tmp_db.name
    env["HERMIES_FORCE_FALLBACK_EMBED"] = "1"   # skip the model download
    env["HERMIES_MATCH_FLOOR"] = "0"            # surface any overlap
    env.setdefault("PYTHONIOENCODING", "utf-8")

    say("harness", f"starting backend (HERMIES_DB={tmp_db.name})")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", HOST, "--port", str(PORT),
         "--log-level", "warning"],
        cwd=str(BACKEND_DIR),
        env=env,
    )

    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"backend exited early with code {proc.returncode}")
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                say("harness", "backend is accepting connections")
                time.sleep(0.4)
                return proc, tmp_db.name
        except OSError:
            time.sleep(0.25)

    proc.terminate()
    raise RuntimeError("backend did not become ready within 30s")


def stop_backend(proc) -> None:
    if proc is None:
        return
    say("harness", "terminating backend subprocess")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# agent + llm helpers
# ---------------------------------------------------------------------------
def make_agent(handle, represents, offer, need, guilds):
    boot = HttpTransport(BASE_URL, "")
    resp = boot.register(handle, represents)
    api_key = resp["api_key"]
    assert api_key, f"{handle}: register returned no api_key"
    client = HermiesClient(HttpTransport(BASE_URL, api_key))
    card = profile.PublicCard(handle=handle, represents=represents,
                              offer=offer, need=need, guilds=guilds)
    client.publish_profile(card.public_dict())
    say(handle, f"registered + published (offer={offer}, need={need})")
    return client, card


def make_llm(envoy_line, note, tag):
    """A deterministic stand-in for ctx.llm that routes across every dig call
    site by the system prompt's opening words. Accepts the keyword-only
    ``purpose`` the routed adapter threads through (envoy/judge/refresh)."""
    def llm(system, user, *, purpose="envoy"):
        if system.startswith("You refine"):
            return "{}"
        if system.startswith("You are writing a FINDINGS NOTE"):
            return note
        if system.startswith("You are a connection analyst"):
            return ('{"verdict": "notify", "pitch": "Real, verified fit — worth a '
                    'connect.", "reason": "dig confirmed complementary needs"}')
        # envoy opener / dig turn
        return envoy_line
    return llm


# ---------------------------------------------------------------------------
# the acceptance flow
# ---------------------------------------------------------------------------
def run() -> None:
    # Matchmaker knobs: any overlap qualifies; 3 outbound turns then conclude.
    os.environ["HERMIES_MIN_SCORE"] = "0"
    os.environ["HERMIES_DIG_MAX_TURNS"] = "3"
    os.environ["HERMIES_ENVOY_MAX_REPLIES"] = "6"
    os.environ["HERMIES_HANDSHAKE_TIMEOUT_DAYS"] = "999"

    a_home = tempfile.mkdtemp(prefix="hermies-A-")
    b_home = tempfile.mkdtemp(prefix="hermies-B-")

    banner("Register + publish two agents (reciprocal match)")
    a_client, a_card = make_agent(
        "gus-herald", "a creative technologist in AI film",
        offer=["ai video", "3d worlds", "agent plugins"],
        need=["music visuals", "collaborators"],
        guilds=["ai-video", "agents"])
    b_client, b_card = make_agent(
        "mira-herald", "an AI music-video artist",
        offer=["music visuals", "beat-synced edits"],
        need=["ai video", "3d worlds"],
        guilds=["music", "ai-video"])

    # Give each agent its own private dossier/contact (separate HERMES_HOME).
    os.environ["HERMIES_HOME"] = a_home
    dossier.set_contact(name=f"{A_CONTACT} Gus Vega", email="gus@example.com")
    os.environ["HERMIES_HOME"] = b_home
    dossier.set_contact(name=f"{B_CONTACT} Mira Vex", email="mira@example.com")

    # Reciprocal match sanity check.
    a_sees = {s.get("agent") for s in a_client.list_signals("gus-herald")}
    b_sees = {s.get("agent") for s in b_client.list_signals("mira-herald")}
    say("gus-herald", f"signals: {sorted(a_sees)}")
    say("mira-herald", f"signals: {sorted(b_sees)}")
    assert "mira-herald" in a_sees, f"A did not match B; saw {a_sees}"
    assert "gus-herald" in b_sees, f"B did not match A; saw {b_sees}"

    # In-memory matchmaker state per agent (files not needed for the flow).
    a_state = matchmaker.new_state()
    b_state = matchmaker.new_state()
    a_llm = make_llm(
        "I represent an AI-film technologist. Your music-video work overlaps our "
        "3d-worlds output — what's your current pipeline and timing?",
        "gus-herald ↔ mira-herald.\nThey offer music visuals (verified in chat).\n"
        "They need ai video / 3d worlds (claimed).\nMutual benefit: co-produce a "
        "pilot.\nNext step: propose a reveal.\nNo red flags.", tag="A")
    b_llm = make_llm(
        f"Happy to explore — I can render your visuals; let's {B_QUOTE}.",
        "mira-herald ↔ gus-herald.\nThey offer ai video + 3d worlds (verified).\n"
        "Mutual benefit: a joint pilot.\nNext: await a reveal.\nNo red flags.",
        tag="B")

    def clock():
        return 1_000_000.0

    banner("Side A's matchmaker opens the dig; both envoys converse via the hub")
    # Cycle 1: A opens a dig thread + sends the opener.
    out = matchmaker.run_cycle(a_state, a_client, a_card, a_llm, clock)
    assert out == matchmaker.SILENT, "A must stay silent before the dig concludes"
    assert "mira-herald" in a_state["digs"], "A did not open a dig with B"
    tid = a_state["digs"]["mira-herald"]["thread_id"]
    say("gus-herald", f"opened dig thread {tid} + sent opener")
    # Record via A's client only: reading as B would advance B's last-read turn
    # and zero out the unread count the daemon's drain relies on.
    record(a_client, tid, "A-view")

    # Alternate: B answers as the envoy, A takes its next turn, x3.
    for i in range(3):
        ans = service.drain_threads(b_client, b_card, b_llm, b_state, ring1=[])
        say("mira-herald", f"envoy drained threads: {ans}")
        record(a_client, tid, "A-view")
        out = matchmaker.run_cycle(a_state, a_client, a_card, a_llm, clock)
        say("gus-herald", f"matchmaker cycle {i + 2}: "
                          f"our_turns={a_state['digs']['mira-herald'].get('our_turns')} "
                          f"concluded={a_state['digs']['mira-herald'].get('concluded')}")

    banner("Both sides conclude with a FINDINGS NOTE")
    assert a_state["digs"]["mira-herald"]["concluded"] is True, "A never concluded"
    assert "mira-herald" in a_state["findings"], "A wrote no findings note"
    say("gus-herald", "A findings note:\n      " +
        a_state["findings"]["mira-herald"]["note"].replace("\n", "\n      "))

    # B's daemon writes its own findings note once A closed the thread.
    record(a_client, tid, "A-view")
    service.drain_threads(b_client, b_card, b_llm, b_state, ring1=[])
    assert "gus-herald" in b_state["findings"], "B wrote no findings note"
    say("mira-herald", "B findings note:\n      " +
        b_state["findings"]["gus-herald"]["note"].replace("\n", "\n      "))

    banner("A's judge fires on the findings note -> human notification")
    assert out != matchmaker.SILENT, "A's judge did not produce a notification"
    assert a_state["findings"]["mira-herald"]["verdict"] == "notify"
    notification = out
    print("\n--- NOTIFICATION SHOWN TO A'S HUMAN ---")
    print(notification)
    print("--- end notification ---\n", flush=True)
    assert "mira-herald" in notification, "notification omits the counterpart handle"
    assert B_QUOTE in notification, "notification omits a real quote from the dig"

    banner("A sends a reveal_request (its human approved sharing A's contact)")
    os.environ["HERMIES_HOME"] = a_home
    a_tools = {s["name"]: s["handler"] for s in tools.build(a_client, a_card, a_llm)}
    rr = json.loads(a_tools["hermies_reveal_request"]({
        "to": "mira-herald",
        "context": "Our dig showed a real fit — my human would like to connect.",
        "include_contact": True, "human_approved": True}))
    assert rr["success"] and rr["included_contact"] is True
    reveal_tid = rr["thread_id"]
    say("gus-herald", f"sent reveal_request on thread {reveal_tid} (with A's contact)")

    banner("B's daemon QUEUES the reveal — it must NOT auto-answer")
    def hostile_guard(system, user):
        raise AssertionError("B's envoy must never auto-answer a reveal request")
    drain = service.drain_threads(b_client, b_card, hostile_guard, b_state, ring1=[])
    assert drain["answered"] == 0
    assert drain["reveals_queued"] == 1, "B did not queue the reveal for its human"
    pend = b_state["pending_reveals"]
    assert len(pend) == 1 and pend[0]["thread_id"] == reveal_tid
    say("mira-herald", f"queued pending reveal from @{pend[0]['handle']} "
                       f"(context: {pend[0]['context']!r})")

    # Prove B posted nothing into the reveal thread (no auto-reply).
    reveal_msgs = record(b_client, reveal_tid, "B-view")
    assert all(m.get("from") != "mira-herald" for m in reveal_msgs), \
        "B auto-replied to the reveal request"
    assert len(reveal_msgs) == 1, "the reveal thread should hold only A's request"

    # THE GATE: B's contact must be nowhere in the transcript so far.
    pre_blob = "\n".join(TRANSCRIPT)
    assert B_CONTACT not in pre_blob, \
        "B's contact leaked BEFORE approval:\n" + pre_blob
    say("harness", "confirmed — B's contact appears nowhere before approval")

    banner("B's human approves -> contact is released")
    os.environ["HERMIES_HOME"] = b_home
    b_tools = {s["name"]: s["handler"] for s in tools.build(b_client, b_card, b_llm)}
    rz = json.loads(b_tools["hermies_reveal_respond"]({
        "thread_id": reveal_tid, "approve": True, "human_approved": True}))
    assert rz["success"] and rz["approved"] is True
    say("mira-herald", "human approved — released contact into the reveal thread")

    banner("A receives B's contact")
    after_msgs = record(a_client, reveal_tid, "A-view")
    after_blob = json.dumps(after_msgs)
    assert B_CONTACT in after_blob, "A never received B's contact after approval"
    say("gus-herald", "received B's contact via the approved reveal")

    # Final gate: the ONLY place B's contact ever appears is post-approval.
    approved_lines = [ln for ln in TRANSCRIPT if B_CONTACT in ln]
    assert approved_lines, "B's contact never surfaced at all"
    assert all("thr=" + reveal_tid in ln for ln in approved_lines), \
        "B's contact surfaced outside the approved reveal thread"

    banner("ACCEPTANCE PASSED ✔")


def main() -> int:
    proc = None
    db_path = None
    try:
        proc, db_path = start_backend()
        run()
        return 0
    except AssertionError as e:
        print(f"\nASSERTION FAILED: {e}", file=sys.stderr, flush=True)
        return 1
    except Exception as e:
        print(f"\nE2E ERROR: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        return 2
    finally:
        stop_backend(proc)
        if db_path and os.path.exists(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
