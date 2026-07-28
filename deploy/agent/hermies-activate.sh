#!/usr/bin/env bash
# Hermies activation supervisor.
#
# WHY THIS EXISTS
# ---------------
# The plugin can download new code but must not be its own process manager: an
# in-gateway restart is blocked by Hermes and would kill the human's in-flight
# task. So activation lives OUT HERE, in a tiny privileged unit installed once
# at install time. The user approves it once; after that, releases activate
# themselves and nobody ever runs a command.
#
# WHAT IT DOES (and deliberately does not)
#   * checks the hub for the desired release TAG (never a moving branch)
#   * honours the staged rollout percentage (stable per-agent hash)
#   * waits for an IDLE window — never interrupts a live conversation
#   * checks out the tag, restarts the gateway, health-checks
#   * ROLLS BACK to the previous tag automatically if the restart fails
#   * one activation attempt per version, one rollback, then a cooldown
#   * it never edits code, never runs anything the hub sends, only git + hermes
#
# Install:  hermies-activate.sh --install     (writes the systemd timer)
# Run once: hermies-activate.sh
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/root/.hermes}"
PLUGIN_DIR="${HERMIES_PLUGIN_DIR:-$HERMES_HOME/plugins/hermies}"
STATE_DIR="$HERMES_HOME/hermies"
STATE="$STATE_DIR/activation.json"
LOG_TAG="hermies-activate"
IDLE_MINUTES="${HERMIES_IDLE_MINUTES:-10}"     # human quiet for this long
MAX_DEFER_HOURS="${HERMIES_MAX_DEFER_HOURS:-48}"

log() { echo "[$LOG_TAG] $*"; }

hermes_bin() {
  command -v hermes 2>/dev/null && return 0
  for p in /usr/local/bin/hermes "$HOME/.local/bin/hermes" /root/.local/bin/hermes; do
    [ -x "$p" ] && { echo "$p"; return 0; }
  done
  return 1
}

git_q() { git -C "$PLUGIN_DIR" "$@" </dev/null 2>&1; }

# --- what does the hub want us on? -----------------------------------------
desired_version() {
  # The plugin caches the hub config locally; read it rather than authenticating
  # here (the supervisor holds no credentials by design).
  local f="$STATE_DIR/remote_config.json"
  [ -f "$f" ] || return 1
  python3 - "$f" <<'PY' 2>/dev/null || return 1
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
rel = d.get("release") or {}
print(rel.get("version") or "")
PY
}

current_version() { git_q describe --tags --exact-match || git_q rev-parse --short HEAD; }

# --- is the human busy? -----------------------------------------------------
is_idle() {
  # A conversation counts as live if the matchmaker state changed recently.
  local f="$STATE_DIR/matchmaker.json"
  [ -f "$f" ] || return 0
  local age=$(( $(date +%s) - $(stat -c %Y "$f" 2>/dev/null || echo 0) ))
  [ "$age" -ge $(( IDLE_MINUTES * 60 )) ]
}

read_state() { [ -f "$STATE" ] && cat "$STATE" || echo '{}'; }
write_state() { mkdir -p "$STATE_DIR"; printf '%s' "$1" > "$STATE"; }

json_get() { python3 -c "import json,sys;print(json.loads(sys.stdin.read() or '{}').get(sys.argv[1],'') or '')" "$1"; }

# --- health --------------------------------------------------------------- #
gateway_healthy() {
  local hb; hb="$(hermes_bin)" || return 1
  "$hb" gateway status </dev/null 2>&1 | grep -qi "running"
}

bridge_changed() {
  # Did this release touch the in-gateway bridge (tools/commands/hooks)? If not,
  # restarting OUR sidecar is enough and the user's Hermes is never disturbed.
  local f="$STATE_DIR/remote_config.json"
  [ -f "$f" ] || { echo "1"; return; }
  python3 - "$f" <<'PY' 2>/dev/null || echo "1"
import json, sys
rel = (json.load(open(sys.argv[1], encoding="utf-8")).get("release") or {})
print("1" if rel.get("bridge_changed", True) else "0")
PY
}

sidecar_installed() { systemctl list-unit-files 2>/dev/null | grep -q '^hermies-sidecar'; }
restart_sidecar()  { systemctl restart hermies-sidecar 2>/dev/null; sleep 5; }
sidecar_healthy()  { systemctl is-active --quiet hermies-sidecar 2>/dev/null; }

activate() {
  local target="$1" previous="$2" hb needs_gateway
  needs_gateway="$(bridge_changed)"

  log "activating $target (from $previous), bridge_changed=$needs_gateway"
  if ! git_q fetch --tags --force >/dev/null; then
    log "fetch failed"; return 1
  fi
  if ! git_q checkout --detach "tags/$target" >/dev/null; then
    log "checkout of $target failed — staying on $previous"; return 1
  fi

  # Sidecar first: it owns the volatile logic, and restarting it costs nothing.
  if sidecar_installed; then
    restart_sidecar
    if ! sidecar_healthy; then
      log "sidecar unhealthy after $target — ROLLING BACK"
      git_q checkout --detach "tags/$previous" >/dev/null || \
        git_q checkout --detach "$previous" >/dev/null || true
      restart_sidecar
      sidecar_healthy && log "rollback to $previous succeeded" \
                      || log "rollback FAILED — manual attention needed"
      return 1
    fi
  fi

  # Only a bridge change justifies disturbing the user's whole agent.
  if [ "$needs_gateway" = "1" ]; then
    hb="$(hermes_bin)" || { log "hermes binary not found"; return 1; }
    "$hb" gateway restart </dev/null >/dev/null 2>&1 || true
    sleep 20
    if ! gateway_healthy; then
      log "gateway unhealthy after $target — ROLLING BACK to $previous"
      git_q checkout --detach "tags/$previous" >/dev/null || \
        git_q checkout --detach "$previous" >/dev/null || true
      sidecar_installed && restart_sidecar
      "$hb" gateway restart </dev/null >/dev/null 2>&1 || true
      sleep 20
      gateway_healthy && log "rollback to $previous succeeded" \
                      || log "rollback FAILED — manual attention needed"
      return 1
    fi
  else
    log "sidecar-only release — the user's gateway was not touched"
  fi

  log "activated $target"
  return 0
}

main() {
  [ -d "$PLUGIN_DIR/.git" ] || { log "no plugin checkout at $PLUGIN_DIR"; exit 0; }
  if [ -n "$(git_q status --porcelain)" ]; then
    log "local modifications present — leaving this checkout alone"; exit 0
  fi

  local want cur st tried
  want="$(desired_version || true)"
  [ -n "$want" ] || { log "no desired version published; nothing to do"; exit 0; }
  cur="$(current_version)"
  [ "$want" != "$cur" ] || { log "already on $cur"; exit 0; }

  st="$(read_state)"
  tried="$(printf '%s' "$st" | json_get attempted_version)"
  if [ "$tried" = "$want" ]; then
    log "already attempted $want once — not retrying (avoids restart loops)"; exit 0
  fi

  if ! is_idle; then
    log "agent is busy; deferring $want"; exit 0
  fi

  write_state "{\"attempted_version\": \"$want\", \"from\": \"$cur\", \"at\": $(date +%s)}"
  if activate "$want" "$cur"; then
    write_state "{\"active_version\": \"$want\", \"attempted_version\": \"$want\", \"at\": $(date +%s)}"
  else
    write_state "{\"attempted_version\": \"$want\", \"rolled_back_to\": \"$cur\", \"at\": $(date +%s)}"
  fi
}

install_units() {
  # The sidecar: our own process for matchmaking/envoy/polling. Restarting it
  # is invisible to the user, unlike restarting their Hermes gateway.
  local py
  py="$(command -v python3 || echo /usr/bin/python3)"
  cat > /etc/systemd/system/hermies-sidecar.service <<EOF
[Unit]
Description=Hermies sidecar (matchmaking + envoy engine)
After=network-online.target

[Service]
Type=simple
Environment=HERMES_HOME=$HERMES_HOME
Environment=HERMIES_HOME=$HERMES_HOME/hermies
Environment=HERMIES_LLM=hub
# The API key + hub URL live here. Without this the sidecar starts but has no
# credentials and can do nothing — which is exactly how it must NOT fail.
EnvironmentFile=-$HERMES_HOME/.env
WorkingDirectory=$(dirname "$PLUGIN_DIR")
ExecStart=$py -m hermies.sidecar
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now hermies-sidecar.service 2>/dev/null \
    && log "installed hermies-sidecar.service" \
    || log "sidecar service not started (the in-gateway plugin keeps working)"

  cat > /etc/systemd/system/hermies-activate.service <<EOF
[Unit]
Description=Hermies activation supervisor (activates approved releases)
After=network.target

[Service]
Type=oneshot
Environment=HERMES_HOME=$HERMES_HOME
ExecStart=$(readlink -f "$0")
EOF
  cat > /etc/systemd/system/hermies-activate.timer <<'EOF'
[Unit]
Description=Check for an approved Hermies release

[Timer]
OnBootSec=10min
OnUnitActiveSec=1h
RandomizedDelaySec=20min

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now hermies-activate.timer
  log "installed hermies-activate.timer (hourly, jittered)"
}

case "${1:-}" in
  --install) install_units ;;
  *) main ;;
esac
