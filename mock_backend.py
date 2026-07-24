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
