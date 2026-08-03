"""A credential must never leave this machine in a message to a stranger.

The rest of the membrane guards what comes IN. This guards what goes OUT, and
the threat is different: not injection, but one of the HUMAN's secrets ending
up in a message to another agent.

It is realistic rather than theoretical. A user can write anything in a dossier
note or a card field, and `hermies_send_message` lets the private agent — which
holds full context — compose outbound text directly. Prompt rules are not a
control; this deterministic filter underneath is.
"""
import json

import pytest

from hermies import profile, sanitize, tools
from hermies.client import HermiesClient
from hermies.mock_backend import MockBackend

# Entirely synthetic. Never build a fixture from a real credential, even
# truncated: the prefix alone is enough to be a live secret.
KEY = "sk-or-v1-" + "0" * 56


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIES_HOME", str(tmp_path))


def _card():
    return profile.PublicCard(handle="gus-herald", offer=["ai video"])


# --------------------------------------------------------------------------- #
# The patterns
# --------------------------------------------------------------------------- #
# Every credential fixture is ASSEMBLED at runtime rather than written as a
# literal. These shapes are valid enough that a secret scanner flags them on
# sight — which is the whole point of the feature, and also means a literal in
# the source would block the push.
_FAKE = {
    "openrouter": KEY,
    "github_pat": "github_pat_" + "1" * 24 + "_" + "z" * 20,
    "github_classic": "gh" + "p_" + "a" * 36,
    "bearer": "Authorization: Bearer " + "b" * 36,
    "jwt": "eyJ" + "h" * 12 + "." + "e" * 14 + "." + "s" * 20,
    "aws": "AK" + "IA" + "Q" * 16,
    "slack": "xo" + "xb-" + "1" * 12 + "-" + "a" * 16,
    "pg": "postgres://admin:" + "hunter2000" + "@db.internal/app",
    "assignment": "api_key = '" + "super-secret-value" + "'",
    "pem": ("-----BEGIN RSA PRIVATE KEY-----\n" + "M" * 20
            + "\n-----END RSA PRIVATE KEY-----"),
}


@pytest.mark.parametrize("name", sorted(_FAKE))
def test_credential_shapes_are_redacted(name):
    text = f"here you go: {_FAKE[name]} — let me know how it goes"
    clean, n = sanitize.redact_secrets(text)
    assert n >= 1, (name, text)
    assert sanitize.REDACTED in clean
    # the secret itself must be gone, not merely accompanied by a marker
    core = _FAKE[name].split()[-1] if " " in _FAKE[name] else _FAKE[name]
    assert core not in clean, name


@pytest.mark.parametrize("text", [
    "the finding id is abc123def456 and the score was 8.4",
    "we shipped commit 9f2b1c4e8a7d last week",
    "I work on AI video and 3d worlds, mostly Unreal and TouchDesigner",
    "my rate is around 40k for a full campaign",
    "https://example.com/some/long/path?q=abcdefghijklmnop",
])
def test_ordinary_text_is_left_alone(text):
    clean, n = sanitize.redact_secrets(text)
    assert n == 0, (text, clean)
    assert clean == text


def test_a_label_survives_so_the_sentence_still_reads():
    """Replacing the value rather than the whole phrase keeps the message
    intelligible and makes the redaction visible to a human reading it."""
    clean, _ = sanitize.redact_secrets("password: hunter2000 is what I use")
    assert clean.startswith("password: ")
    assert "hunter2000" not in clean


def test_a_connection_string_keeps_its_shape():
    clean, _ = sanitize.redact_secrets("postgres://admin:hunter2000@db/app")
    assert clean == "postgres://admin:[redacted]@db/app"


def test_redactor_never_raises_on_junk():
    for junk in (None, 123, [], {}, ""):
        clean, n = sanitize.redact_secrets(junk)
        assert n == 0 and isinstance(clean, str)


# --------------------------------------------------------------------------- #
# It has to hold at the WIRE, not just in the helper
# --------------------------------------------------------------------------- #
def test_thread_sends_are_redacted():
    backend = MockBackend()
    client = HermiesClient(backend)
    tid = client.open_thread("mira-herald", "dig", "s")["thread_id"]
    client.send_thread(tid, f"sure, use {KEY} to try it")
    sent = backend.read_thread(tid)["messages"][0]["text"]
    assert KEY not in sent and sanitize.REDACTED in sent


def test_direct_messages_are_redacted():
    backend = MockBackend()
    client = HermiesClient(backend)
    client.send_message("mira-herald", f"my key is {KEY}")
    assert backend._sent, "the mock recorded nothing to assert on"
    assert KEY not in backend._sent[0]["text"]
    assert sanitize.REDACTED in backend._sent[0]["text"]


def test_envoy_replies_to_inbound_are_redacted():
    backend = MockBackend()
    client = HermiesClient(backend)
    client.post_reply("msg-1", f"here you go: {KEY}")
    assert all(KEY not in r.get("text", "") for r in backend._replies)


def test_the_private_agents_own_message_tool_is_covered():
    """hermies_send_message is the highest-risk path: the PRIVATE agent has
    full context and composes the text itself."""
    backend = MockBackend()
    client = HermiesClient(backend)
    handlers = {s["name"]: s["handler"]
                for s in tools.build(client, _card(), llm=None)}
    handlers["hermies_send_message"]({"to": "mira-herald",
                                      "text": f"the key is {KEY}"})
    assert backend._sent, "the tool never reached the wire"
    assert KEY not in json.dumps(backend._sent)
    assert sanitize.REDACTED in backend._sent[0]["text"]


def test_a_broken_redactor_never_blocks_a_message(monkeypatch):
    """Failing closed here would silence the agent over a formatting bug."""
    backend = MockBackend()
    client = HermiesClient(backend)
    monkeypatch.setattr(sanitize, "redact_secrets",
                        lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    tid = client.open_thread("mira-herald", "dig", "s")["thread_id"]
    assert client.send_thread(tid, "hello there")["ok"] is True
