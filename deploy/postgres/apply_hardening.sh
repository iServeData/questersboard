#!/usr/bin/env bash
# Install step 2: harden a fresh PostgreSQL cluster and create the WoWQuester roles and database.
#
#   sudo deploy/postgres/apply_hardening.sh            # apply
#   sudo deploy/postgres/apply_hardening.sh --check    # read-only verification only
#
# What it changes (and nothing else):
#   /etc/postgresql/<ver>/main/pg_hba.conf             replaced (original kept as pg_hba.conf.pre_wowquester)
#   /etc/postgresql/<ver>/main/conf.d/50-wowquester.conf   added
#   postgresql@<ver>-main                               restarted
#   roles wowquester_admin/_app/_worker/_readonly, database wowquester   created if missing
#   /root/wowquester_db/{admin,app,worker,readonly}     random passwords (0600), only until install step 4
#                                                       turns them into systemd credentials and shreds them
# Passwords never appear on a command line or in the PostgreSQL log.
#
# Undo (before any WoWQuester data exists):
#   sudo cp -p /etc/postgresql/16/main/pg_hba.conf.pre_wowquester /etc/postgresql/16/main/pg_hba.conf
#   sudo rm /etc/postgresql/16/main/conf.d/50-wowquester.conf && sudo systemctl restart postgresql@16-main
#   sudo -u postgres psql -c 'DROP DATABASE wowquester' -c 'DROP ROLE wowquester_app, wowquester_worker, wowquester_readonly, wowquester_admin'
set -euo pipefail

PGVER="${PGVER:-16}"
ETC="/etc/postgresql/$PGVER/main"
UNIT="postgresql@$PGVER-main"
HERE="$(cd "$(dirname "$0")" && pwd)"
PWDIR=/root/wowquester_db
MODE="${1:-apply}"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -d "$ETC" ] || { echo "no cluster config at $ETC (set PGVER?)" >&2; exit 1; }
psql_pg() { (cd /tmp && runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 "$@"); }

check() {
  echo "== listeners (expect only 127.0.0.1:5432 and [::1]:5432)"
  ss -ltnH | awk '$4 ~ /:5432$/ {print "  " $4}'
  echo "== settings"
  psql_pg -Atc "select '  ' || name || ' = ' || setting from pg_settings where name in
    ('listen_addresses','password_encryption','log_connections','log_disconnections','log_statement',
     'idle_in_transaction_session_timeout','ssl') order by name"
  echo "== pg_hba.conf parse errors (expect none)"
  psql_pg -Atc "select '  line ' || line_number || ': ' || error from pg_hba_file_rules where error is not null"
  echo "== superusers (expect only postgres) and roles named root (expect none)"
  psql_pg -Atc "select '  ' || rolname || case when rolsuper then ' SUPERUSER' else '' end from pg_roles
    where rolsuper or rolname = 'root' or rolname like 'wowquester%' order by 1"
  echo "== wowquester database owner and PUBLIC rights (expect wowquester_admin, no =Tc/ for PUBLIC)"
  psql_pg -Atc "select '  ' || datname || ' owner=' || pg_get_userbyid(datdba) || ' acl=' || coalesce(datacl::text,'')
    from pg_database where datname = 'wowquester'"
  echo "== postgres over TCP (expect: pg_hba.conf rejects connection)"
  (cd /tmp && runuser -u postgres -- psql -X -w -h 127.0.0.1 -U postgres -c 'select 1' 2>&1 | sed 's/^/  /' | head -2) || true
  echo "== wowquester_app login over TCP"
  if [ -s "$PWDIR/app" ]; then
    (cd /tmp && PGPASSWORD="$(cat "$PWDIR/app")" runuser -u nobody -- psql -X -w -h 127.0.0.1 -U wowquester_app \
       -d wowquester -Atc "select '  ok: ' || current_user || ' on ' || current_database()" 2>&1 | sed 's/^/  /') || true
  else
    echo "  skipped (passwords already moved to credentials)"
  fi
}

if [ "$MODE" = "--check" ]; then check; exit 0; fi

grep -Eq "^\s*include_dir\s*=\s*'conf.d'" "$ETC/postgresql.conf" \
  || { echo "$ETC/postgresql.conf has no include_dir = 'conf.d'; stopping" >&2; exit 1; }

echo "== installing configuration"
[ -e "$ETC/pg_hba.conf.pre_wowquester" ] || cp -p "$ETC/pg_hba.conf" "$ETC/pg_hba.conf.pre_wowquester"
install -o postgres -g postgres -m 0640 "$HERE/pg_hba.conf" "$ETC/pg_hba.conf"
install -o postgres -g postgres -m 0644 "$HERE/postgresql_wowquester.conf" "$ETC/conf.d/50-wowquester.conf"
systemctl restart "$UNIT"
systemctl is-active --quiet "$UNIT" || { echo "$UNIT failed to start; see journalctl -u $UNIT" >&2; exit 1; }

echo "== passwords (kept in $PWDIR until install step 4)"
install -d -m 0700 "$PWDIR"
for r in admin app worker readonly; do
  [ -s "$PWDIR/$r" ] || (umask 077; openssl rand -hex 32 > "$PWDIR/$r")
done

echo "== roles and database"
WQ_ADMIN_PW="$(cat "$PWDIR/admin")" WQ_APP_PW="$(cat "$PWDIR/app")" \
WQ_WORKER_PW="$(cat "$PWDIR/worker")" WQ_READONLY_PW="$(cat "$PWDIR/readonly")" \
  psql_pg -q -f - < "$HERE/roles.sql"
# Make the password files authoritative even if the roles already existed (rerun-safe).
WQ_ADMIN_PW="$(cat "$PWDIR/admin")" WQ_APP_PW="$(cat "$PWDIR/app")" \
WQ_WORKER_PW="$(cat "$PWDIR/worker")" WQ_READONLY_PW="$(cat "$PWDIR/readonly")" \
  psql_pg -q -f - <<'SQL'
\getenv admin_pw WQ_ADMIN_PW
\getenv app_pw WQ_APP_PW
\getenv worker_pw WQ_WORKER_PW
\getenv readonly_pw WQ_READONLY_PW
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
SET password_encryption = 'scram-sha-256';
SELECT format('ALTER ROLE wowquester_admin PASSWORD %L', :'admin_pw') \gexec
SELECT format('ALTER ROLE wowquester_app PASSWORD %L', :'app_pw') \gexec
SELECT format('ALTER ROLE wowquester_worker PASSWORD %L', :'worker_pw') \gexec
SELECT format('ALTER ROLE wowquester_readonly PASSWORD %L', :'readonly_pw') \gexec
SQL

check
echo "done. Next: install step 3 (release and units)."
