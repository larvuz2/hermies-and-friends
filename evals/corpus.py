"""Deterministic synthetic corpus for the Hermies matchmaking quality harness.

No LLM calls, no wall-clock reads, no OS randomness: everything is assembled
from a seeded ``random.Random`` over hand-authored templates, so ``cards()``
returns byte-identical output on every process and platform.

Structure
---------
* ~30 **planted ground-truth pairs** (60 cards) hand-authored so a human would
  say "these two should obviously meet". Tagged cross-vocabulary (the semantic
  link uses *different words*, which a token matcher cannot bridge) and/or
  personal (non-professional: language exchange, hobby/travel/fitness buddies).
* ~140 **filler cards** across 12 domains, seeded-random assembly from rich
  per-domain vocab pools.
* ~20 **noisy cards** (empty-ish, vague like "stuff", keyword-stuffed spam).

Exports
-------
* ``cards() -> list[dict]``            every public card (planted + filler + noise)
* ``ground_truth_pairs() -> list[(handleA, handleB, note)]``
* ``last_seen_map() -> dict[handle, int]``   deterministic presence timestamps
* ``noisy_handles() -> set[str]``     the spam/empty cards, for spam-resistance
* ``cross_vocab_pairs()`` / ``personal_pairs()``   tagged subsets
* ``stats() -> dict``                 corpus summary for the scorecard

Card schema (matches the frozen interface): string ``handle`` / ``tagline`` /
``represents`` and list ``building`` / ``offer`` / ``need`` / ``curious`` /
``avoid`` / ``abilities`` / ``signals_wanted`` / ``guilds``.
"""
import random

SEED = 20260724
# Fixed reference "now" (epoch seconds). NOT a wall-clock read — deterministic.
NOW = 1_720_000_000
_DAY = 86_400

_LIST_FIELDS = (
    "building", "offer", "need", "curious", "avoid",
    "abilities", "signals_wanted", "guilds",
)


def _card(handle, tagline, represents, *, building=None, offer=None, need=None,
          curious=None, avoid=None, abilities=None, signals_wanted=None,
          guilds=None):
    return {
        "handle": handle,
        "tagline": tagline,
        "represents": represents,
        "building": list(building or []),
        "offer": list(offer or []),
        "need": list(need or []),
        "curious": list(curious or []),
        "avoid": list(avoid or []),
        "abilities": list(abilities or []),
        "signals_wanted": list(signals_wanted or []),
        "guilds": list(guilds or []),
    }


# --------------------------------------------------------------------------- #
# Planted ground-truth pairs.
#
# Each entry: (card_a, card_b, note, cross_vocab, personal).
# * cross_vocab pairs deliberately share NO salient tokens and NO guilds — the
#   only bridge is meaning, so a token matcher must miss them and a real
#   embedding model must catch them.
# * same-vocab pairs share a distinctive multi-word hook (rare in the filler
#   pools) so a competent token matcher ranks the partner near the top.
# --------------------------------------------------------------------------- #
def _planted():
    P = []

    # 1 — CROSS-VOCAB: "3d worlds" vs "three-dimensional environments"
    P.append((
        _card("@voxel-sandbox", "immersive 3d worlds for browser games",
              "a studio building explorable 3d worlds",
              offer=["immersive 3d worlds", "virtual sandbox spaces",
                     "explorable open maps"],
              need=["playtesters for our sandbox"],
              guilds=["indie-web-games"]),
        _card("@spatial-levels", "level designer for three-dimensional environments",
              "a designer crafting three-dimensional environments",
              offer=["three-dimensional environment art", "volumetric scene layout"],
              need=["three-dimensional environments to populate",
                    "spatial level design collaborators"],
              guilds=["level-design-collective"]),
        "3d worlds <-> three-dimensional environments (same concept, different words)",
        True, False))

    # 2 — CROSS-VOCAB: "beat-synced visuals" vs "music video effects"
    P.append((
        _card("@pulse-visuals", "beat-synced visuals for live shows",
              "a VJ making beat-synced visuals",
              offer=["beat-synced visuals", "reactive light shows", "tempo-driven projections"],
              need=["tracks to accompany on stage"],
              guilds=["vj-circuit"]),
        _card("@clip-fx", "music video effects and motion design",
              "an editor producing music video effects",
              offer=["music video effects", "motion design for songs"],
              need=["artists who want music video effects", "songs to cut motion for"],
              guilds=["motion-editors"]),
        "beat-synced visuals <-> music video effects (same craft, different words)",
        True, False))

    # 3 — CROSS-VOCAB: "find a cofounder" vs "join an early startup as partner"
    P.append((
        _card("@solo-founder-lea", "solo founder who wants to find a cofounder",
              "a solo founder building a climate SaaS",
              offer=["a working climate SaaS prototype", "domain expertise in energy"],
              need=["find a cofounder", "a co-owner to run product with me"],
              guilds=["climate-builders"]),
        _card("@partner-seeker-mo", "engineer looking to join an early startup as partner",
              "an engineer ready to help lead a young company",
              offer=["ready to co-build a venture from the ground up",
                     "an equity-stake technical partnership"],
              need=["join an early startup as an equity partner",
                    "a founding seat on an ambitious team"],
              guilds=["operators-network"]),
        "find a cofounder <-> join an early startup as partner (two sides, no shared words)",
        True, False))

    # 4 — CROSS-VOCAB: "generative film pipeline" vs "AI-assisted movie production"
    P.append((
        _card("@genfilm-lab", "a generative film pipeline for short stories",
              "a lab building a generative film pipeline",
              offer=["a generative film pipeline", "text-to-shot storyboarding"],
              need=["directors to pilot the pipeline"],
              guilds=["synthetic-cinema"]),
        _card("@reel-automaton", "AI-assisted movie production workflows",
              "a shop running AI-assisted movie production",
              offer=["AI-assisted movie production workflow", "automated post pipelines"],
              need=["teams wanting AI-assisted movie production",
                    "shorts to produce end to end"],
              guilds=["auto-post-house"]),
        "generative film pipeline <-> AI-assisted movie production (identical intent)",
        True, False))

    # 5 — CROSS-VOCAB + PERSONAL: Spanish tandem vs "practicar ingles" exchange
    P.append((
        _card("@tandem-anita", "native Spanish speaker wanting a language tandem",
              "a person seeking a Spanish/English language tandem",
              offer=["native Spanish conversation", "help with Latin American slang"],
              need=["a language tandem partner for Spanish", "weekly speaking practice"],
              guilds=["tandem-lounge"]),
        _card("@practicar-diego", "quiero practicar ingles con un hablante nativo",
              "alguien que quiere practicar ingles",
              offer=["conversacion en espanol", "intercambio de idiomas"],
              need=["practicar ingles con nativos", "un companero de intercambio"],
              guilds=["intercambio-de-idiomas"]),
        "Spanish tandem <-> practicar ingles exchange (both want the same swap)",
        True, True))

    # 6 — CROSS-VOCAB: "procedural terrain generation" vs "algorithmic landscape creation"
    P.append((
        _card("@procgen-terra", "procedural terrain generation for open worlds",
              "a tools dev who writes procedural terrain generation",
              offer=["procedural terrain generation", "noise-based heightmaps"],
              need=["games needing procedural terrain generation"],
              guilds=["tech-art-guild"]),
        _card("@landscape-algo", "algorithmic landscape creation and biomes",
              "an artist making algorithmic landscape creation",
              offer=["algorithmic landscape creation", "generated biome systems"],
              need=["projects wanting algorithmic landscape creation"],
              guilds=["generative-art-lab"]),
        "procedural terrain generation <-> algorithmic landscape creation (same thing)",
        True, False))

    # 7 — CROSS-VOCAB: "get users to invite friends" vs "word-of-mouth referral engines"
    P.append((
        _card("@loop-growth", "a subscription app that wants users bringing users",
              "a startup after person-to-person growth",
              offer=["a subscription app with early traction"],
              need=["a way to get our users to invite their friends",
                    "person-to-person acquisition"],
              guilds=["growth-hackers"]),
        _card("@referral-engine", "word-of-mouth referral engines that compound",
              "a marketer building word-of-mouth referral engines",
              offer=["word-of-mouth referral engines", "member-get-member programs"],
              need=["apps that want referrals to spread"],
              guilds=["retention-club"]),
        "get users to invite friends <-> word-of-mouth referral engines (complementary)",
        True, False))

    # 8 — CROSS-VOCAB + PERSONAL: chess sparring vs over-the-board practice opponent
    P.append((
        _card("@sparring-knight", "looking for a chess sparring partner",
              "a club player wanting a chess sparring partner",
              offer=["rapid matches most evenings", "opening prep discussion"],
              need=["a chess sparring partner around 1600", "regular sparring bouts"],
              guilds=["evening-chess"]),
        _card("@otb-opponent", "seeking a regular over-the-board practice opponent",
              "a person wanting an over-the-board practice opponent",
              offer=["over-the-board play at the cafe", "post-session analysis"],
              need=["a steady over-the-board practice opponent", "someone near my rating"],
              guilds=["cafe-boardgames"]),
        "chess sparring partner <-> over-the-board practice opponent (same hobby need)",
        True, True))

    # 9 — PERSONAL: travel buddy, Southeast Asia
    P.append((
        _card("@backpack-sam", "backpacking Southeast Asia this fall, want a travel buddy",
              "a traveler planning a Southeast Asia backpacking trip",
              offer=["a flexible three-month itinerary", "budget hostel know-how"],
              need=["a travel buddy for Southeast Asia", "someone to share the trip"],
              curious=["Vietnam", "Thailand", "slow travel"],
              guilds=["nomad-lounge"]),
        _card("@wander-priya", "planning a Southeast Asia trip, looking for someone to explore with",
              "a traveler organizing a Southeast Asia journey",
              offer=["route planning", "shared costs on the road"],
              need=["a travel buddy for Southeast Asia", "a companion for Thailand and Vietnam"],
              curious=["Thailand", "Vietnam", "backpacking"],
              guilds=["nomad-lounge"]),
        "travel buddies for the same Southeast Asia trip",
        False, True))

    # 10 — PERSONAL: weekend photo-walk buddy
    P.append((
        _card("@street-lens-jo", "weekend street photography walks in the city",
              "a hobbyist into weekend street photography walks",
              offer=["knowledge of good light spots", "film camera to share"],
              need=["a photo-walk buddy on weekends", "people to shoot streets with"],
              guilds=["photo-walkers"]),
        _card("@snapshot-ravi", "looking for a photo-walk buddy on weekends",
              "a photographer seeking weekend photo-walk company",
              offer=["a second shooter perspective", "editing tips"],
              need=["weekend street photography walk partners", "a photo-walk buddy"],
              guilds=["photo-walkers"]),
        "weekend photo-walk buddies (personal hobby)",
        False, True))

    # 11 — PERSONAL: half-marathon training buddy
    P.append((
        _card("@runner-mika", "training for a half marathon, want a running partner",
              "a runner prepping a half marathon",
              offer=["a 12-week plan", "steady 9-min pace"],
              need=["a half-marathon training buddy", "morning long-run partner"],
              guilds=["morning-runners"]),
        _card("@dawn-strider", "half-marathon training buddy for morning runs",
              "a runner looking for a morning training partner",
              offer=["accountability", "routes around the park"],
              need=["someone training for a half marathon", "a morning long-run partner"],
              guilds=["morning-runners"]),
        "half-marathon training buddies (personal fitness)",
        False, True))

    # 12 — PERSONAL: weekend hiking companion
    P.append((
        _card("@trailhead-noa", "weekend hiking and trail companion wanted",
              "a hiker seeking weekend trail companions",
              offer=["a car to reach trailheads", "map and route knowledge"],
              need=["people to hike with on weekends", "a weekend trail companion"],
              guilds=["weekend-trails"]),
        _card("@ridgeline-kit", "looking for people to hike with on weekends",
              "a hiker wanting weekend hiking partners",
              offer=["trail snacks and good pace", "photos of the summit"],
              need=["a weekend hiking companion", "a group to hit trails with"],
              guilds=["weekend-trails"]),
        "weekend hiking companions (personal hobby)",
        False, True))

    # 13 — music: producer with beats <-> vocalist
    P.append((
        _card("@beatsmith-ketu", "hip-hop producer with instrumental beats",
              "a producer sitting on a catalog of instrumental beats",
              offer=["instrumental beats", "hip-hop production", "beat licensing"],
              need=["a vocalist for topline melodies", "singers to feature"],
              guilds=["beat-market"]),
        _card("@topline-vee", "vocalist writing toplines over instrumental beats",
              "a vocalist looking for beats to sing over",
              offer=["vocal recording", "topline melodies", "hooks"],
              need=["instrumental beats to sing over", "a hip-hop producer to work with"],
              guilds=["beat-market"]),
        "producer's instrumental beats <-> vocalist's toplines",
        False, False))

    # 14 — game dev needs pixel artist <-> pixel artist wants game projects
    P.append((
        _card("@dungeon-dev-ori", "indie game dev needing pixel art for a roguelike",
              "a solo dev making a pixel-art roguelike",
              offer=["gameplay programming", "revenue share"],
              need=["a pixel artist for a roguelike", "pixel art sprites and tilesets"],
              guilds=["pixel-roguelikes"]),
        _card("@sprite-mila", "pixel artist looking for game projects",
              "a pixel artist wanting roguelike work",
              offer=["pixel art sprites and tilesets", "animation frames"],
              need=["indie game projects needing pixel art", "a roguelike to art"],
              guilds=["pixel-roguelikes"]),
        "roguelike dev needs pixel art <-> pixel artist wants roguelike work",
        False, False))

    # 15 — indie film director needs colorist <-> colorist
    P.append((
        _card("@shortfilm-dir-ines", "indie film director needing a colorist",
              "a director finishing an indie short",
              offer=["a finished edit", "festival submission plan"],
              need=["a colorist for color grading", "someone to grade my short film"],
              guilds=["indie-film-post"]),
        _card("@grade-house", "colorist offering color grading for indie films",
              "a colorist grading indie shorts",
              offer=["color grading", "DaVinci Resolve finishing"],
              need=["indie films needing a colorist", "shorts to color grade"],
              guilds=["indie-film-post"]),
        "indie director needs a colorist <-> colorist grades indie shorts",
        False, False))

    # 16 — founder needs seed funding <-> angel investor
    P.append((
        _card("@seed-stage-uma", "founder raising a seed round for dev tools",
              "a founder raising seed funding",
              offer=["a growing dev-tools startup", "strong early traction"],
              need=["seed funding", "an angel investor for a seed check"],
              guilds=["devtools-founders"]),
        _card("@angel-halden", "angel investor writing seed checks into dev tools",
              "an angel investor backing seed-stage startups",
              offer=["seed checks", "intros to later-stage VCs"],
              need=["seed-stage dev-tools founders", "startups raising a seed round"],
              guilds=["angel-syndicate"]),
        "founder raising seed funding <-> angel writing seed checks",
        False, False))

    # 17 — freelance React dev <-> agency needs React contractor
    P.append((
        _card("@react-freelance-ty", "freelance React developer open to contracts",
              "a freelance React developer",
              offer=["freelance React development", "TypeScript front-end work"],
              need=["React contracts", "agencies hiring a React contractor"],
              guilds=["react-freelancers"]),
        _card("@agency-northwind", "agency needing a React contractor for client work",
              "an agency staffing React projects",
              offer=["steady client contracts", "design handoff"],
              need=["a freelance React developer", "a React contractor for TypeScript work"],
              guilds=["react-freelancers"]),
        "freelance React dev <-> agency needs a React contractor",
        False, False))

    # 18 — ML researcher needs GPU compute <-> lab offers GPU cluster
    P.append((
        _card("@ml-researcher-fen", "ML researcher needing GPU compute for training",
              "a researcher training vision models",
              offer=["a novel training method", "co-authorship"],
              need=["GPU compute for training", "access to a GPU cluster"],
              guilds=["ml-research-net"]),
        _card("@gpu-commons", "lab offering GPU cluster access to researchers",
              "a lab sharing spare GPU cluster time",
              offer=["GPU cluster access", "compute grants"],
              need=["ML researchers needing GPU compute", "training workloads to host"],
              guilds=["ml-research-net"]),
        "ML researcher needs GPU compute <-> lab offers GPU cluster access",
        False, False))

    # 19 — marketer needs SEO audit <-> SEO specialist
    P.append((
        _card("@saas-market-bel", "SaaS marketer needing an SEO audit",
              "a marketer growing a SaaS site",
              offer=["a content budget", "long-term retainer"],
              need=["an SEO audit", "an SEO specialist to fix rankings"],
              guilds=["saas-growth"]),
        _card("@seo-doctor", "SEO specialist offering technical SEO audits",
              "an SEO specialist auditing SaaS sites",
              offer=["technical SEO audits", "keyword strategy"],
              need=["SaaS sites needing an SEO audit", "marketers wanting SEO help"],
              guilds=["saas-growth"]),
        "marketer needs SEO audit <-> SEO specialist offers audits",
        False, False))

    # 20 — author needs book cover <-> illustrator
    P.append((
        _card("@indie-author-wren", "indie author needing a book cover illustration",
              "a novelist self-publishing a fantasy book",
              offer=["a finished manuscript", "royalty share"],
              need=["a book cover illustration", "an illustrator for my fantasy novel"],
              guilds=["selfpub-authors"]),
        _card("@cover-atelier", "illustrator specializing in book cover illustration",
              "an illustrator drawing book covers",
              offer=["book cover illustration", "fantasy character art"],
              need=["authors needing a book cover illustration", "novels to illustrate"],
              guilds=["selfpub-authors"]),
        "indie author needs a book cover <-> book-cover illustrator",
        False, False))

    # 21 — fitness client needs nutrition coaching <-> coach (professional)
    P.append((
        _card("@lift-client-dana", "amateur lifter needing nutrition coaching",
              "a lifter wanting a nutrition plan",
              offer=["commitment and consistency", "a monthly coaching budget"],
              need=["nutrition coaching", "a coach to build a meal plan"],
              guilds=["strength-coaching"]),
        _card("@coach-priya-fit", "fitness coach offering nutrition plans",
              "a certified coach writing nutrition plans",
              offer=["nutrition coaching", "personalized meal plans", "macro tracking"],
              need=["clients needing nutrition coaching", "lifters wanting a meal plan"],
              guilds=["strength-coaching"]),
        "lifter needs nutrition coaching <-> coach offers nutrition plans",
        False, False))

    # 22 — 3D modeler needs rigging <-> rigger
    P.append((
        _card("@model-shop-quill", "3D modeler needing character rigging",
              "a modeler making game-ready characters",
              offer=["high-quality 3D models", "clean topology"],
              need=["character rigging", "a rigger for my 3D models"],
              guilds=["char-art-guild"]),
        _card("@rig-master-oz", "technical artist offering character rigging",
              "a rigger prepping models for animation",
              offer=["character rigging", "skinning and weight painting"],
              need=["3D models needing rigging", "modelers wanting a rigger"],
              guilds=["char-art-guild"]),
        "3D modeler needs rigging <-> rigger offers character rigging",
        False, False))

    # 23 — podcast host needs audio editor <-> audio engineer
    P.append((
        _card("@pod-host-sable", "podcast host needing an audio editor",
              "a host running a weekly interview podcast",
              offer=["a steady weekly episode flow", "editing retainer"],
              need=["an audio editor for podcast episodes", "someone to clean up audio"],
              guilds=["podcast-makers"]),
        _card("@audio-eng-flint", "audio engineer offering podcast editing",
              "an engineer editing and mastering podcasts",
              offer=["podcast editing", "noise cleanup and mastering"],
              need=["podcasts needing an audio editor", "hosts wanting clean audio"],
              guilds=["podcast-makers"]),
        "podcast host needs an audio editor <-> audio engineer edits podcasts",
        False, False))

    # 24 — VR dev needs Unity multiplayer <-> Unity netcode specialist
    P.append((
        _card("@vr-studio-hale", "VR studio needing Unity multiplayer netcode",
              "a VR studio building a co-op experience",
              offer=["a shipped VR experience", "a contract budget"],
              need=["Unity multiplayer netcode", "a netcode specialist for co-op VR"],
              guilds=["unity-multiplayer"]),
        _card("@netcode-vale", "Unity netcode specialist for multiplayer games",
              "an engineer specializing in Unity multiplayer",
              offer=["Unity multiplayer netcode", "lag compensation and sync"],
              need=["VR games needing Unity multiplayer", "studios wanting netcode help"],
              guilds=["unity-multiplayer"]),
        "VR studio needs Unity multiplayer <-> Unity netcode specialist",
        False, False))

    # 25 — data scientist needs labeled dataset <-> annotation service
    P.append((
        _card("@ds-vale-nur", "data scientist needing a labeled dataset",
              "a data scientist training a classifier",
              offer=["a modeling pipeline", "a labeling budget"],
              need=["a labeled dataset", "an annotation service for images"],
              guilds=["applied-ml"]),
        _card("@label-forge", "annotation service offering data labeling",
              "a service producing labeled datasets",
              offer=["image annotation", "labeled datasets", "quality review"],
              need=["teams needing a labeled dataset", "data scientists wanting annotation"],
              guilds=["applied-ml"]),
        "data scientist needs a labeled dataset <-> annotation service labels data",
        False, False))

    # 26 — brand designer needs copywriter <-> copywriter
    P.append((
        _card("@brand-studio-lio", "brand designer needing a copywriter",
              "a designer building brand identities",
              offer=["brand identity design", "visual systems"],
              need=["a copywriter for brand voice", "brand copy and taglines"],
              guilds=["brand-collective"]),
        _card("@wordsmith-gray", "copywriter offering brand copy and voice",
              "a copywriter shaping brand voice",
              offer=["brand copy", "taglines and messaging", "brand voice guides"],
              need=["brand designers needing a copywriter", "studios wanting brand copy"],
              guilds=["brand-collective"]),
        "brand designer needs a copywriter <-> copywriter offers brand copy",
        False, False))

    # 27 — producer needs mixing engineer <-> mixing engineer
    P.append((
        _card("@track-maker-esi", "music producer needing a mixing engineer",
              "a producer finishing an EP",
              offer=["finished multitracks", "a mixing budget"],
              need=["a mixing engineer", "someone to mix and master my tracks"],
              guilds=["mix-master-guild"]),
        _card("@mixdown-rue", "mixing engineer offering mixdowns and mastering",
              "an engineer mixing and mastering records",
              offer=["mixing and mastering", "stem mixdowns"],
              need=["producers needing a mixing engineer", "tracks to mix and master"],
              guilds=["mix-master-guild"]),
        "producer needs a mixing engineer <-> mixing engineer offers mixdowns",
        False, False))

    # 28 — startup needs technical cofounder CTO <-> CTO seeking founding role
    P.append((
        _card("@nontech-founder-bo", "non-technical founder needing a technical cofounder",
              "a founder with a marketplace idea and users",
              offer=["early users and revenue", "sales and fundraising"],
              need=["a technical cofounder", "a CTO to build the product"],
              guilds=["founder-dating"]),
        _card("@fractional-cto-io", "CTO seeking a founding technical cofounder role",
              "an engineering leader wanting to co-found",
              offer=["technical leadership", "architecture and hiring"],
              need=["a startup needing a technical cofounder", "a founding CTO role"],
              guilds=["founder-dating"]),
        "non-technical founder needs a technical cofounder <-> CTO seeks founding role",
        False, False))

    # 29 — animator needs storyboard artist <-> storyboard artist
    P.append((
        _card("@anim-studio-yara", "animation studio needing a storyboard artist",
              "a studio pre-producing an animated short",
              offer=["a funded short", "an animation team"],
              need=["a storyboard artist", "storyboards for an animated short"],
              guilds=["animation-preprod"]),
        _card("@board-artist-cove", "storyboard artist offering boards for animation",
              "a storyboard artist working in animation",
              offer=["storyboards", "animatics and shot planning"],
              need=["animation studios needing a storyboard artist", "shorts to board"],
              guilds=["animation-preprod"]),
        "animation studio needs a storyboard artist <-> storyboard artist offers boards",
        False, False))

    # 30 — researcher needs co-author <-> academic collaborator
    P.append((
        _card("@phd-candidate-sol", "PhD candidate seeking a co-author for a paper",
              "a candidate writing a systems paper",
              offer=["experiments and a draft", "novel results"],
              need=["a co-author for a paper", "a collaborator on a publication"],
              guilds=["systems-research"]),
        _card("@postdoc-arden", "postdoc looking to collaborate on a publication",
              "a postdoc open to new co-authorships",
              offer=["writing and analysis", "related prior work"],
              need=["a co-author for a paper", "a collaborator on a publication"],
              guilds=["systems-research"]),
        "PhD candidate needs a co-author <-> postdoc wants a collaboration",
        False, False))

    return P


# --------------------------------------------------------------------------- #
# Filler domains: rich vocab pools; cards assembled by seeded sampling.
# --------------------------------------------------------------------------- #
_DOMAINS = {
    "ai-film": {
        "guild": "ai-video-lab",
        "tagline": ["AI video experiments", "generative shorts", "ML-driven post"],
        "represents": ["an AI video creator", "a generative-film tinkerer",
                       "an ML post-production artist"],
        "building": ["a text-to-video short", "an AI b-roll tool", "a face-swap demo"],
        "offer": ["prompt engineering for video", "AI upscaling", "synthetic b-roll",
                  "model fine-tuning for style", "diffusion pipelines"],
        "need": ["a narrative editor", "GPU time for renders", "a sound designer",
                 "story ideas", "beta users"],
        "curious": ["diffusion models", "camera control", "lip sync"],
        "abilities": ["python", "comfyui", "after effects"],
    },
    "music": {
        "guild": "producers-room",
        "tagline": ["home-studio producer", "electronic music", "session musician"],
        "represents": ["a bedroom producer", "an electronic artist", "a session player"],
        "building": ["an EP", "a sample pack", "a live set"],
        "offer": ["synth sound design", "drum programming", "session guitar",
                  "arrangement help", "sample packs"],
        "need": ["a mastering engineer", "a manager", "sync placements",
                 "collaborators", "a rehearsal space"],
        "curious": ["modular synths", "spatial audio", "AI stems"],
        "abilities": ["ableton", "logic pro", "sound design"],
    },
    "gamedev": {
        "guild": "indie-gamedev",
        "tagline": ["solo game dev", "indie studio", "gameplay engineer"],
        "represents": ["a solo game developer", "a two-person game studio",
                       "a gameplay engineer"],
        "building": ["a metroidvania", "a farming sim", "a mobile puzzler"],
        "offer": ["gameplay programming", "level design", "game feel tuning",
                  "shader work", "playtest feedback"],
        "need": ["a composer", "a marketing lead", "QA testers",
                 "a publisher intro", "UI art"],
        "curious": ["procedural content", "roguelike design", "steam wishlists"],
        "abilities": ["unity", "godot", "c-sharp"],
    },
    "graphics3d": {
        "guild": "cg-artists",
        "tagline": ["3D generalist", "environment artist", "look-dev artist"],
        "represents": ["a 3D generalist", "an environment artist", "a look-dev artist"],
        "building": ["a demo reel", "a stylized asset pack", "an archviz scene"],
        "offer": ["hard-surface modeling", "texturing", "lighting and lookdev",
                  "archviz renders", "sculpting"],
        "need": ["a rigger", "render farm credits", "a portfolio review",
                 "freelance leads", "an animator"],
        "curious": ["usd pipelines", "real-time raytracing", "houdini"],
        "abilities": ["blender", "substance", "maya"],
    },
    "startup": {
        "guild": "founders-hub",
        "tagline": ["pre-seed founder", "bootstrapping SaaS", "startup operator"],
        "represents": ["a pre-seed founder", "a bootstrapped SaaS builder",
                       "a startup operator"],
        "building": ["a B2B SaaS", "a marketplace", "a fintech app"],
        "offer": ["a validated idea", "early revenue", "operating experience",
                  "a pitch deck", "customer interviews"],
        "need": ["a first hire", "intros to VCs", "a design partner",
                 "advisors", "distribution channels"],
        "curious": ["pricing strategy", "plg", "b2b sales"],
        "abilities": ["fundraising", "product management", "sales"],
    },
    "freelance-dev": {
        "guild": "dev-freelancers",
        "tagline": ["freelance backend dev", "full-stack contractor", "devops for hire"],
        "represents": ["a freelance backend developer", "a full-stack contractor",
                       "a devops engineer for hire"],
        "building": ["a client API", "a side-project SaaS", "an open-source library"],
        "offer": ["backend development", "cloud infrastructure", "API design",
                  "code review", "database tuning"],
        "need": ["long-term contracts", "referrals", "a design partner",
                 "a subcontractor", "recurring clients"],
        "curious": ["rust", "kubernetes", "serverless"],
        "abilities": ["python", "go", "postgres"],
    },
    "research": {
        "guild": "research-net",
        "tagline": ["independent researcher", "PhD student", "applied scientist"],
        "represents": ["an independent researcher", "a PhD student",
                       "an applied scientist"],
        "building": ["a survey paper", "an open dataset", "a benchmark"],
        "offer": ["literature reviews", "experiment design", "statistical analysis",
                  "reproducible code", "peer review"],
        "need": ["a co-author", "compute credits", "study participants",
                 "grant leads", "a mentor"],
        "curious": ["causal inference", "open science", "replication"],
        "abilities": ["r", "python", "latex"],
    },
    "marketing": {
        "guild": "growth-collective",
        "tagline": ["growth marketer", "content strategist", "paid-ads specialist"],
        "represents": ["a growth marketer", "a content strategist",
                       "a paid-ads specialist"],
        "building": ["a content engine", "a newsletter", "a launch campaign"],
        "offer": ["content strategy", "paid ads management", "email funnels",
                  "landing-page copy", "analytics setup"],
        "need": ["a designer", "case studies", "a developer for tracking",
                 "referral partners", "clients"],
        "curious": ["lifecycle marketing", "attribution", "community-led growth"],
        "abilities": ["hubspot", "ga4", "copywriting"],
    },
    "art": {
        "guild": "illustration-guild",
        "tagline": ["freelance illustrator", "concept artist", "comic artist"],
        "represents": ["a freelance illustrator", "a concept artist", "a comic artist"],
        "building": ["a portfolio", "a webcomic", "a sticker line"],
        "offer": ["character illustration", "concept art", "editorial illustration",
                  "comic pages", "poster design"],
        "need": ["a writer", "print vendors", "an agent",
                 "commission leads", "a colorist"],
        "curious": ["visual development", "storyboarding", "printmaking"],
        "abilities": ["procreate", "photoshop", "clip studio"],
    },
    "fitness": {
        "guild": "coaching-circle",
        "tagline": ["personal trainer", "yoga instructor", "strength coach"],
        "represents": ["a personal trainer", "a yoga instructor", "a strength coach"],
        "building": ["an online coaching program", "a mobility course",
                     "a fitness community"],
        "offer": ["personal training", "mobility routines", "programming",
                  "form checks", "accountability"],
        "need": ["clients", "a videographer", "a nutritionist referral",
                 "a booking tool", "gym space"],
        "curious": ["habit science", "recovery", "wearables"],
        "abilities": ["programming", "coaching", "anatomy"],
    },
    "language-travel": {
        "guild": "world-exchange",
        "tagline": ["language learner", "digital nomad", "cultural exchange"],
        "represents": ["a language learner", "a digital nomad", "a cultural exchanger"],
        "building": ["conversational fluency", "a travel blog", "a phrasebook"],
        "offer": ["native French chat", "local tips for Lisbon", "translation help",
                  "couch to crash", "city walking tours"],
        "need": ["a conversation partner", "travel companions", "housing swaps",
                 "language practice", "local friends"],
        "curious": ["slow travel", "immersion", "polyglot methods"],
        "abilities": ["french", "portuguese", "teaching"],
    },
    "hobby": {
        "guild": "hobby-club",
        "tagline": ["chess enthusiast", "amateur photographer", "board-game host"],
        "represents": ["a chess enthusiast", "an amateur photographer",
                       "a board-game host"],
        "building": ["an opening repertoire", "a photo zine", "a game night"],
        "offer": ["casual games", "photography feedback", "a games library",
                  "puzzle sessions", "darkroom access"],
        "need": ["practice partners", "a photo-walk group", "players",
                 "critique", "club members"],
        "curious": ["endgames", "street photography", "euro games"],
        "abilities": ["chess", "photography", "hosting"],
    },
}


def _filler(rng):
    cards = []
    per = 12  # ~12 per domain * 12 domains = 144 filler cards
    for slug, pool in _DOMAINS.items():
        for i in range(per):
            def pick(key, lo, hi):
                items = pool[key]
                k = min(len(items), rng.randint(lo, hi))
                return rng.sample(items, k)
            card = _card(
                f"@{slug}-{i:02d}",
                rng.choice(pool["tagline"]),
                rng.choice(pool["represents"]),
                building=pick("building", 1, 2),
                offer=pick("offer", 2, 3),
                need=pick("need", 2, 3),
                curious=pick("curious", 1, 2),
                abilities=pick("abilities", 1, 2),
                guilds=[pool["guild"]],
            )
            cards.append(card)
    return cards


# --------------------------------------------------------------------------- #
# Noisy cards: empty-ish, vague, and keyword-stuffed spam.
# --------------------------------------------------------------------------- #
# Hype/scam word-salad. Deliberately DISJOINT from the precise vocabulary real
# cards use (no "growth"/"viral"/"seo"/"design") so spam reads as spam: a card
# that clears a professional bar should never be pushed aside by this. A few
# ultra-generic buzzwords are included; they are so frequent they get dropped as
# corpus stopwords, which is exactly the point.
_SPAM_WORDS = [
    "web3", "crypto", "blockchain", "nft", "dao", "metaverse", "dropshipping",
    "hustle", "grind", "moonshot", "10x", "100x", "passive-income", "monetize",
    "affiliate", "biohack", "manifest", "abundance", "mindset", "synergy",
    "leverage", "disrupt", "paradigm", "frictionless", "gamification",
    "virality", "clickbait", "ninja", "guru", "rockstar", "unicorn",
    "bleeding-edge", "thought-leader", "blitzscale", "holistic", "consulting",
    "solutions", "leverage-synergies", "onboarding", "engagement",
]


def _noise(rng):
    cards = []
    n = 0

    def add(c):
        nonlocal n
        cards.append(c)
        n += 1

    # 7 empty-ish / vague
    add(_card("@noise-empty-00", "", ""))
    add(_card("@noise-empty-01", "stuff", "someone"))
    add(_card("@noise-vague-02", "things and stuff", "a person",
              offer=["stuff"], need=["things"]))
    add(_card("@noise-vague-03", "hi", "me", offer=["misc"], curious=["everything"]))
    add(_card("@noise-vague-04", "just here", "an agent", offer=["help"], need=["help"]))
    add(_card("@noise-vague-05", "open to anything", "somebody",
              offer=["general assistance"], need=["opportunities"]))
    add(_card("@noise-vague-06", "networking", "a networker",
              offer=["connections"], need=["connections"]))

    # 13 keyword-stuffed spam (deterministic samples of the buzzword bag)
    for i in range(13):
        offer = rng.sample(_SPAM_WORDS, 14)
        need = rng.sample(_SPAM_WORDS, 14)
        curious = rng.sample(_SPAM_WORDS, 8)
        add(_card(
            f"@noise-spam-{i:02d}",
            " ".join(rng.sample(_SPAM_WORDS, 6)),
            "the #1 growth ai web3 solutions agency",
            offer=offer, need=need, curious=curious,
            abilities=rng.sample(_SPAM_WORDS, 6),
            guilds=["everything-guild"],
        ))
    return cards


# --------------------------------------------------------------------------- #
# Assembly (cached; pure — rebuilt identically from SEED on every call).
# --------------------------------------------------------------------------- #
_BUILT = None


def _build():
    global _BUILT
    if _BUILT is not None:
        return _BUILT

    rng = random.Random(SEED)
    planted = _planted()

    all_cards = []
    pairs = []            # (handleA, handleB, note)
    cross = []            # notes flagged cross-vocab
    personal = []         # notes flagged personal
    for a, b, note, is_cross, is_personal in planted:
        all_cards.append(a)
        all_cards.append(b)
        pairs.append((a["handle"], b["handle"], note))
        if is_cross:
            cross.append((a["handle"], b["handle"], note))
        if is_personal:
            personal.append((a["handle"], b["handle"], note))

    all_cards.extend(_filler(rng))
    noise = _noise(rng)
    noisy = {c["handle"] for c in noise}
    all_cards.extend(noise)

    # Deterministic presence timestamps. Real (active) cards are all freshly
    # seen so recency is held CONSTANT across them: presence is a recency
    # tiebreaker, not a quality signal, and letting random staleness reshuffle
    # ranks would corrupt the recall measurement (a stale-but-better partner
    # losing to a fresh-but-worse filler). Noisy cards are stale, which -- with
    # the engine's recency decay -- reinforces their suppression. This mirrors a
    # public launch where everyone who just joined is recently active.
    last_seen = {}
    for c in all_cards:
        h = c["handle"]
        if h in noisy:
            age = rng.randint(30, 180)      # days: stale -> presence ~0
        else:
            age = 0                          # freshly seen -> presence ~1
        last_seen[h] = NOW - age * _DAY

    # Sanity: unique handles.
    handles = [c["handle"] for c in all_cards]
    assert len(handles) == len(set(handles)), "duplicate handle in corpus"

    _BUILT = {
        "cards": all_cards,
        "pairs": pairs,
        "cross": cross,
        "personal": personal,
        "noisy": noisy,
        "last_seen": last_seen,
    }
    return _BUILT


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def cards():
    """Every public card (planted + filler + noise), deterministic order."""
    return [dict(c) for c in _build()["cards"]]


def ground_truth_pairs():
    """List of (handleA, handleB, note) planted 'should obviously meet' pairs."""
    return list(_build()["pairs"])


def cross_vocab_pairs():
    """Subset where the semantic link uses different words (token-hard)."""
    return list(_build()["cross"])


def personal_pairs():
    """Subset that is personal / non-professional (hobby, travel, exchange)."""
    return list(_build()["personal"])


def noisy_handles():
    """Handles of the spam / empty / vague cards."""
    return set(_build()["noisy"])


def last_seen_map():
    """handle -> deterministic last-seen epoch seconds (for presence scoring)."""
    return dict(_build()["last_seen"])


def stats():
    b = _build()
    n_cards = len(b["cards"])
    n_noise = len(b["noisy"])
    return {
        "total_cards": n_cards,
        "planted_pairs": len(b["pairs"]),
        "cross_vocab_pairs": len(b["cross"]),
        "personal_pairs": len(b["personal"]),
        "noisy_cards": n_noise,
        "filler_cards": n_cards - 2 * len(b["pairs"]) - n_noise,
        "domains": len(_DOMAINS),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(stats(), indent=2))
