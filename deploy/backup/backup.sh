#!/usr/bin/env bash
# Encrypted backup: database (pg_dump custom format) + uploads + config, encrypted with GnuPG to a
# public key. Only the PUBLIC key lives on the host, so a host compromise can't read old backups.
#   sudo /opt/questersboard/current/deploy/backup/backup.sh [outdir]
# Recipient fingerprint: /etc/questersboard/backup_recipient (one line). Credentials are NOT included
# (they're host-key encrypted and useless elsewhere); keep an offline copy per docs/backup_restore.md.
set -euo pipefail
umask 077
OUT="${1:-/var/backups/questersboard}"
ETC="${QUESTERSBOARD_ETC:-/etc/questersboard}"
STATE="${QUESTERSBOARD_STATE:-/var/lib/questersboard}"
DB=questersboard
KEEP_DAYS="${QUESTERSBOARD_BACKUP_KEEP_DAYS:-${WOWQUESTER_BACKUP_KEEP_DAYS:-30}}"
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
RCPT="$(tr -d '[:space:]' < "$ETC/backup_recipient")"
[[ "$RCPT" =~ ^[0-9A-F]{40}$ ]] || { echo "backup_recipient must be a 40-hex GnuPG fingerprint" >&2; exit 78; }
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
PGPASSWORD="${QUESTERSBOARD_DB_ADMIN_PW:-$(systemd-creds decrypt --name=questersboard.db-admin-password /etc/credstore.encrypted/questersboard.db-admin-password -)}" \
  pg_dump -h "${QUESTERSBOARD_PGHOST:-127.0.0.1}" -p "${QUESTERSBOARD_PGPORT:-5432}" -U questersboard_admin -d "$DB" -Fc --no-owner -f "$WORK/db.dump"
tar -C / -cf "$WORK/files.tar" "${ETC#/}/config.toml" "${STATE#/}/uploads"
sha256sum "$WORK/db.dump" "$WORK/files.tar" | sed "s#$WORK/##" > "$WORK/SHA256SUMS"
tar -C "$WORK" -cf - db.dump files.tar SHA256SUMS \
  | gpg --batch --yes --trust-model always --homedir "$ETC/backup_gnupg" -r "$RCPT" -e -o "$OUT/questersboard_$STAMP.tar.gpg"
echo "wrote $OUT/questersboard_$STAMP.tar.gpg"
find "$OUT" -maxdepth 1 -name 'questersboard_*.tar.gpg' -mtime +"$KEEP_DAYS" -print -delete
