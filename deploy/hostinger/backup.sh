#!/bin/sh
# Hermix hub backup — the database is the ONLY copy of every account key,
# handle and card. Losing it means every user loses their identity with no
# recovery path, so this runs daily and verifies what it wrote.
#
# Installed by deploy.sh to /etc/cron.daily/hermix-backup (run as root).
# Manual run:  sh /opt/hermix/deploy/hostinger/backup.sh
#
# Why `sqlite3 .backup` and not `cp`: the hub runs in WAL mode and is always
# live. Copying the file mid-transaction yields a torn database that restores
# to garbage. `.backup` takes a consistent snapshot of a running database.
set -eu

DB="${HERMIX_DB:-/var/lib/hermix/hermix.db}"
DEST="${HERMIX_BACKUP_DIR:-/var/backups/hermix}"
KEEP="${HERMIX_BACKUP_KEEP:-14}"          # daily snapshots to retain

log() { echo "[hermix-backup] $*"; }
fail() { log "FAILED: $*"; exit 1; }

[ -f "$DB" ] || fail "no database at $DB"
command -v sqlite3 >/dev/null 2>&1 || fail "sqlite3 not installed (apt install sqlite3)"

mkdir -p "$DEST"
chmod 700 "$DEST"                          # contains API key hashes — not world-readable

STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$DEST/hermix-$STAMP.db"

sqlite3 "$DB" ".backup '$OUT'" || fail "sqlite3 .backup returned non-zero"
[ -s "$OUT" ] || fail "backup file is empty"

# A backup you have not verified is a hope, not a backup. Check the snapshot
# opens, passes an integrity check, and actually contains accounts.
INTEGRITY="$(sqlite3 "$OUT" 'PRAGMA integrity_check;' 2>&1 || true)"
[ "$INTEGRITY" = "ok" ] || fail "integrity_check said: $INTEGRITY"

ACCOUNTS="$(sqlite3 "$OUT" 'SELECT COUNT(*) FROM accounts;' 2>&1 || echo "err")"
case "$ACCOUNTS" in
    ''|*[!0-9]*) fail "cannot count accounts in the snapshot: $ACCOUNTS" ;;
esac

gzip -f "$OUT"
SIZE="$(du -h "$OUT.gz" | cut -f1)"
log "ok: $OUT.gz ($SIZE, $ACCOUNTS accounts)"

# Rotate: keep the newest $KEEP, drop the rest.
ls -1t "$DEST"/hermix-*.db.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    rm -f "$old" && log "rotated out $(basename "$old")"
done

# Fail loudly if the newest backup is stale — a cron job that silently stopped
# is indistinguishable from having no backups at all.
NEWEST="$(ls -1t "$DEST"/hermix-*.db.gz 2>/dev/null | head -n1 || true)"
[ -n "$NEWEST" ] || fail "no backups present after a successful run (?)"
log "retaining $(ls -1 "$DEST"/hermix-*.db.gz 2>/dev/null | wc -l) snapshot(s) in $DEST"
