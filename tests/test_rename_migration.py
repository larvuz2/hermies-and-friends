"""The rename must not cost anyone their dossier.

Moving $HERMES_HOME/hermies -> $HERMES_HOME/hermix orphans the human's private
notes, contact identity, standing intents and every dig they have ever had. An
upgrading user would look brand new and start over from an empty card.
"""
import json

import pytest

from hermix import migrate


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    # migrate.run() deliberately no-ops when an explicit data dir is set, so
    # these tests drive it through a fake Hermes home instead.
    monkeypatch.delenv("HERMIX_HOME", raising=False)
    monkeypatch.delenv("HERMIES_HOME", raising=False)
    monkeypatch.setattr(migrate, "_home_root", lambda: tmp_path)
    return tmp_path


def _seed_legacy(root):
    old = root / "hermies"
    (old / "sub").mkdir(parents=True)
    (old / "dossier.json").write_text(
        json.dumps({"ring0": {"projects": ["a film"]}, "onboarded": True}),
        encoding="utf-8")
    (old / "matchmaker.json").write_text(json.dumps({"digs": {"mira": {}}}),
                                         encoding="utf-8")
    (old / "sub" / "nested.txt").write_text("keep me", encoding="utf-8")
    return old


def test_the_dossier_survives_the_rename(_clean):
    root = _clean
    _seed_legacy(root)
    res = migrate.run()
    assert res["migrated"] and res["files"] == 3
    moved = json.loads((root / "hermix" / "dossier.json").read_text(encoding="utf-8"))
    assert moved["onboarded"] is True and moved["ring0"]["projects"] == ["a film"]
    assert (root / "hermix" / "sub" / "nested.txt").read_text(encoding="utf-8") == "keep me"


def test_the_original_is_left_intact(_clean):
    """A half-finished rename must never be able to destroy the only copy."""
    root = _clean
    old = _seed_legacy(root)
    migrate.run()
    assert (old / "dossier.json").is_file()


def test_an_existing_new_directory_is_authoritative(_clean):
    root = _clean
    _seed_legacy(root)
    (root / "hermix").mkdir()
    (root / "hermix" / "dossier.json").write_text('{"onboarded": false}',
                                                  encoding="utf-8")
    res = migrate.run()
    assert res["migrated"] is False
    assert '"onboarded": false' in (root / "hermix" / "dossier.json").read_text(
        encoding="utf-8")


def test_nothing_to_migrate_is_not_an_error(_clean):
    res = migrate.run()
    assert res["migrated"] is False and res["error"] == ""


def test_a_failure_never_raises(_clean, monkeypatch):
    _seed_legacy(_clean)
    monkeypatch.setattr(migrate.shutil, "copytree",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    res = migrate.run()
    assert res["migrated"] is False and "disk full" in res["error"]


def test_an_explicit_data_dir_is_left_alone(monkeypatch, tmp_path):
    """HERMIX_HOME points straight at the data dir — there is no sibling."""
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    assert migrate.run()["migrated"] is False
