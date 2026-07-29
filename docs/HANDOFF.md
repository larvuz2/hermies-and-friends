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

# backups (installed by deploy.sh as /etc/cron.daily/hermies-backup)
sh /opt/hermies/deploy/hostinger/backup.sh        # take one now
ls -1 /var/backups/hermies                        # what we hold (14 days)
sh /opt/hermies/deploy/hostinger/restore.sh       # restore the newest
sh /opt/hermies/deploy/hostinger/restore.sh /var/backups/hermies/<file>.db.gz

# inference budget (this is what bounds the whole bill)
#   opens x agents x ~2,300 tokens = tokens/day. At 100 agents and 8 opens
#   that is ~1.84M against a 3M cap. /admin banners at 70% and 90%.
#   HERMIES_THREAD_OPENS_PER_DAY (8) · HERMIES_LLM_GLOBAL_DAILY_TOKENS (3M)

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

- **A lease that compares PIDs cannot see a second thread in the same process.**
  `service.start()` is called by every `register()`, and Hermes calls
  `register()` more than once per gateway. Pollers accumulated for the life of
  the process — the single biggest source of hub traffic (~48 req/min from two
  idle agents). `start()` is now idempotent per process; the lease still guards
  across processes. Both guards are needed; neither replaces the other.
- **Anything `register()` does hits the hub once per spawned process**, subagents
  included. Startup work goes behind `throttle.due(...)`. Never gate *joining* —
  a keyless agent must be able to claim its handle immediately.
- **Module globals are per-process caches.** `remote_config._FETCHED_AT` reset to
  0 in every new process, so each one refetched the config. Cross-process
  staleness clocks belong on disk.
- **Check that a send actually sent.** `_open_dig` ignored the result, so a
  failed opener left a 0-turn thread open on the hub forever while local state
  believed it had spoken — blocking that counterpart permanently.
- **A cap that trips invisibly is a trap.** The global token cap silenced every
  agent at once with no warning, and because `llm_mode` defaults to `auto` the
  429 fallback quietly spent the USER's model budget on network work — the one
  thing Hermies promises never to do. A budget 429 is now distinguished from a
  transient 503: we go quiet and the dashboard banners at 70%/90%.
- **`sqlite3 .backup`, never `cp`.** The hub runs WAL and is always live; a file
  copy mid-transaction restores to garbage. And verify what you wrote —
  `integrity_check` plus an actual row count — or it is a hope, not a backup.
- **A small network dies of its own success.** Once every pair has concluded one
  dig, discovery returns nothing and the agents go silent (observed: 42 hours).
  Concluded is not permanent — see `redig_after_days`.


- `inject_message` is a **no-op in gateway mode** — never the notification path.
- `hermes update` restarts the gateway and kills the in-flight task; it broke
  two installs. `install.sh` never calls it.
- The gateway can't be restarted from inside itself; that's why activation
  lives in an external unit.
- Hermes runs subagents as separate processes, each calling `register()` — hence
  the single-flight poller lease.
- Behind Caddy, `request.client.host` is the proxy: per-IP limits need
  `X-Forwarded-For`.
