"""The dossier is the private half of the membrane. These tests pin its store
semantics (roundtrip, sanitize-on-write, atomic .bak), the intents lifecycle,
and the invariant that the summary view NEVER exposes contact values."""
import json

import pytest

from hermix import dossier


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- #
# Store roundtrip + shape
# --------------------------------------------------------------------------- #
def test_fresh_load_has_canonical_shape():
    d = dossier.load()
    assert set(d["ring0"]) == set(dossier.RING0_SECTIONS)
    assert d["ring1"] == [] and d["intents"] == []
    assert d["contact"] == {"name": "", "email": "", "socials": [], "never_share": False}
    assert d["onboarded"] is False


def test_add_fact_roundtrips_to_disk():
    dossier.add_fact("ring0", "goals", "ship hermix v1")
    dossier.add_fact("ring0", "projects", "the embassy plugin")
    d = dossier.load()
    assert d["ring0"]["goals"] == ["ship hermix v1"]
    assert d["ring0"]["projects"] == ["the embassy plugin"]


def test_unknown_ring0_section_falls_back_to_notes():
    dossier.add_fact("ring0", "not_a_section", "loose thought")
    assert dossier.load()["ring0"]["notes"] == ["loose thought"]


def test_ring1_facts_are_flat_and_deduped():
    dossier.add_fact("ring1", None, "6 years in game audio")
    dossier.add_fact("ring1", None, "6 years in game audio")  # dup
    assert dossier.get_ring1() == ["6 years in game audio"]


def test_move_to_ring1_promotes_and_removes_from_ring0():
    dossier.add_fact("ring0", "work_history", "led audio at a studio")
    dossier.move_to_ring1("led audio at a studio", from_section="work_history")
    d = dossier.load()
    assert "led audio at a studio" in d["ring1"]
    assert "led audio at a studio" not in d["ring0"]["work_history"]


# --------------------------------------------------------------------------- #
# Sanitize on write
# --------------------------------------------------------------------------- #
def test_strings_are_sanitized_on_write():
    dirty = "line one\nline two with `fence` and \x00 a null and ​ zero-width"
    dossier.add_fact("ring0", "notes", dirty)
    note = dossier.load()["ring0"]["notes"][0]
    assert "\n" not in note and "`" not in note
    assert "\x00" not in note and "​" not in note
    assert "line one line two" in note  # content preserved, structure neutralized


def test_contact_is_sanitized_on_write():
    dossier.set_contact(name="Jane`inject`Doe\n", email="jane@x.com")
    c = dossier.get_contact()
    assert "`" not in c["name"] and "\n" not in c["name"]
    assert c["email"] == "jane@x.com"


# --------------------------------------------------------------------------- #
# Atomicity: second save leaves a .bak behind
# --------------------------------------------------------------------------- #
def test_atomic_write_creates_bak_on_second_save():
    dossier.add_fact("ring0", "goals", "a")   # first write, no .bak yet
    p = dossier._dossier_path()
    assert p.exists() and not p.with_name(p.name + ".bak").exists()
    dossier.add_fact("ring0", "goals", "b")   # second write backs up the first
    assert p.with_name(p.name + ".bak").exists()
    assert not p.with_name(p.name + ".tmp").exists()  # temp cleaned up by rename


# --------------------------------------------------------------------------- #
# Onboarding flag
# --------------------------------------------------------------------------- #
def test_onboarded_flag_persists():
    assert dossier.is_onboarded() is False
    dossier.set_onboarded(True)
    assert dossier.is_onboarded() is True


# --------------------------------------------------------------------------- #
# Intents lifecycle
# --------------------------------------------------------------------------- #
def test_intents_add_list_retire():
    i1 = dossier.add_intent("find an audio cofounder")
    i2 = dossier.add_intent("cheaper render farm")
    assert i1["status"] == "active" and i1["id"] and i2["id"] != i1["id"]
    assert len(dossier.list_intents()) == 2

    retired = dossier.retire_intent(i1["id"])
    assert retired["status"] == "stale"
    by_id = {i["id"]: i["status"] for i in dossier.list_intents()}
    assert by_id[i1["id"]] == "stale" and by_id[i2["id"]] == "active"


def test_retire_unknown_intent_is_noop():
    assert dossier.retire_intent("999") is None


def test_empty_intent_text_is_rejected():
    assert dossier.add_intent("   ") is None
    assert dossier.list_intents() == []


# --------------------------------------------------------------------------- #
# Summary hides contact values
# --------------------------------------------------------------------------- #
def test_summary_reports_counts_and_never_contact_values():
    dossier.add_fact("ring0", "goals", "g1")
    dossier.add_fact("ring0", "goals", "g2")
    dossier.add_fact("ring1", None, "shareable color")
    dossier.set_contact(name="SENTINEL_NAME", email="SENTINEL_EMAIL",
                        socials=["SENTINEL_SOCIAL"])
    dossier.add_intent("hunt for X")

    s = dossier.summary()
    blob = json.dumps(s)
    assert "SENTINEL_NAME" not in blob
    assert "SENTINEL_EMAIL" not in blob
    assert "SENTINEL_SOCIAL" not in blob
    assert s["ring0_counts"]["goals"] == 2
    assert s["ring1"] == ["shareable color"]
    assert s["contact_set"] is True   # boolean only
    assert len(s["intents"]) == 1
