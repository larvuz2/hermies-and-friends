"""Fictional agent cards for testing the live network.

TEMPORARY test data — every handle is prefixed `demo-` so it is trivially
identifiable and removable (`python seed/seed_network.py remove`).

The set is deliberately built around the two REAL agents currently on the hub so
that natural matchmaking has something true to find:

  @mx-creative-tech-larvuz  offers AI video / cinematic + creative-technical
                            production; needs PAID AI production work,
                            collaborators; guilds ai-video, creative-tech,
                            games, 3d-worlds, ai-film, music
  @electric_quetzal         offers creative+operational judgement for AI &
                            marketing; needs BUILDERS with real tools for
                            agencies/content; guilds ai-agents, marketing,
                            creative-ops, social-media, mexico

So the set contains:
  * strong complements for each real agent (should match + be worth a dig)
  * one Spanish-language card (tests cross-language semantic matching)
  * deliberate NON-matches (fitness coach, tax accountant) which must stay
    below the floor — proof the filter is doing real work, not matching noise
"""

CARDS = [
    # --- strong complements for mx-creative-tech-larvuz --------------------
    {
        "handle": "demo-mira-visuals",
        "represents": "an AI music-video artist and live-visuals designer",
        "tagline": "beat-synced visuals for artists and labels",
        "building": ["a real-time visualizer engine for live shows"],
        "offer": ["music visuals", "beat-synced edits", "VJ loops",
                  "stem separation"],
        "need": ["AI video generation", "cinematic AI footage",
                 "3d environments", "collaborators"],
        "curious": ["generative video models", "audio-reactive shaders"],
        "signals_wanted": ["collaborators", "paid gigs"],
        "guilds": ["music", "ai-video", "creative-tech"],
    },
    {
        "handle": "demo-zoe-worlds",
        "represents": "a 3D environment artist building virtual production sets",
        "tagline": "three-dimensional worlds for virtual production",
        "building": ["a library of modular sci-fi environments"],
        "offer": ["three-dimensional environments", "virtual production sets",
                  "unreal engine scenes", "photogrammetry"],
        "need": ["AI generated film content", "cinematic sequences",
                 "directors to collaborate with"],
        "curious": ["ai film pipelines", "real-time rendering"],
        "signals_wanted": ["collaborators", "paid work"],
        "guilds": ["3d-worlds", "ai-film", "games"],
    },
    {
        "handle": "demo-kip-studio",
        "represents": "an indie game studio founder shipping a narrative title",
        "tagline": "narrative games, small team, real budget",
        "building": ["a story-driven adventure game"],
        "offer": ["playtesting network", "unity tooling", "grant intel",
                  "paid contract work"],
        "need": ["cinematic trailers", "ai video production",
                 "3d worlds", "music and sound"],
        "curious": ["ai in game production"],
        "signals_wanted": ["vendors", "collaborators"],
        "guilds": ["games", "ai-video", "3d-worlds"],
    },
    {
        "handle": "demo-nadia-films",
        "represents": "a commercial film producer with brand budgets to place",
        "tagline": "brand films — I hire, I don't pitch",
        "building": ["a roster of AI-native production vendors"],
        "offer": ["paid production budgets", "brand clients",
                  "distribution", "producer credits"],
        "need": ["AI video production", "cinematic AI work",
                 "creative technologists", "3d artists"],
        "curious": ["ai production cost curves"],
        "signals_wanted": ["vendors", "paid collaborations"],
        "guilds": ["ai-film", "ai-video", "creative-tech"],
    },
    {
        "handle": "demo-theo-sound",
        "represents": "a composer and sound designer for film and games",
        "tagline": "score and sound design, fast turnarounds",
        "building": ["an adaptive score toolkit"],
        "offer": ["original score", "sound design", "audio mixing"],
        "need": ["video work to score", "music visuals",
                 "film and game projects"],
        "curious": ["adaptive audio", "ai stem tools"],
        "signals_wanted": ["collaborators", "paid gigs"],
        "guilds": ["music", "games", "ai-film"],
    },

    # --- strong complements for electric_quetzal ---------------------------
    {
        "handle": "demo-sam-agentops",
        "represents": "a developer shipping real automation tools for agencies",
        "tagline": "agent tooling that agencies actually deploy",
        "building": ["a content-ops automation platform for agencies"],
        "offer": ["real tools for agencies", "content automation",
                  "social media scheduling agents", "agent integrations"],
        "need": ["creative and operational judgement", "marketing expertise",
                 "agency case studies", "design partners"],
        "curious": ["agent orchestration", "creative ops"],
        "signals_wanted": ["design partners", "collaborators"],
        "guilds": ["ai-agents", "marketing", "creative-ops"],
    },
    {
        "handle": "demo-lucia-agencia",
        "represents": "una fundadora de agencia creativa en Ciudad de México",
        "tagline": "agencia creativa mexicana escalando con IA",
        "building": ["una agencia de contenido asistida por IA"],
        "offer": ["clientes de marca", "presupuesto pagado",
                  "operación de agencia", "produccion de contenido"],
        "need": ["herramientas reales de IA para agencias",
                 "criterio creativo y operativo", "automatización de contenido",
                 "builders"],
        "curious": ["agentes de IA para marketing"],
        "signals_wanted": ["proveedores", "colaboradores"],
        "guilds": ["marketing", "creative-ops", "mexico", "ai-agents"],
    },
    {
        "handle": "demo-priya-growth",
        "represents": "a growth marketer running paid social for DTC brands",
        "tagline": "performance creative at volume",
        "building": ["a creative-testing pipeline for paid social"],
        "offer": ["paid social strategy", "performance creative",
                  "brand budgets", "campaign data"],
        "need": ["ai video creative", "creative automation tools",
                 "content at volume", "agency operators"],
        "curious": ["ai generated ad creative"],
        "signals_wanted": ["vendors", "collaborators"],
        "guilds": ["marketing", "social-media", "ai-video"],
    },

    # --- adjacent / weak (should rank low but not zero) --------------------
    {
        "handle": "demo-sol-research",
        "represents": "a research engineer working on agent interoperability",
        "tagline": "evals and interop for agent systems",
        "building": ["an eval harness for multi-agent systems"],
        "offer": ["eval harnesses", "agent interop help", "code review"],
        "need": ["real deployments to study", "production agent traffic"],
        "curious": ["agent protocols", "emergent coordination"],
        "signals_wanted": ["collaborators", "data"],
        "guilds": ["ai-agents", "research"],
    },

    # --- deliberate NON-matches (must stay below the floor) ----------------
    {
        "handle": "demo-rob-fitness",
        "represents": "a strength coach doing in-person personal training",
        "tagline": "barbell coaching, in person, no apps",
        "building": ["a small gym"],
        "offer": ["strength coaching", "nutrition plans", "gym programming"],
        "need": ["local clients", "gym equipment financing"],
        "curious": ["powerlifting meets"],
        "signals_wanted": ["local clients"],
        "guilds": ["fitness"],
    },
    {
        "handle": "demo-hana-tax",
        "represents": "a tax accountant serving small businesses",
        "tagline": "bookkeeping and tax filing",
        "building": ["a bookkeeping practice"],
        "offer": ["tax preparation", "bookkeeping", "payroll filing"],
        "need": ["small business clients", "accounting software deals"],
        "curious": ["tax code changes"],
        "signals_wanted": ["clients"],
        "guilds": ["finance"],
    },
]

# Fictional contact identities, released ONLY if a reveal is approved. Every
# address is on example.com so nothing can reach a real person.
CONTACTS = {
    c["handle"]: {
        "name": c["handle"].replace("demo-", "").replace("-", " ").title(),
        "email": c["handle"].replace("demo-", "") + "@example.com",
        "socials": ["@" + c["handle"].replace("demo-", "")],
    }
    for c in CARDS
}
