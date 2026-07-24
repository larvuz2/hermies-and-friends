#!/usr/bin/env bash
# =============================================================================
# Hermies agent — post-install smoke check
# =============================================================================
# Run AFTER install-agent.sh. Verifies the systemd service is up, that the
# hermies plugin registered, and that the hub is reachable. Prints PASS/FAIL per
# check and exits non-zero if any hard check fails.
#
#   deploy/agent/smoke-check.sh [hub-url]
#   (hub-url defaults to https://srv1691895.hstgr.cloud, or HERMIES_API_URL from
#    /root/.hermes/.env if present)
# =============================================================================
set -uo pipefail   # NOT -e: we want to run every check and tally results

SERVICE="hermes-agent"
ENV_FILE="/root/.hermes/.env"

# Resolve hub URL: arg > .env > default.
HUB="${1:-}"
if [ -z "$HUB" ] && [ -f "$ENV_FILE" ]; then
  HUB="$(grep -E '^HERMIES_API_URL=' "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
fi
HUB="${HUB:-https://srv1691895.hstgr.cloud}"
HUB="${HUB%/}"

fails=0
pass() { echo " [PASS] $1"; }
fail() { echo " [FAIL] $1"; fails=$((fails + 1)); }
warn() { echo " [WARN] $1"; }

echo "======================================================================"
echo " Hermies agent smoke check   (service=$SERVICE  hub=$HUB)"
echo "======================================================================"

# --- 1. systemd service active ----------------------------------------------
if systemctl is-active --quiet "$SERVICE"; then
  pass "systemd service '$SERVICE' is active"
else
  fail "systemd service '$SERVICE' is NOT active  (journalctl -u $SERVICE -n 50)"
fi

# --- 2. plugin registration line in the recent journal ----------------------
# The plugin logs 'hermies registered (live|offline/mock) for handle=...' on load.
JOURNAL="$(journalctl -u "$SERVICE" --since '-10 min' --no-pager 2>/dev/null || true)"
if echo "$JOURNAL" | grep -qi 'hermies registered'; then
  LINE="$(echo "$JOURNAL" | grep -i 'hermies registered' | tail -n1 | sed 's/^[[:space:]]*//')"
  pass "plugin registered  ->  $LINE"
elif echo "$JOURNAL" | grep -qi 'hermies'; then
  warn "no 'hermies registered' line, but 'hermies' appears in the log:"
  echo "$JOURNAL" | grep -i 'hermies' | tail -n3 | sed 's/^/        /'
else
  fail "no 'hermies' log line in the last 10 min — plugin may not be loading"
  echo "        Check:  hermes plugins list   and   journalctl -u $SERVICE -n 80"
fi

# --- 3. hub /healthz ---------------------------------------------------------
HEALTH="$(curl -fsS "$HUB/healthz" 2>/dev/null || true)"
if echo "$HEALTH" | grep -q '"ok":true'; then
  pass "hub reachable      ->  $HEALTH"
else
  fail "hub /healthz did not return ok:true  (got: '${HEALTH:-<no response>}')"
fi

echo "----------------------------------------------------------------------"
if [ "$fails" -eq 0 ]; then
  echo " RESULT: ALL CHECKS PASSED"
  exit 0
else
  echo " RESULT: $fails CHECK(S) FAILED"
  exit 1
fi
