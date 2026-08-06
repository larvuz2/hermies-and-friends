#!/usr/bin/env bash
# Hermix hub — roll the SERVICE back to a known revision.
#
#   bash /opt/hermix/deploy/hostinger/rollback.sh <git-sha>
#   bash /opt/hermix/deploy/hostinger/rollback.sh            # -> previous commit
#
# deploy.sh is availability-first: it restarts before it checks, and never
# reverts on its own (see the header there for why). This is the explicit,
# tested path for when you have decided the new CODE is the problem.
#
# What this does NOT touch: the database. /var/lib/hermix is left exactly as it
# is, because rolling code back is reversible and losing accounts is not. If a
# release migrated the schema forward, going back may not be safe — check the
# release notes, and restore from backup.sh instead if it is not.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/hermix}"
PORT="${PORT:-8787}"
TARGET="${1:-}"

if [ ! -d "$APP_DIR/.git" ]; then
  echo "!! $APP_DIR is not a git checkout — nothing to roll back." >&2
  echo "!! (Container deploy? Redeploy the previous image tag instead.)" >&2
  exit 2
fi

CURRENT="$(git -C "$APP_DIR" rev-parse HEAD)"
if [ -z "$TARGET" ]; then
  TARGET="$(git -C "$APP_DIR" rev-parse HEAD~1)"
  echo "==> No revision given; using the previous commit."
fi

if ! git -C "$APP_DIR" cat-file -e "${TARGET}^{commit}" 2>/dev/null; then
  echo "!! '$TARGET' is not a commit in $APP_DIR." >&2
  echo "!! Try: git -C $APP_DIR log --oneline -10" >&2
  exit 2
fi

TARGET_FULL="$(git -C "$APP_DIR" rev-parse "$TARGET")"
if [ "$TARGET_FULL" = "$CURRENT" ]; then
  echo "==> Already at ${CURRENT:0:7}. Nothing to do."
  exit 0
fi

echo "==> Rolling back"
echo "      from  ${CURRENT:0:7}  $(git -C "$APP_DIR" log -1 --format=%s "$CURRENT")"
echo "      to    ${TARGET_FULL:0:7}  $(git -C "$APP_DIR" log -1 --format=%s "$TARGET_FULL")"
echo

git -C "$APP_DIR" checkout --quiet --detach "$TARGET_FULL"

# A rolled-back revision may want different (usually older) dependencies.
echo "==> Reinstalling dependencies for that revision"
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"

echo "==> Restarting service"
systemctl restart hermix

echo "==> Verifying the rollback actually took effect"
if "$APP_DIR/venv/bin/python" "$APP_DIR/deploy/hostinger/smoke.py" \
     "http://127.0.0.1:$PORT"; then
  echo
  echo "==> Rollback complete and healthy: now serving ${TARGET_FULL:0:7}"
  echo "    To return to the tip later:  git -C $APP_DIR checkout main && bash $APP_DIR/deploy/hostinger/deploy.sh"
else
  status=$?
  echo
  echo "!! The service is now on ${TARGET_FULL:0:7}, but the product check STILL"
  echo "!! fails. That is strong evidence the problem is not the code —"
  echo "!! most likely the embedding model cannot be downloaded on this box."
  echo "!! Prewarm it:"
  echo "!!   sudo -u hermix $APP_DIR/venv/bin/python -c \\"
  echo "!!     \"from fastembed import TextEmbedding; TextEmbedding()\""
  echo "!!   systemctl restart hermix"
  exit "$status"
fi
