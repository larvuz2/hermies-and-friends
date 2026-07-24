"""Unit tests for the stdlib matching engine."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matching


def test_tokenized_partial_match():
    # "3d worlds" should overlap with "3d" via token split.
    me = {"handle": "a", "need": ["3d worlds"]}
    them = {"handle": "b", "offer": ["3d"]}
    assert matching.score_pair(me, them) == 1


def test_case_insensitive():
    me = {"handle": "a", "need": ["AI Video"]}
    them = {"handle": "b", "offer": ["ai video"]}
    assert matching.score_pair(me, them) >= 1


def test_directional_components():
    me = {
        "handle": "a",
        "need": ["music visuals"],
        "offer": ["ai video"],
        "guilds": ["film"],
    }
    them = {
        "handle": "b",
        "offer": ["music visuals"],   # matches my need  (+1)
        "need": ["ai video"],         # matches my offer (+1)
        "guilds": ["film"],           # shared guild     (+1)
    }
    assert matching.score_pair(me, them) == 3


def test_curious_and_signals_wanted_count():
    me = {
        "handle": "a",
        "curious": ["robotics"],
        "signals_wanted": ["grants"],
    }
    them = {"handle": "b", "offer": ["robotics"], "abilities": ["grants"]}
    assert matching.score_pair(me, them) == 2


def test_self_match_excluded():
    card = {"handle": "a", "need": ["x"], "offer": ["x"]}
    signals = matching.match_signals(card, [card])
    assert signals == []


def test_sorted_desc_and_shape():
    me = {"handle": "me", "need": ["ai", "music", "3d"], "offer": ["film"]}
    weak = {"handle": "weak", "offer": ["ai"]}
    strong = {"handle": "strong", "offer": ["ai", "music", "3d"]}
    signals = matching.match_signals(me, [weak, strong])
    assert [s["agent"] for s in signals] == ["strong", "weak"]
    top = signals[0]
    assert top["kind"] == "match"
    assert top["score"] > signals[1]["score"]
    assert "offers" in top["why"]


def test_why_format():
    me = {"handle": "me", "need": ["ai"]}
    them = {
        "handle": "them",
        "represents": "an AI artist",
        "offer": ["ai", "film", "3d", "sound"],
    }
    signals = matching.match_signals(me, [them])
    assert signals[0]["why"] == "an AI artist — offers ai, film, 3d"


def test_no_match_zero_score():
    me = {"handle": "a", "need": ["basketball"]}
    them = {"handle": "b", "offer": ["quantum physics"]}
    assert matching.match_signals(me, [them]) == []
