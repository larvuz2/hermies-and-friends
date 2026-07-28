"""Threaded conversations: the MockBackend must honour the frozen hub contract
(open/send/close/list/read), enforce the 12-message turn budget with a
409-equivalent on overflow, and drive the hermies_ask / hermies_thread tools."""
import json

import pytest

from hermies import tools, profile
from hermies.client import HermiesClient
from hermies.mock_backend import MockBackend


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Keep local state per-test — asks/digs now persist, so a shared
    HERMES_HOME would leak between tests (and into the developer's real one)."""
    monkeypatch.setenv("HERMIES_HOME", str(tmp_path))


def _card():
    return profile.PublicCard(handle="gus-herald", offer=["ai video"])


def _handlers(client):
    return {s["name"]: s["handler"] for s in tools.build(client, _card(), llm=None)}


# --------------------------------------------------------------------------- #
# MockBackend thread contract
# --------------------------------------------------------------------------- #
def test_open_send_read_close_roundtrip():
    b = MockBackend()
    tid = b.open_thread("mira-herald", "dig", "a fit?")["thread_id"]
    assert tid

    assert b.send_thread(tid, "hi there")["ok"] is True
    b.script_reply(tid, "hi back")            # the counterpart envoy answers
    msgs = b.read_thread(tid)["messages"]
    assert [m["from"] for m in msgs] == ["me", "mira-herald"]
    assert msgs[1]["turn"] == 2

    listed = b.list_threads()["threads"]
    assert listed[0]["thread_id"] == tid and listed[0]["turns"] == 2
    assert listed[0]["unread"] == 0          # read_thread cleared it

    assert b.close_thread(tid)["ok"] is True
    # 409-equivalent once concluded
    res = b.send_thread(tid, "too late")
    assert res.get("status") == 409 and "ok" not in res


def test_turn_budget_rejects_the_thirteenth_message():
    b = MockBackend()
    tid = b.open_thread("x", "dig", "s")["thread_id"]
    for i in range(12):                      # messages 1..12 fill the budget
        res = b.send_thread(tid, f"msg {i}")
        assert res["ok"] is True and res["turn"] == i + 1
    res = b.send_thread(tid, "the 13th")
    assert res.get("status") == 409 and "ok" not in res
    assert b.list_threads()["threads"][0]["turns"] == 12


def test_budget_counts_both_parties():
    b = MockBackend()
    tid = b.open_thread("x", "dig", "s")["thread_id"]
    for i in range(6):
        assert b.send_thread(tid, "mine")["ok"] is True
        assert b.script_reply(tid, "theirs")["ok"] is True
    # 12 total used; both next attempts are refused.
    assert b.send_thread(tid, "over")["status"] == 409
    assert b.script_reply(tid, "over")["status"] == 409


def test_read_unknown_thread_is_404():
    assert MockBackend().read_thread("nope").get("status") == 404


# --------------------------------------------------------------------------- #
# Tools over the client
# --------------------------------------------------------------------------- #
def test_hermies_ask_opens_ask_thread_and_returns_thread_id():
    b = MockBackend()
    h = _handlers(HermiesClient(b))
    out = json.loads(h["hermies_ask"]({"to": "sol-herald", "question": "what stack?"}))
    assert out["success"] is True and out["thread_id"]
    th = b.list_threads()["threads"][0]
    assert th["kind"] == "ask" and th["turns"] == 1


def test_hermies_ask_requires_to_and_question():
    h = _handlers(HermiesClient(MockBackend()))
    assert json.loads(h["hermies_ask"]({"to": "x"}))["success"] is False


def test_hermies_thread_list_read_send_close():
    b = MockBackend()
    h = _handlers(HermiesClient(b))
    tid = b.open_thread("mira", "dig", "s")["thread_id"]
    b.script_reply(tid, "hello`with`fence")

    read = json.loads(h["hermies_thread"]({"action": "read", "thread_id": tid}))
    assert "`" not in read["messages"][0]["text"]   # inbound sanitized

    sent = json.loads(h["hermies_thread"]({"action": "send", "thread_id": tid,
                                           "text": "reply"}))
    assert sent["ok"] is True

    listed = json.loads(h["hermies_thread"]({"action": "list"}))
    assert listed["threads"][0]["thread_id"] == tid

    closed = json.loads(h["hermies_thread"]({"action": "close", "thread_id": tid}))
    assert closed["ok"] is True


def test_hermies_thread_unknown_action():
    h = _handlers(HermiesClient(MockBackend()))
    assert "error" in json.loads(h["hermies_thread"]({"action": "frobnicate"}))
