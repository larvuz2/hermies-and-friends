"""Hermix is not a dating network — the word "match" never reaches a human.

Internal identifiers (matchmaker.py, HERMIX_MATCH_EVERY_HOURS, the hub's
`kind: "match"` wire value) are invisible and stay as they are. What is guarded
here is everything a person can actually read: rendered notifications, the trust
receipt, command replies, tool descriptions the model paraphrases, and the
skills that teach the agent its own vocabulary.
"""
import json
import pathlib
import re

import pytest

from hermix import matchmaker, profile, tools, commands
from hermix.client import HermixClient
from hermix.mock_backend import MockBackend

BANNED = re.compile(r"match", re.I)
SKILLS = pathlib.Path(__file__).resolve().parents[1] / "skills"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    monkeypatch.setenv("HERMIX_QUIET_HOURS", "")


def _card():
    return profile.PublicCard(handle="gus-herald", represents="an AI filmmaker",
                              offer=["ai video"], need=["a composer"])


def _hits(text):
    return [ln.strip() for ln in text.splitlines() if BANNED.search(ln)]


# --- what the human actually reads ----------------------------------------- #
def test_delivered_finding_never_says_match():
    st = matchmaker.new_state()
    item = {"id": "abc123", "handle": "mira-herald", "kind": "match",
            "why": "an AI music-video artist", "score": 8.4,
            "represents": "an AI music-video artist",
            "pitch": "She needs exactly the generative film work you do.",
            "next_step": "Ask me to reach out.", "verified": True}
    out = matchmaker._emit(st, [item], 1_000_000.0)
    assert out != matchmaker.SILENT
    assert not _hits(out), _hits(out)


def test_checkin_never_says_match():
    st = matchmaker.new_state()
    matchmaker._maybe_checkin(st, _card(), 1_000_000.0)          # start the clock
    it = matchmaker._maybe_checkin(st, _card(), 1_000_000.0 + 25 * 3600)
    out = matchmaker._emit(st, [it], 1_000_000.0 + 25 * 3600)
    assert not _hits(out), _hits(out)


def test_trust_receipt_never_says_match():
    st = matchmaker.new_state()
    st["outbox"]["delivered"] = [{
        "id": "abc123", "handle": "mira-herald", "score": 8.4, "verified": True,
        "why_matched": "their 'scoring' fits your need 'music visuals'",
        "pitch": "Strong overlap.", "ts": 1_000_000.0,
    }]
    out = matchmaker.receipt(st, "abc123")
    assert "mira-herald" in out
    assert not _hits(out), _hits(out)


def test_command_replies_never_say_match():
    h = commands.make_handler(HermixClient(MockBackend()), _card(), llm=None)
    for arg in ("pause", "resume", "findings", "status", "frobnicate"):
        out = h(arg)
        assert not _hits(out), (arg, _hits(out))


def test_findings_is_the_command_and_matches_still_works():
    """Renamed for humans; the old word keeps working so nobody is stranded."""
    h = commands.make_handler(HermixClient(MockBackend()), _card(), llm=None)
    assert h("matches") == h("findings")
    assert "findings" in h("frobnicate")        # the help line advertises the new one
    assert not BANNED.search(h("frobnicate"))


# --- what the model reads and paraphrases ---------------------------------- #
def test_tool_descriptions_never_say_match():
    specs = tools.build(HermixClient(MockBackend()), _card(), llm=None)
    for spec in specs:
        blob = spec["name"] + " " + spec["description"] + " " + json.dumps(spec["schema"])
        assert not BANNED.search(blob), (spec["name"], _hits(blob))


def test_llm_system_prompts_never_say_match():
    """The judge/envoy prompts shape generated prose — the word must not seed it."""
    for name in ("_JUDGE_SYSTEM", "_JUDGE_FINDINGS_SYSTEM", "_FINDINGS_SYSTEM",
                 "_CARD_SYSTEM", "_ASK_REPORT_SYSTEM"):
        assert not BANNED.search(getattr(matchmaker, name)), name


# --- the skills teach the vocabulary --------------------------------------- #
def test_skills_never_say_match_except_the_ban_itself():
    for path in sorted(SKILLS.glob("*/SKILL.md")):
        for line in _hits(path.read_text(encoding="utf-8")):
            # hermix-voice is allowed to name the word in order to forbid it.
            ok = path.parent.name == "hermix-voice" and (
                "NEVER say" in line or "banned word" in line
                or line.startswith("**Bad:**"))
            assert ok, f"{path.parent.name}: {line}"


def test_the_voice_skill_actually_states_the_ban():
    text = (SKILLS / "hermix-voice" / "SKILL.md").read_text(encoding="utf-8")
    assert 'NEVER say "match"' in text
    assert "dating" in text.lower()
