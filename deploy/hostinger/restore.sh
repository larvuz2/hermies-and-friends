#!/bin/sh
# Restore the Hermix hub database from a backup snapshot.
#
#   sh restore.sh                       # newest snapshot
#   sh restore.sh /var/backups/hermix/hermix-20260729-030000.db.gz
#
# Deliberately explicit and reversible: it stops the hub, keeps the current
# database as .pre-restore, verifies the snapshot BEFORE overwriting anything,
# and starts the hub again. Verify a restore now, not during an outage.
set -eu

DB="${HERMIX_DB:-/var/lib/hermix/hermix.db}"
DEST="${HERMIX_BACKUP_DIR:-/var/backups/hermix}"
SRC="${1:-}"

log() { echo "[hermix-restore] $*"; }
fail() { log "ABORTED: $*"; exit 1; }

[ -n "$SRC" ] || SRC="$(ls -1t "$DEST"/hermix-*.db.gz 2>/dev/null | head -n1 || true)"
[ -n "$SRC" ] || fail "no snapshot given and none found in $DEST"
[ -f "$SRC" ] || fail "no such snapshot: $SRC"
command -v sqlite3 >/dev/null 2>&1 || fail "sqlite3 not installed"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
CAND="$TMP/candidate.db"

log "unpacking $SRC"
gunzip -c "$SRC" > "$CAND" || fail "could not decompress $SRC"

# Verify the snapshot BEFORE touching the live database.
INTEGRITY="$(sqlite3 "$CAND" 'PRAGMA integrity_check;' 2>&1 || true)"
[ "$INTEGRITY" = "ok" ] || fail "snapshot failed integrity_check: $INTEGRITY"
ACCOUNTS="$(sqlite3 "$CAND" 'SELECT COUNT(*) FROM accounts;' 2>&1 || echo err)"
case "$ACCOUNTS" in
    ''|*[!0-9]*) fail "snapshot has no readable accounts table: $ACCOUNTS" ;;
esac
log "snapshot is sound: $ACCOUNTS accounts"

log "stopping the hub"
systemctl stop hermix || log "warning: could not stop hermix (continuing)"

if [ -f "$DB" ]; then
    cp "$DB" "$DB.pre-restore" || fail "could not preserve the current database"
    log "current database kept at $DB.pre-restore"
fi

# WAL/SHM belong to the old database — leaving them would corrupt the new one.
rm -f "$DB-wal" "$DB-shm"
mkdir -p "$(dirname "$DB")"
cp "$CAND" "$DB" || fail "could not install the snapshot"

log "starting the hub"
systemctl start hermix || fail "restored the database but the hub did not start"

log "done — $ACCOUNTS accounts restored from $(basename "$SRC")"
log "the hub needs ~8s to load the embedding model before /healthz answers"
