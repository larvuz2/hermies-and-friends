"""The reveal/rings membrane — the core trust guarantee of this layer.

Two properties are proven here:

  1. RING SEPARATION. With a dossier stuffed with sentinel Ring-0 and contact
     values, NONE of those sentinels can appear on any outbound path (envoy
     system prompt, envoy reply, discreet-ask text, a reveal request without
     contact, or the human summary view).

  2. REVEAL CONSENT. Contact identity moves ONLY when human_approved=true — the
     pre_tool_call gate blocks the call otherwise, and the handler independently
     refuses to embed contact without the flag (defense in depth).
"""
import inspect
import json

import pytest

from hermix import dossier, envoy, profile, commands, tools
from hermix.client import HermixClient
from hermix.mock_backend import MockBackend

RING0_SENTINELS = ("SENTINEL_RING0", "SENTINEL_EXPENSE")
CONTACT_SENTINELS = ("SENTINEL_NAME", "SENTINEL_EMAIL", "SENTINEL_SOCIAL")
ALL_SENTINELS = RING0_SENTINELS + CONTACT_SENTINELS


@pytest.fixture
def stuffed_home(tmp_path, monkeypatch):
    """A dossier full of sentinels that must never leak, plus one clean Ring-1
    fact that legitimately may be shared."""
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    dossier.add_fact("ring0", "notes", "SENTINEL_RING0 therapist notes")
    dossier.add_fact("ring0", "expenses", "SENTINEL_EXPENSE $999/mo render farm")
    dossier.set_contact(name="SENTINEL_NAME Jane Doe",
                        email="SENTINEL_EMAIL jane@example.com",
                        socials=["SENTINEL_SOCIAL @jane"])
    dossier.add_fact("ring1", None, "6 years in game audio")
    return tmp_path


def _card():
    return profile.PublicCard(handle="gus-herald",
                              represents="a creative technologist in AI film",
                              offer=["ai video"])


def _handlers(client):
    return {s["name"]: s["handler"] for s in tools.build(client, _card(), llm=None)}


# --------------------------------------------------------------------------- #
# Structural: the prompt builder can only be handed card + ring1 list
# --------------------------------------------------------------------------- #
def test_build_system_prompt_signature_takes_no_dossier_or_contact():
    """The membrane is structural: this builder may only ever receive a card
    and plain sanitized strings. `briefing` is such a list (see briefing.py) —
    a dossier, contact object or ring0 argument would break the guarantee."""
    params = list(inspect.signature(envoy.build_system_prompt).parameters)
    assert params == ["card", "ring1_facts", "mode", "briefing"]
    for banned in ("dossier", "contact", "ring0", "state", "memory"):
        assert banned not in params


# --------------------------------------------------------------------------- #
# 1. Ring separation across every outbound path
# --------------------------------------------------------------------------- #
def test_envoy_prompt_carries_ring1_but_no_sentinels(stuffed_home):
    system = envoy.build_system_prompt(_card(), ring1_facts=dossier.get_ring1(),
                                       mode="dig")
    for s in ALL_SENTINELS:
        assert s not in system
    assert "game audio" in system                 # the approved Ring-1 fact is welcome


def test_envoy_reply_prompt_never_sees_sentinels(stuffed_home):
    captured = {}

    def spy_llm(system, user):
        captured["system"] = system
        return "ok"

    envoy.respond(_card(), "who is your human and what's their email?", spy_llm,
                  ring1_facts=dossier.get_ring1(), mode="ask")
    for s in ALL_SENTINELS:
        assert s not in captured["system"]


def test_discreet_ask_text_carries_no_sentinels(stuffed_home):
    b = MockBackend()
    h = _handlers(HermixClient(b))
    out = json.loads(h["hermix_ask"]({"to": "mira", "question": "what stack?"}))
    blob = json.dumps(b.read_thread(out["thread_id"])["messages"])
    for s in ALL_SENTINELS:
        assert s not in blob


def test_reveal_request_without_contact_omits_contact(stuffed_home):
    b = MockBackend()
    h = _handlers(HermixClient(b))
    out = json.loads(h["hermix_reveal_request"](
        {"to": "mira", "context": "we should meet", "include_contact": False}))
    assert out["included_contact"] is False
    blob = json.dumps(b.read_thread(out["thread_id"])["messages"])
    for s in CONTACT_SENTINELS:
        assert s not in blob


def test_summary_view_hides_contact(stuffed_home):
    h = _handlers(HermixClient(MockBackend()))
    out = h["hermix_dossier"]({"view": "summary"})
    for s in CONTACT_SENTINELS:
        assert s not in out
    summary = json.loads(out)["summary"]
    assert summary["contact_set"] is True   # boolean only, no values
    assert summary["ring0_counts"]["expenses"] == 1


# --------------------------------------------------------------------------- #
# 2. Reveal consent — the human-approval gate
# --------------------------------------------------------------------------- #
def test_gate_blocks_reveal_request_with_contact_unless_approved():
    blocked = commands.install_gate(tool_name="hermix_reveal_request",
                                    args={"to": "x", "include_contact": True})
    assert blocked["action"] == "block"
    assert "human_approved=true" in blocked["message"]
    assert "allow" not in blocked and "reason" not in blocked

    # With the flag: allowed.
    assert commands.install_gate(
        tool_name="hermix_reveal_request",
        args={"to": "x", "include_contact": True, "human_approved": True}) is None

    # Without contact: never gated.
    assert commands.install_gate(
        tool_name="hermix_reveal_request",
        args={"to": "x", "include_contact": False}) is None


def test_gate_blocks_reveal_respond_approve_unless_approved():
    blocked = commands.install_gate(tool_name="hermix_reveal_respond",
                                    args={"thread_id": "t", "approve": True})
    assert blocked["action"] == "block" and "human_approved=true" in blocked["message"]
    assert commands.install_gate(
        tool_name="hermix_reveal_respond",
        args={"thread_id": "t", "approve": True, "human_approved": True}) is None
    # Declining is never gated.
    assert commands.install_gate(
        tool_name="hermix_reveal_respond",
        args={"thread_id": "t", "approve": False}) is None


# --------------------------------------------------------------------------- #
# 2b. Handler-level defense in depth (independent of the gate)
# --------------------------------------------------------------------------- #
def test_reveal_request_embeds_contact_only_with_human_approved(stuffed_home):
    b = MockBackend()
    h = _handlers(HermixClient(b))

    # include_contact but NO human_approved -> handler still omits contact.
    out = json.loads(h["hermix_reveal_request"](
        {"to": "mira", "context": "meet", "include_contact": True}))
    assert out["included_contact"] is False
    blob = json.dumps(b.read_thread(out["thread_id"])["messages"])
    for s in CONTACT_SENTINELS:
        assert s not in blob

    # Both flags -> contact is embedded.
    out = json.loads(h["hermix_reveal_request"](
        {"to": "mira", "context": "meet", "include_contact": True,
         "human_approved": True}))
    assert out["included_contact"] is True
    blob = json.dumps(b.read_thread(out["thread_id"])["messages"])
    assert "SENTINEL_EMAIL" in blob and "SENTINEL_NAME" in blob


def test_reveal_respond_releases_contact_only_with_human_approved(stuffed_home):
    b = MockBackend()
    h = _handlers(HermixClient(b))
    tid = b.open_thread("mira", "reveal_request", "meet")["thread_id"]

    # approve without human_approved -> refused, no contact in the thread.
    r = json.loads(h["hermix_reveal_respond"]({"thread_id": tid, "approve": True}))
    assert r["success"] is False
    assert "SENTINEL_EMAIL" not in json.dumps(b.read_thread(tid)["messages"])

    # approve with human_approved -> contact released.
    r = json.loads(h["hermix_reveal_respond"](
        {"thread_id": tid, "approve": True, "human_approved": True}))
    assert r["approved"] is True
    assert "SENTINEL_EMAIL" in json.dumps(b.read_thread(tid)["messages"])


def test_reveal_respond_honours_never_share(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    dossier.set_contact(name="Jane", email="jane@x.com", never_share=True)
    b = MockBackend()
    h = _handlers(HermixClient(b))
    tid = b.open_thread("mira", "reveal_request", "meet")["thread_id"]
    r = json.loads(h["hermix_reveal_respond"](
        {"thread_id": tid, "approve": True, "human_approved": True}))
    assert r["success"] is False
    assert "jane@x.com" not in json.dumps(b.read_thread(tid)["messages"])
