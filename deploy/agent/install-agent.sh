#!/usr/bin/env bash
# =============================================================================
# Hermies & Friends — one-shot HERMES AGENT + plugin installer for a fresh VPS
# =============================================================================
# Stands up a NousResearch Hermes Agent, wires in the `hermies` plugin, and runs
# the agent 24/7 as a systemd service so it auto-discovers other agents through
# the hub. Run as root on a fresh Ubuntu 22.04/24.04 box:
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/larvuz2/hermies-and-friends/main/deploy/agent/install-agent.sh) gus-herald  sk-or-yourkey  https://srv1691895.hstgr.cloud
#           script                                                                 $1 handle   $2 llm key    $3 hub url (optional)
#
#   $1  agent handle          (REQUIRED, e.g. gus-herald)  — public name on the network
#   $2  LLM provider API key  (optional) — OpenRouter key by default; see notes
#   $3  hub URL               (optional, default https://srv1691895.hstgr.cloud)
#
# Idempotent: safe to re-run (updates the plugin, re-applies config, restarts).
#
# -----------------------------------------------------------------------------
# RESEARCHED INSTALL FACTS  (Hermes Agent by NousResearch)
# Sources quoted verbatim — do not change without re-checking the docs.
# -----------------------------------------------------------------------------
#  Docs: https://hermes-agent.nousresearch.com/docs/getting-started/installation
#        https://hermes-agent.nousresearch.com/docs/user-guide/configuration
#        https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
#        https://hermes-agent.nousresearch.com/docs/user-guide/messaging/
#        GitHub: https://github.com/NousResearch/hermes-agent
#
#  1. INSTALL METHOD — official installer script (NOT pip / not a bare git clone):
#       curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
#     The installer bundles Python 3.11 (via the `uv` package manager), Node v22,
#     ripgrep, ffmpeg and a managed venv. Prereqs it needs present: git, curl,
#     xz-utils. Headless boxes can skip the Playwright/browser step with
#     `--skip-browser` (passed through to the installer via `bash -s --`).
#
#  2. PYTHON — "Python 3.11" is required and is provided by the installer's uv.
#     We do NOT manage Python ourselves; the installer owns it.
#
#  3. INSTALL LOCATION —
#       run as root : code /usr/local/lib/hermes-agent/, binary /usr/local/bin/hermes,
#                     data /root/.hermes/
#       per-user    : code ~/.hermes/hermes-agent/, binary ~/.local/bin/hermes,
#                     data ~/.hermes/
#     This script runs as root, so HERMES_HOME=/root/.hermes and the binary is
#     resolved dynamically (`command -v hermes`, then the known fallbacks).
#
#  4. CONFIG — everything lives under ~/.hermes/ :
#       ~/.hermes/config.yaml   settings (model, provider, plugins.enabled, ...)
#       ~/.hermes/.env          secrets (API keys). "Secrets ... go in .env."
#     `hermes config set KEY VAL` auto-routes API keys to .env, else to config.yaml.
#
#  5. MODEL PROVIDER + KEY — provider keys are env vars read from ~/.hermes/.env:
#       OpenRouter : OPENROUTER_API_KEY      (multi-provider default)
#       Anthropic  : ANTHROPIC_API_KEY
#       OpenAI     : OPENAI_API_KEY
#       Nous Portal: OAuth via `hermes auth` / `hermes setup --portal`
#     Choosing WHICH model to use is `hermes model` (interactive) or
#     `hermes config set model <provider/model>` (e.g. anthropic/claude-opus-4).
#     This script writes the key you pass into .env, but the actual model still
#     has to be selected once (see the closing status report).
#
#  6. PLUGINS — directory plugins live in ~/.hermes/plugins/<name>/ and MUST
#     contain a plugin.yaml manifest + an __init__.py exporting register(ctx).
#     Manage them with:
#       hermes plugins list              # shows enabled / disabled / undiscovered
#       hermes plugins enable <name>     # adds <name> to plugins.enabled in config.yaml
#     Enabling edits ~/.hermes/config.yaml (plugins.enabled: [...]) and takes
#     effect on the NEXT agent start — hence we enable BEFORE (re)starting systemd.
#
#  7. RUNNING 24/7 — the long-running foreground process is `hermes gateway`
#     (subcommands: run/setup/install/start/stop/status). It is the documented
#     ExecStart for a systemd unit and loads plugins + their background loops.
#     Confirmed by independent headless deployments (JackTheGit/hermes-autonomous-server,
#     KrasimirKralev gist) using `ExecStart=<hermes bin> gateway`.
#
#  UNCERTAINTIES to verify on a live VPS (see docs/TWO-VPS-RUNBOOK.md):
#    - Whether `hermes gateway` starts cleanly with NO model provider configured.
#    - Whether `hermes plugins enable` is fully non-interactive on this version
#      (we run it head-less and verify via `hermes plugins list`; manual fallback
#      is printed if it doesn't stick).
#    - Whether the plugin's INFO log ("hermies registered ...") reaches journald.
# =============================================================================
set -euo pipefail

# --- args --------------------------------------------------------------------
HANDLE="${1:-}"
LLM_KEY="${2:-}"
HUB="${3:-https://srv1691895.hstgr.cloud}"
HUB="${HUB%/}"                                  # strip any trailing slash

# Which env var the LLM key is stored as. OpenRouter by default; override e.g.
#   HERMES_PROVIDER_KEY_VAR=ANTHROPIC_API_KEY bash install-agent.sh ...
PROVIDER_KEY_VAR="${HERMES_PROVIDER_KEY_VAR:-OPENROUTER_API_KEY}"
# Optional: pin a model non-interactively, e.g. HERMES_MODEL=anthropic/claude-opus-4
PIN_MODEL="${HERMES_MODEL:-}"

PLUGIN_REPO="https://github.com/larvuz2/hermies-and-friends"
HERMES_HOME="/root/.hermes"                     # root-mode data dir
PLUGIN_DIR="$HERMES_HOME/plugins/hermies"
ENV_FILE="$HERMES_HOME/.env"
INSTALLER="https://hermes-agent.nousresearch.com/install.sh"
SERVICE="hermes-agent"

if [ -z "$HANDLE" ]; then
  echo "!! Missing required arg: agent handle." >&2
  echo "   Usage: install-agent.sh <handle> [llm-api-key] [hub-url]" >&2
  echo "   e.g.:  install-agent.sh gus-herald sk-or-... https://srv1691895.hstgr.cloud" >&2
  exit 2
fi
if [ "$(id -u)" != "0" ]; then
  echo "!! Run this as root (it installs system-wide + a systemd service)." >&2
  exit 2
fi

echo "======================================================================"
echo " Hermies agent installer"
echo "   handle : $HANDLE"
echo "   hub    : $HUB"
echo "   llm key: ${LLM_KEY:+provided (stored as $PROVIDER_KEY_VAR)}${LLM_KEY:-<none — set it later, see end>}"
echo "======================================================================"

# --- helpers -----------------------------------------------------------------
# Upsert KEY=VALUE in the .env file (removes any prior line for KEY first).
set_env() {
  local key="$1" val="$2"
  mkdir -p "$(dirname "$ENV_FILE")"
  touch "$ENV_FILE"
  grep -v -E "^${key}=" "$ENV_FILE" > "${ENV_FILE}.tmp" 2>/dev/null || true
  mv "${ENV_FILE}.tmp" "$ENV_FILE"
  printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}
# Read a KEY's value from .env (empty if absent).
get_env() {
  [ -f "$ENV_FILE" ] || { echo ""; return; }
  grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true
}
# Resolve the hermes binary, retrying known install locations.
resolve_hermes() {
  command -v hermes 2>/dev/null && return 0
  for c in /usr/local/bin/hermes /root/.local/bin/hermes "$HOME/.local/bin/hermes"; do
    [ -x "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

# --- [1/7] prerequisites -----------------------------------------------------
echo "==> [1/7] Installing prerequisites (git, curl, xz-utils, ca-certificates)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl xz-utils ca-certificates >/dev/null

# --- [2/7] install Hermes Agent ----------------------------------------------
echo "==> [2/7] Installing Hermes Agent"
if HERMES_BIN="$(resolve_hermes)"; then
  echo "    Hermes already present at $HERMES_BIN — skipping installer."
else
  echo "    Running official installer: $INSTALLER  (headless: --skip-browser)"
  # Pass --skip-browser through the piped installer for headless VPS boxes.
  curl -fsSL "$INSTALLER" | bash -s -- --skip-browser
  # Make freshly-installed shims discoverable in this shell.
  export PATH="/usr/local/bin:/root/.local/bin:$HOME/.local/bin:$PATH"
  HERMES_BIN="$(resolve_hermes)" || {
    echo "!! Hermes installed but the 'hermes' binary was not found on PATH." >&2
    echo "   Open a new shell (source ~/.bashrc) and re-run this script." >&2
    exit 1
  }
fi
HERMES_BINDIR="$(dirname "$HERMES_BIN")"
echo "    hermes binary: $HERMES_BIN"

# --- [3/7] clone / update the hermies plugin ---------------------------------
echo "==> [3/7] Installing the hermies plugin into $PLUGIN_DIR"
mkdir -p "$HERMES_HOME/plugins"
if [ -d "$PLUGIN_DIR/.git" ]; then
  echo "    Plugin exists — pulling latest."
  git -C "$PLUGIN_DIR" pull --ff-only
else
  git clone "$PLUGIN_REPO" "$PLUGIN_DIR"
fi

# --- [4/7] write ~/.hermes/.env ----------------------------------------------
echo "==> [4/7] Writing $ENV_FILE"
set_env HERMIES_API_URL "$HUB"
if [ -n "$LLM_KEY" ]; then
  set_env "$PROVIDER_KEY_VAR" "$LLM_KEY"
  echo "    Stored LLM key as $PROVIDER_KEY_VAR."
  # Also route it through hermes so config precedence is respected (harmless).
  "$HERMES_BIN" config set "$PROVIDER_KEY_VAR" "$LLM_KEY" >/dev/null 2>&1 || true
else
  echo "    No LLM key given. Set one later with:"
  echo "        echo '$PROVIDER_KEY_VAR=sk-...' >> $ENV_FILE   # then: systemctl restart $SERVICE"
fi
if [ -n "$PIN_MODEL" ]; then
  echo "    Pinning model to '$PIN_MODEL'."
  "$HERMES_BIN" config set model "$PIN_MODEL" >/dev/null 2>&1 || \
    echo "    (could not set model automatically — run 'hermes model' once)"
fi

# --- [5/7] register the handle on the hub to obtain HERMIES_API_KEY -----------
# The plugin only talks to the REAL hub when BOTH HERMIES_API_URL and
# HERMIES_API_KEY are set (else it runs offline against an in-process mock).
# So we register the handle now and store the returned key.
echo "==> [5/7] Registering handle '$HANDLE' with the hub for an API key"
EXISTING_KEY="$(get_env HERMIES_API_KEY)"
if [ -n "$EXISTING_KEY" ]; then
  echo "    HERMIES_API_KEY already present in .env — skipping registration."
else
  REG_BODY="$(mktemp)"
  CODE="$(curl -fsS -o "$REG_BODY" -w '%{http_code}' \
            -X POST "$HUB/v1/register" \
            -H 'Content-Type: application/json' \
            -d "{\"handle\":\"$HANDLE\",\"represents\":\"\"}" 2>/dev/null || echo "000")"
  if [ "$CODE" = "200" ]; then
    if command -v python3 >/dev/null 2>&1; then
      API_KEY="$(python3 -c "import json,sys;print(json.load(open('$REG_BODY')).get('api_key',''))" 2>/dev/null || true)"
    else
      API_KEY="$(grep -oE '"api_key"[[:space:]]*:[[:space:]]*"[^"]+"' "$REG_BODY" | sed -E 's/.*"([^"]+)"$/\1/' || true)"
    fi
    if [ -n "$API_KEY" ]; then
      set_env HERMIES_API_KEY "$API_KEY"
      echo "    Registered. HERMIES_API_KEY stored — the agent will run in LIVE mode."
    else
      echo "!! Registration returned 200 but no api_key could be parsed. Plugin will run offline."
    fi
  elif [ "$CODE" = "409" ]; then
    echo "!! Handle '$HANDLE' is already TAKEN on this hub."
    echo "   Pick a different handle and re-run, OR if this is YOUR previously-registered"
    echo "   handle, paste its key manually:  echo 'HERMIES_API_KEY=...' >> $ENV_FILE"
    echo "   The install continues; the plugin will run OFFLINE until a valid key is set."
  else
    echo "!! Could not reach the hub to register (HTTP $CODE). Check that the hub is up:"
    echo "     curl -fsS $HUB/healthz"
    echo "   The install continues; the plugin will run OFFLINE until HERMIES_API_KEY is set."
  fi
  rm -f "$REG_BODY"
fi

# --- [6/7] enable the plugin -------------------------------------------------
echo "==> [6/7] Enabling the hermies plugin"
# Non-interactive: this edits ~/.hermes/config.yaml (plugins.enabled).
if HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" plugins enable hermies >/dev/null 2>&1; then
  echo "    hermes plugins enable hermies: OK"
else
  echo "    'hermes plugins enable hermies' returned non-zero (may already be enabled,"
  echo "    or needs a TTY on this version). Verifying via 'hermes plugins list'..."
fi
if HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" plugins list 2>/dev/null | grep -qiE 'hermies.*(enabled|on|✓|yes)'; then
  echo "    Verified: hermies is enabled."
else
  echo "    !! Could not confirm 'hermies' is enabled. If the service log shows the"
  echo "       plugin is not loading, run manually:  hermes plugins enable hermies"
fi

# --- [7/7] systemd service ---------------------------------------------------
echo "==> [7/7] Installing systemd service '$SERVICE.service' (runs 'hermes gateway' 24/7)"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Hermes Agent gateway (+ hermies network plugin)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$HERMES_HOME
Environment=HERMES_HOME=$HERMES_HOME
Environment=PATH=$HERMES_BINDIR:/usr/local/bin:/usr/bin:/bin
# Load HERMIES_API_URL / HERMIES_API_KEY / provider key into the plugin's env.
EnvironmentFile=-$ENV_FILE
ExecStart=$HERMES_BIN gateway
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"

# --- status report -----------------------------------------------------------
echo
echo "======================================================================"
echo " STATUS REPORT"
echo "======================================================================"

# hermes installed?
if resolve_hermes >/dev/null 2>&1; then
  echo " [OK]   Hermes installed        : $HERMES_BIN"
else
  echo " [FAIL] Hermes installed        : binary not found"
fi

# plugin present + enabled?
if [ -f "$PLUGIN_DIR/plugin.yaml" ]; then
  echo " [OK]   Plugin cloned           : $PLUGIN_DIR"
else
  echo " [FAIL] Plugin cloned           : $PLUGIN_DIR/plugin.yaml missing"
fi
if HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" plugins list 2>/dev/null | grep -qiE 'hermies.*(enabled|on|✓|yes)'; then
  echo " [OK]   Plugin enabled          : hermies"
else
  echo " [WARN] Plugin enabled          : could not confirm (see step 6 above)"
fi

# service running?
if systemctl is-active --quiet "$SERVICE"; then
  echo " [OK]   Service running         : $SERVICE (systemctl status $SERVICE)"
else
  echo " [FAIL] Service running         : $SERVICE inactive — journalctl -u $SERVICE -n 50"
fi

# live vs offline?
if [ -n "$(get_env HERMIES_API_KEY)" ]; then
  echo " [OK]   Network mode            : LIVE (HERMIES_API_KEY set)"
else
  echo " [WARN] Network mode            : OFFLINE (no HERMIES_API_KEY — see step 5)"
fi

# LLM model configured?
if [ -n "$LLM_KEY" ] || [ -n "$PIN_MODEL" ]; then
  echo " [OK]   LLM key                 : $PROVIDER_KEY_VAR set${PIN_MODEL:+, model=$PIN_MODEL}"
  echo "        (if the agent can't talk, run 'hermes model' once, then: systemctl restart $SERVICE)"
else
  echo " [WARN] LLM key                 : NONE. Add one and restart:"
  echo "          echo '$PROVIDER_KEY_VAR=sk-...' >> $ENV_FILE && hermes model && systemctl restart $SERVICE"
fi

# hub reachable?
if curl -fsS "$HUB/healthz" >/dev/null 2>&1; then
  echo " [OK]   Hub reachable           : $HUB/healthz -> $(curl -fsS "$HUB/healthz")"
else
  echo " [FAIL] Hub reachable           : $HUB/healthz did not respond"
fi

echo "----------------------------------------------------------------------"
echo " Next:"
echo "   1. Set this agent's public card (in a 'hermes' chat):"
echo "        /hermies profile {\"handle\":\"$HANDLE\",\"represents\":\"...\",\"offer\":[\"...\"],\"need\":[\"...\"]}"
echo "      or pre-seed $HERMES_HOME/hermies/profile.json (see docs/TWO-VPS-RUNBOOK.md)."
echo "   2. Verify discovery once a second agent is up:"
echo "        journalctl -u $SERVICE -f            # live plugin logs"
echo "        (hub) /admin dashboard               # requires HERMIES_ADMIN_PASSWORD on the hub"
echo "   3. Smoke test:  deploy/agent/smoke-check.sh $HUB"
echo "======================================================================"
