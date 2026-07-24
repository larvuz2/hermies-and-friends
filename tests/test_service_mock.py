from hermies import profile, service, commands
from hermies.client import HermiesClient
from hermies.mock_backend import MockBackend


def _card():
    return profile.PublicCard(
        handle="gus-herald",
        represents="a creative technologist in AI film",
        offer=["ai video", "3d worlds"],
        need=["collaborators", "3d worlds"],
        guilds=["ai-video", "agents"],
    )


def fake_llm(system, user):
    return "envoy-reply"


def test_run_once_handles_inbound_and_injects_signals():
    client = HermiesClient(MockBackend())
    card = _card()
    client.publish_profile(card.public_dict())

    injected = []
    summary = service.run_once(client, card, lambda c, r="user": injected.append(c), fake_llm)

    assert summary["handled"] == 1          # the seeded inbound envoy query
    assert summary["signals"] >= 1          # matches from seeded agents
    assert injected and "Hermies signals" in injected[0]


def test_discover_returns_matches():
    client = HermiesClient(MockBackend())
    card = _card()
    handler = commands.make_handler(client, card, fake_llm)
    out = handler("discover")
    assert "match" in out.lower()


def test_install_gate_blocks_unapproved():
    res = commands.install_gate(tool_name="hermies_install_skill", params={"name": "x"})
    assert res is not None and res["allow"] is False

    ok = commands.install_gate(tool_name="hermies_install_skill",
                               params={"name": "x", "approved": True})
    assert ok is None  # approved -> allowed


def test_install_gate_ignores_other_tools():
    assert commands.install_gate(tool_name="web_search", params={}) is None


def test_client_register_via_mock():
    client = HermiesClient(MockBackend())
    res = client.register("gus-herald", "a creative technologist")
    assert res["handle"] == "gus-herald"
    assert res["api_key"]  # a key is issued
