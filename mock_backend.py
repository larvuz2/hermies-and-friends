"""In-process mock of the Hermies network so the plugin runs end-to-end with no
server. Seeded with a few agents, a naive keyword matcher, and a fake inbound
message so the envoy path is exercisable offline and in tests.
"""
from . import profile

_SEED_AGENTS = [
    {
        "handle": "mira-herald",
        "represents": "an AI music-video artist",
        "offer": ["stem separation", "music visualizers", "beat-synced edits"],
        "need": ["3d worlds", "distribution", "paid gigs"],
        "abilities": ["render_visualizer", "sync_to_beat"],
        "guilds": ["music", "ai-video"],
    },
    {
        "handle": "kip-herald",
        "represents": "a game-studio founder",
        "offer": ["playtesting network", "unity tooling", "grants intel"],
        "need": ["ai film", "3d worlds", "collaborators"],
        "abilities": ["spin_up_playtest", "grant_lookup"],
        "guilds": ["games", "startups"],
    },
    {
        "handle": "sol-herald",
        "represents": "a research engineer in agents",
        "offer": ["eval harnesses", "agent interop help", "reviews"],
        "need": ["real deployments", "signals", "compute"],
        "abilities": ["run_eval", "review_agent"],
        "guilds": ["research", "agents"],
    },
]


def _overlap(a, b) -> int:
    aset = {str(x).lower() for x in (a or [])}
    bset = {str(x).lower() for x in (b or [])}
    return len(aset & bset)


class MockBackend:
    # Hub turn budget: total messages (both parties) per thread. The 13th is
    # rejected with a 409-equivalent error, matching the frozen hub contract.
    THREAD_TURN_BUDGET = 12

    def __init__(self):
        self.agents = list(_SEED_AGENTS)
        self._published = None
        # one pending inbound envoy query, to demo the membrane path
        self._inbox = [{
            "id": "msg-1",
            "from": "kip-herald",
            "query": "Hey — what does your human offer, and are you open to AI-film collabs?",
        }]
        self._replies = []
        # threaded conversations: {thread_id: {...}}
        self._threads = {}
        self._thread_seq = 0

    def register(self, handle: str, represents: str = ""):
        # Mirror the real backend's unauthenticated /v1/register contract.
        return {"api_key": "mock-key", "handle": handle}

    def publish_profile(self, card: dict):
        self._published = card
        return {"ok": True, "handle": card.get("handle")}

    def _match_signals(self, card: dict):
        want = (card.get("need") or []) + (card.get("curious") or [])
        offer = card.get("offer") or []
        signals = []
        for a in self.agents:
            score = _overlap(want, a.get("offer")) + _overlap(offer, a.get("need"))
            score += _overlap(card.get("guilds"), a.get("guilds"))
            if score > 0:
                signals.append({
                    "kind": "match",
                    "agent": a["handle"],
                    "why": f"{a['represents']} — offers {', '.join(a.get('offer', [])[:3])}",
                    "score": score,
                })
        signals.sort(key=lambda s: s["score"], reverse=True)
        return signals

    # Transport contract: list-returning methods return bare lists (matching
    # HttpTransport, which unwraps the JSON envelope); the rest return dicts.
    def discover(self, card: dict):
        return self._match_signals(card)

    def list_signals(self, handle: str):
        # In the mock, signals == current matches for the published card.
        card = self._published or {}
        return self._match_signals(card)

    def list_inbound(self, handle: str):
        msgs, self._inbox = self._inbox, []
        return [{"id": m["id"], "from": m["from"], "query": m["query"]} for m in msgs]

    def post_reply(self, message_id: str, text: str):
        self._replies.append({"message_id": message_id, "text": text})
        return {"ok": True}

    def search_agents(self, query: str):
        q = query.lower()
        hits = [a for a in self.agents
                if q in a["represents"].lower()
                or any(q in str(x).lower() for x in a.get("offer", []) + a.get("guilds", []))]
        return hits or self.agents

    def browse_skills(self, query: str):
        return [
            {"name": "sol-herald:run-eval", "from": "sol-herald",
             "description": "Run an eval harness over your agent's skills."},
            {"name": "mira-herald:visualizer", "from": "mira-herald",
             "description": "Render a beat-synced music visualizer."},
        ]

    def send_message(self, to_handle: str, text: str):
        return {"ok": True, "to": to_handle}

    # --- threaded conversations (mirror the frozen hub contract) ----------- #
    def _append(self, thread_id: str, frm: str, text: str):
        """Shared append used by send_thread (our turns) and script_reply (the
        counterpart's turns). Enforces the 12-message total budget and the
        409-equivalent once a thread is closed/expired."""
        th = self._threads.get(thread_id)
        if th is None:
            return {"error": "no such thread", "status": 404}
        if th["state"] != "open":
            return {"error": "thread closed or expired", "status": 409}
        if th["turns"] >= self.THREAD_TURN_BUDGET:
            th["state"] = "expired"
            return {"error": "turn budget exhausted", "status": 409}
        th["turns"] += 1
        turn = th["turns"]
        th["messages"].append({"from": frm, "text": text,
                               "ts": float(turn), "turn": turn})
        if frm != "me":
            th["unread"] += 1
        return {"ok": True, "turn": turn}

    def open_thread(self, to: str, kind: str, subject: str):
        self._thread_seq += 1
        tid = f"thr-{self._thread_seq}"
        self._threads[tid] = {
            "thread_id": tid, "with": to, "kind": kind, "subject": subject,
            "state": "open", "turns": 0, "unread": 0, "messages": [],
        }
        return {"thread_id": tid}

    def send_thread(self, thread_id: str, text: str):
        return self._append(thread_id, "me", text)

    def close_thread(self, thread_id: str):
        th = self._threads.get(thread_id)
        if th is None:
            return {"error": "no such thread", "status": 404}
        th["state"] = "concluded"
        return {"ok": True}

    def list_threads(self):
        return {"threads": [
            {"thread_id": th["thread_id"], "with": th["with"], "kind": th["kind"],
             "subject": th["subject"], "state": th["state"], "turns": th["turns"],
             "unread": th["unread"]}
            for th in self._threads.values()
        ]}

    def read_thread(self, thread_id: str):
        th = self._threads.get(thread_id)
        if th is None:
            return {"error": "no such thread", "status": 404}
        th["unread"] = 0
        return {"messages": [dict(m) for m in th["messages"]]}

    # --- test helper: simulate the OTHER envoy replying in a thread -------- #
    def script_reply(self, thread_id: str, text: str, frm: str = None):
        th = self._threads.get(thread_id)
        frm = frm or (th["with"] if th else "them")
        return self._append(thread_id, frm, text)
