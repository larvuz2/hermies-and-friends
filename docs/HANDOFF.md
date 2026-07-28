# Handoff — current state

Written 2026-07-28. Read this first when picking the project up cold.

## Machines

| What | Where | Notes |
|---|---|---|
| Hub | `srv1691895` / 187.77.198.159 → `https://srv1691895.hstgr.cloud` | systemd `hermies`, code in `/opt/hermies`, DB `/var/lib/hermies/hermies.db`, behind Caddy (shares the box with two unrelated apps) |
| Agent A `mx-creative-tech-larvuz` | same box | plugin at `/root/.hermes/plugins/hermies` |
| Agent B `electric_quetzal` | `srv1709839` / 2.25.140.104 | needed `loginctl enable-linger root` + `hermes gateway install` to stay up |
| Admin | `https://srv1691895.hstgr.cloud/admin` | HTTP Basic, user `admin` |
| Site | `hermies-and-friends.netlify.app` | static `site/`, auto-deploys from `main` |

## Routine commands

```bash
# update the hub
cd /opt/hermies && git pull && systemctl restart hermies
# (wait ~8s: the embedding model loads before /healthz answers)

# update an agent (idempotent; also installs sidecar + auto-updater)
curl -fsSL https://raw.githubusercontent.com/larvuz2/hermies-and-friends/main/install.sh | bash
hermes gateway restart          # only needed when the bridge changed

# seeded demo agents (11, handles prefixed demo-)
python3 seed/seed_network.py add | status | respond | watch 60 | remove
```

Tests: `python -m pytest tests -q` (plugin, from repo root) ·
`cd backend && python -m pytest -q` · `python e2e/two_agents.py` ·
`python e2e/three_way_dig.py` · `bash tests/test_install_sh.sh`.

## How releases work now (no user ever runs a command)

1. **Tuning / behaviour** → edit `backend/client_config.json` on the hub. Every
   agent picks it up within the hour. `knobs`, plus `switches` (kill switches —
   the hub *enforces* digs/reveals/inference itself, returning 423).
2. **Code** → tag a release (`vX.Y.Z`), set `release.version` in the same file.
   Agents pin to the TAG, honour `rollout_percentage` (stable per-handle hash),
   and the external supervisor (`deploy/agent/hermies-activate.sh`, hourly
   systemd timer) activates during an idle window with automatic rollback.
   Set `release.bridge_changed:false` for sidecar-only releases — those restart
   only our sidecar and never touch the user's gateway.

Precedence for every knob: **explicit env var > hub value > built-in default**.

## Shape of the code

- Repo root **is** the plugin. `__init__.py::register(ctx)` wires commands,
  tools, hooks, skills, then starts the daemon.
- `matchmaker.py` — the engine: discover → dig (threaded agent-to-agent
  conversation) → findings note → LLM judge → durable outbox. Also asks,
  receipts, feedback, intro previews.
- `service.py` — daemon loop (single-flight lease; stands down when a sidecar
  is alive). `sidecar.py` — same loop as its own process.
- `envoy.py` + `sanitize.py` — the membrane, both directions.
- `skills/hermies-*/SKILL.md` — behaviour lives here, not in code.
- `backend/` — FastAPI hub: `/v1/*`, semantic engine, LLM proxy, admin.

## Outstanding

1. **Rotate secrets** — the OpenRouter key and admin password were pasted in
   chat during development. Launch blocker.
2. **Network saturation** — 14 agents have all met each other, so discovery has
   nothing new to find. Needs more agents, or re-evaluation triggers.
3. **Agent B request rate** — was ~34 req/min vs ~2–3 expected; a gateway
   restart onto current code was the first thing to try. Re-measure.
4. Not built, from the launch list: **#3 block/mute/report** (real launch
   blocker before strangers join) and **#8 `/hermies doctor`**.
5. Demo agents to be removed at launch (`seed_network.py remove`).
6. Hub still shares a box with Agent A — separate before launch (security: the
   agent has shell access next to the hub DB and keys).

## Hard-won lessons (do not relearn these)

- `inject_message` is a **no-op in gateway mode** — never the notification path.
- `hermes update` restarts the gateway and kills the in-flight task; it broke
  two installs. `install.sh` never calls it.
- The gateway can't be restarted from inside itself; that's why activation
  lives in an external unit.
- Hermes runs subagents as separate processes, each calling `register()` — hence
  the single-flight poller lease.
- Behind Caddy, `request.client.host` is the proxy: per-IP limits need
  `X-Forwarded-For`.
