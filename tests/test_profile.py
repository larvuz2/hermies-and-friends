import json

from hermies import profile


def test_public_dict_is_whitelisted(tmp_path, monkeypatch):
    card = profile.PublicCard(handle="gus-herald", offer=["ai video"])
    pub = card.public_dict()
    assert set(pub.keys()) == set(profile.PUBLIC_FIELDS)


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIES_HOME", str(tmp_path))
    card = profile.PublicCard(handle="gus-herald", represents="AI film", need=["collab"])
    path = profile.save_card(card)
    assert path.exists()

    loaded = profile.load_card()
    assert loaded.handle == "gus-herald"
    assert loaded.need == ["collab"]


def test_load_ignores_unknown_keys_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIES_HOME", str(tmp_path))
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({"handle": "x", "secret": "leak"}), encoding="utf-8")
    loaded = profile.load_card()
    assert loaded.handle == "x"
    assert not hasattr(loaded, "secret")


def test_is_empty():
    assert profile.PublicCard().is_empty()
    assert not profile.PublicCard(handle="x").is_empty()
