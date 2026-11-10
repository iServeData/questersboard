#!/usr/bin/env bash
# One-time host rename from the old internal name (wowquester) to questersboard.
# Public URLs (/wow/quester/...) do not change. Run from the repo root, as root, services STOPPED:
#   sudo ./deploy/rename_host.sh                    # dry run: print every step and the config.toml diff
#   sudo ./deploy/rename_host.sh --apply            # database, roles, pg_hba, account, folders, credentials,
#                                                   # units, wrapper, then ./deploy/install.sh (does not start)
#   sudo ./deploy/rename_host.sh --nginx            # dry run of the Nginx step (separate approval)
#   sudo ./deploy/rename_host.sh --nginx --apply    # new snippet + include line, nginx -t, reload
#   sudo ./deploy/rename_host.sh --rollback         # undo everything above (old release is still in /opt/wowquester)
# Saved state for rollback: /root/questersboard_rename/ (old units, wrapper, pg_hba, nginx site file).
# Old credential files and /opt/wowquester are left in place; remove them after the rename has run for a while.
set -euo pipefail

OLD=wowquester
NEW=questersboard
ROLES=(admin app worker readonly)
OLD_UNITS=(wowquester-web.socket wowquester-web.service wowquester-bot.service wowquester-worker.service
           wowquester-backup.service wowquester-backup.timer)
CREDS=/etc/credstore.encrypted
STATE=/root/questersboard_rename
PGETC="${QUESTERSBOARD_PGETC:-$(ls -d /etc/postgresql/*/main 2>/dev/null | sort -V | tail -1)}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

MODE=rename APPLY=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --nginx) MODE=nginx ;;
    --rollback) MODE=rollback; APPLY=1 ;;
    *) echo "usage: $0 [--nginx] [--apply] | --rollback" >&2; exit 2 ;;
  esac
done

say() { echo "== $*"; }
run() { echo "+ $*"; [[ $APPLY == 1 ]] || return 0; "$@"; }
die() { echo "error: $*" >&2; exit 1; }
psql_pg() { sudo -u postgres psql -X -qAt -v ON_ERROR_STOP=1 "$@"; }
sqlrun() { echo "+ psql: $1"; [[ $APPLY == 1 ]] || return 0; psql_pg -c "$1"; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -f "$REPO/deploy/systemd/$NEW-web.service" ]] || die "this checkout has no $NEW units; git pull first"
[[ $APPLY == 1 ]] || say "DRY RUN (nothing is changed; add --apply)"

any_active() {
  local s
  for s in "$@"; do systemctl is-active --quiet "$s" && return 0; done
  return 1
}

nginx_site() {   # the site file that includes the QuestersBoard snippet (old or new name)
  grep -lE "snippets/($OLD|$NEW)_location" /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf 2>/dev/null \
    | head -1 | xargs -r readlink -f || true
}

# ---------------------------------------------------------------- rename
do_rename() {
  if any_active wowquester-web.socket wowquester-web wowquester-bot wowquester-worker; then
    [[ $APPLY == 1 ]] && die "stop the services first: systemctl stop wowquester-web.socket wowquester-web wowquester-bot wowquester-worker"
    echo "!! services are running (fine for a dry run; --apply needs them stopped)"
  fi
  [[ -d /etc/$OLD ]] || die "/etc/$OLD not found (already renamed?)"
  [[ ! -e /etc/$NEW ]] || die "/etc/$NEW already exists"
  [[ -n "$PGETC" && -f "$PGETC/pg_hba.conf" ]] || die "PostgreSQL config dir not found"
  local r t
  for r in "${ROLES[@]}"; do
    t="$(psql_pg -c "select left(rolpassword,13) from pg_authid where rolname='${OLD}_$r'")"
    [[ "$t" == "SCRAM-SHA-256" ]] || die "role ${OLD}_$r password is not SCRAM (a rename would clear an MD5 password)"
  done
  if [[ $APPLY == 1 && "$(psql_pg -c "select count(*) from pg_stat_activity where datname='$OLD'")" != 0 ]]; then
    die "something is still connected to database $OLD"
  fi

  run install -d -o root -g root -m 0700 "$STATE"

  say "1. PostgreSQL database and roles (passwords are SCRAM, so they survive the rename)"
  sqlrun "ALTER DATABASE $OLD RENAME TO $NEW"
  [[ "$(psql_pg -c "select count(*) from pg_database where datname='${OLD}_restore_check'")" == 0 ]] \
    || sqlrun "ALTER DATABASE ${OLD}_restore_check RENAME TO ${NEW}_restore_check"
  for r in "${ROLES[@]}"; do sqlrun "ALTER ROLE ${OLD}_$r RENAME TO ${NEW}_$r"; done

  say "2. pg_hba.conf and conf.d ($PGETC)"
  run cp -p "$PGETC/pg_hba.conf" "$STATE/pg_hba.conf"
  run sed -i "s/$OLD/$NEW/g" "$PGETC/pg_hba.conf"
  [[ -e "$PGETC/conf.d/50-$OLD.conf" ]] && run mv "$PGETC/conf.d/50-$OLD.conf" "$PGETC/conf.d/50-$NEW.conf"
  run systemctl reload postgresql

  say "3. Service account $OLD -> $NEW"
  run usermod -l "$NEW" -d "/var/lib/$NEW" "$OLD"
  run groupmod -n "$NEW" "$OLD"

  say "4. Folders and config.toml"
  echo "--- config.toml changes:"
  sed "s/$OLD/$NEW/g" "/etc/$OLD/config.toml" | diff -u "/etc/$OLD/config.toml" - || true
  run mv "/etc/$OLD" "/etc/$NEW"
  run cp -p "/etc/$NEW/config.toml" "/etc/$NEW/config.toml.before_rename"
  run sed -i "s/$OLD/$NEW/g" "/etc/$NEW/config.toml"
  local d
  for d in "/var/lib/$OLD" "/var/backups/$OLD" "/root/${OLD}_db"; do
    [[ -e "$d" ]] && run mv "$d" "${d/$OLD/$NEW}"
  done

  say "5. Credentials re-encrypted under the new names (old files kept for rollback)"
  local f n
  for f in "$CREDS/$OLD".*; do
    [[ -e "$f" ]] || continue
    n="${f##*/$OLD.}"
    echo "+ systemd-creds decrypt $OLD.$n | systemd-creds encrypt --with-key=host $NEW.$n"
    [[ $APPLY == 1 ]] || continue
    systemd-creds decrypt --name="$OLD.$n" "$f" - \
      | systemd-creds encrypt --with-key=host --name="$NEW.$n" - "$CREDS/$NEW.$n"
    chmod 0600 "$CREDS/$NEW.$n"
  done

  say "6. Old units and wrapper saved to $STATE, then removed"
  local u enabled=()
  for u in "${OLD_UNITS[@]}"; do
    [[ -e /etc/systemd/system/$u ]] || continue
    systemctl is-enabled --quiet "$u" 2>/dev/null && enabled+=("$u")
    run cp -p "/etc/systemd/system/$u" "$STATE/$u"
  done
  echo "  enabled before: ${enabled[*]:-none}"
  [[ $APPLY == 1 ]] && printf '%s\n' "${enabled[@]}" > "$STATE/enabled_units"
  (( ${#enabled[@]} )) && run systemctl disable "${enabled[@]}"
  for u in "${OLD_UNITS[@]}"; do [[ -e /etc/systemd/system/$u ]] && run rm "/etc/systemd/system/$u"; done
  [[ -e /usr/local/sbin/$OLD-admin ]] && run mv "/usr/local/sbin/$OLD-admin" "$STATE/$OLD-admin"
  run systemctl daemon-reload

  say "7. Install the new release into /opt/$NEW (install.sh), backup units if they were installed"
  run "$REPO/deploy/install.sh"
  if [[ -e "$STATE/wowquester-backup.timer" || ( $APPLY == 0 && -e /etc/systemd/system/wowquester-backup.timer ) ]]; then
    for u in $NEW-backup.service $NEW-backup.timer; do
      run install -o root -g root -m 0644 "$REPO/deploy/backup/$u" "/etc/systemd/system/$u"
    done
    run systemctl daemon-reload
  fi
  for u in "${enabled[@]}"; do run systemctl enable "${u/$OLD/$NEW}"; done

  say "done. Next (separate approvals): start the services, then: sudo ./deploy/rename_host.sh --nginx --apply"
}

# ---------------------------------------------------------------- nginx
do_nginx() {
  local site; site="$(nginx_site)"
  [[ -n "$site" ]] || die "no Nginx site file includes the snippet"
  grep -q "snippets/${OLD}_location" "$site" || { say "$site already uses the new snippet"; return 0; }
  say "Nginx: install snippets/${NEW}_locations.conf and switch the include line in $site"
  echo "--- $site changes:"
  sed "s#snippets/${OLD}_location#snippets/${NEW}_location#" "$site" | diff -u "$site" - || true
  run install -d -o root -g root -m 0700 "$STATE"
  run cp -p "$site" "$STATE/nginx_site.conf"
  [[ $APPLY == 1 ]] && echo "$site" > "$STATE/nginx_site_path"
  run install -o root -g root -m 0644 "$REPO/deploy/nginx/${NEW}_locations.conf" "/etc/nginx/snippets/${NEW}_locations.conf"
  run sed -i "s#snippets/${OLD}_location#snippets/${NEW}_location#" "$site"
  if [[ $APPLY == 1 ]] && ! nginx -t; then
    echo "!! nginx -t failed; restoring $site" >&2
    cp -p "$STATE/nginx_site.conf" "$site"
    exit 1
  fi
  run systemctl reload nginx
  say "done. Old snippet /etc/nginx/snippets/${OLD}_locations.conf is left in place (unused)."
}

# ---------------------------------------------------------------- rollback
do_rollback() {
  [[ -d "$STATE" ]] || die "$STATE not found; nothing to roll back"
  say "Rollback: stop and remove new units"
  local u
  for u in $NEW-web.socket $NEW-web.service $NEW-bot.service $NEW-worker.service $NEW-backup.timer $NEW-backup.service; do
    systemctl disable --now "$u" 2>/dev/null || true
    rm -f "/etc/systemd/system/$u"
  done
  rm -f "/usr/local/sbin/$NEW-admin"

  if [[ -f "$STATE/nginx_site_path" ]]; then
    say "Nginx include restored"
    cp -p "$STATE/nginx_site.conf" "$(cat "$STATE/nginx_site_path")"
    nginx -t && systemctl reload nginx
    rm -f "$STATE/nginx_site_path"
  fi

  say "Credentials, folders, account"
  local f
  for f in "$CREDS/$NEW".*; do [[ -e "$f" && -e "${f/$NEW./$OLD.}" ]] && rm -f "$f"; done
  [[ -f /etc/$NEW/config.toml.before_rename ]] && mv /etc/$NEW/config.toml.before_rename /etc/$NEW/config.toml
  local d
  for d in "/etc/$NEW" "/var/lib/$NEW" "/var/backups/$NEW" "/root/${NEW}_db"; do
    [[ -e "$d" ]] && mv "$d" "${d/$NEW/$OLD}"
  done
  id -u "$NEW" >/dev/null 2>&1 && usermod -l "$OLD" -d "/var/lib/$OLD" "$NEW"
  getent group "$NEW" >/dev/null && groupmod -n "$OLD" "$NEW"

  say "PostgreSQL"
  local r
  for r in "${ROLES[@]}"; do
    [[ "$(psql_pg -c "select count(*) from pg_roles where rolname='${NEW}_$r'")" == 1 ]] \
      && psql_pg -c "ALTER ROLE ${NEW}_$r RENAME TO ${OLD}_$r"
  done
  for d in "$NEW" "${NEW}_restore_check"; do
    [[ "$(psql_pg -c "select count(*) from pg_database where datname='$d'")" == 1 ]] \
      && psql_pg -c "ALTER DATABASE $d RENAME TO ${d/$NEW/$OLD}"
  done
  [[ -f "$STATE/pg_hba.conf" ]] && cp -p "$STATE/pg_hba.conf" "$PGETC/pg_hba.conf"
  [[ -e "$PGETC/conf.d/50-$NEW.conf" ]] && mv "$PGETC/conf.d/50-$NEW.conf" "$PGETC/conf.d/50-$OLD.conf"
  systemctl reload postgresql

  say "Old units and wrapper"
  for u in "${OLD_UNITS[@]}"; do [[ -e "$STATE/$u" ]] && cp -p "$STATE/$u" "/etc/systemd/system/$u"; done
  [[ -e "$STATE/$OLD-admin" ]] && cp -p "$STATE/$OLD-admin" "/usr/local/sbin/$OLD-admin"
  systemctl daemon-reload
  [[ -s "$STATE/enabled_units" ]] && xargs -a "$STATE/enabled_units" systemctl enable
  say "done. Start with: sudo systemctl start wowquester-web.socket wowquester-web wowquester-worker wowquester-bot"
}

case "$MODE" in
  rename) do_rename ;;
  nginx) do_nginx ;;
  rollback) do_rollback ;;
esac
