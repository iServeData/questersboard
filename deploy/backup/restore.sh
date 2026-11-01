#!/usr/bin/env bash
# Restore an encrypted backup. Default target is a SCRATCH database (restore drill), never production:
#   sudo restore.sh <file.tar.gpg>                       # -> wowquester_restore_check, verified, then dropped
#   sudo restore.sh <file.tar.gpg> --into-production     # stops units, recreates wowquester (approval required)
# Needs the private key in root's GnuPG keyring (import it only for the restore, remove afterwards).
# Extension entries are skipped from the dump: database_init.sql creates btree_gist as postgres.
set -euo pipefail
umask 077
FILE="${1:?usage: restore.sh <file.tar.gpg> [--into-production]}"
MODE="${2:-check}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PGH="${WOWQUESTER_PGHOST:-127.0.0.1}"; PGP="${WOWQUESTER_PGPORT:-5432}"
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
WORK="$(mktemp -d)"; chmod 0711 "$WORK"; trap 'rm -rf "$WORK"' EXIT
gpg --batch -d "$FILE" | tar -C "$WORK" -xf -
( cd "$WORK" && sha256sum -c --quiet SHA256SUMS )
ADMIN_PW="${WOWQUESTER_DB_ADMIN_PW:-$(systemd-creds decrypt --name=wowquester.db-admin-password /etc/credstore.encrypted/wowquester.db-admin-password -)}"
pg_restore -l "$WORK/db.dump" | grep -v -E ' (EXTENSION|COMMENT - EXTENSION) ' > "$WORK/list"

restore_into() {
  local db="$1"
  sudo -u postgres psql -h "${WOWQUESTER_PGSOCK:-/var/run/postgresql}" -p "$PGP" -qX -v ON_ERROR_STOP=1 -v db="$db" \
       -f "$HERE/postgres/database_init.sql"
  PGPASSWORD="$ADMIN_PW" pg_restore -h "$PGH" -p "$PGP" -U wowquester_admin -d "$db" --no-owner \
       --exit-on-error --single-transaction -L "$WORK/list" "$WORK/db.dump"
}

if [[ "$MODE" == "--into-production" ]]; then
  [[ "${WOWQUESTER_APPROVED_CHANGE:-}" == "restore-production" ]] || {
    echo "refusing: set WOWQUESTER_APPROVED_CHANGE=restore-production after approval" >&2; exit 3; }
  systemctl stop wowquester-web.socket wowquester-web wowquester-bot wowquester-worker
  echo "safety backup of the current database before replacing it:"
  "$HERE/backup/backup.sh"
  restore_into wowquester
  tar -C / -xf "$WORK/files.tar" var/lib/wowquester/uploads
  chown -R wowquester:wowquester /var/lib/wowquester/uploads
  echo "database (with grants) and uploads restored; config.toml from the backup is in the archive, not applied."
  echo "next: systemctl start wowquester-web.socket wowquester-web wowquester-bot wowquester-worker && wowquester-admin health"
else
  T=wowquester_restore_check
  restore_into "$T"
  PGPASSWORD="$ADMIN_PW" psql -h "$PGH" -p "$PGP" -U wowquester_admin -d "$T" -AtX -c \
    "SELECT 'alembic='||version_num FROM alembic_version" -c "SELECT 'events='||count(*) FROM events" \
    -c "SELECT 'chronicle_entries='||count(*) FROM chronicle_entries" \
    -c "SELECT 'app_can_select_events='||has_table_privilege('wowquester_app','events','SELECT')"
  sudo -u postgres psql -h "${WOWQUESTER_PGSOCK:-/var/run/postgresql}" -p "$PGP" -qX -c "DROP DATABASE $T WITH (FORCE)"
  echo "restore drill OK (scratch database dropped)"
fi
