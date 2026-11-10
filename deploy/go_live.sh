#!/usr/bin/env bash
# Go live: serve QuestersBoard on the public site instead of the prototype (docs/production_rollout.md, steps 5-6, 8).
#   sudo ./deploy/go_live.sh --url https://LIVE_HOST --site-file /etc/nginx/sites-available/SITE \
#                            --after 'include /etc/nginx/snippets/SOME_EXISTING_INCLUDE.conf;'
#   sudo ./deploy/go_live.sh --rollback /root/wowquester_go_live_STAMP.tgz
#
# What it does, in order (nothing changes until step 3):
#   1. Checks: run as root; Discord redirect confirmed; config, Nginx snippet and live site file present; the
#      --after line appears exactly once in the live site file; nginx -t passes; services installed.
#   2. Backs up the live site file and config.toml to /root (mode 600).
#   3. Sets [community] public_base_url to --url (config.toml stays root:wowquester 0640) and validates it.
#   4. Adds `include /etc/nginx/snippets/wowquester_location[s].conf;` after the --after line, nginx -t
#      (everything is put back if it fails), reload.
#   5. Restarts web, bot and worker; runs deploy/health.sh.
#   6. Checks the live host: status.json answers JSON without sign-in, the login redirect names the live
#      callback, the Chronicle archive answers.
# Firewall, DNS, certificates, the database and Discord commands are not touched. Safe to run again.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# (WQ_* overrides exist only for tests/test_go_live.sh's stand-ins.)
ETC="${WQ_ETC:-/etc/wowquester}"
CONF="$ETC/config.toml"
SNIP="${WQ_SNIP:-/etc/nginx/snippets/wowquester_locations.conf}"
ADMIN="${WQ_ADMIN:-/usr/local/sbin/wowquester-admin}"
HEALTH="${WQ_HEALTH:-$HERE/health.sh}"
LINE='include /etc/nginx/snippets/wowquester_location[s].conf;'
SERVICES=(wowquester-web wowquester-bot wowquester-worker)
STAMP="$(date +%Y%m%d_%H%M%S)"
say()  { printf '\n== %s\n' "$*"; }
stop() { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }

URL="" SITE="" AFTER="" ROLLBACK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="${2%/}"; shift 2 ;;
    --site-file) SITE="$2"; shift 2 ;;
    --after) AFTER="$2"; shift 2 ;;
    --rollback) ROLLBACK="$2"; shift 2 ;;
    *) stop "unknown option: $1" ;;
  esac
done
[ "$EUID" -eq 0 ] || [ -n "${WQ_ETC:-}" ] || stop "run with sudo."

restart_and_health() {
  systemctl restart "${SERVICES[@]}"
  sleep 3
  "$HEALTH" --deploy || return 1
}

if [ -n "$ROLLBACK" ]; then
  say "Rollback from $ROLLBACK"
  [ -f "$ROLLBACK" ] || stop "$ROLLBACK not found."
  tar -tzPf "$ROLLBACK"
  tar -xzPf "$ROLLBACK"
  nginx -t -q || stop "nginx -t fails after restoring; nothing reloaded. Check the files listed above."
  systemctl reload nginx
  restart_and_health || echo "health check reported a problem; see above"
  echo "Restored. QuestersBoard is back on its previous address."
  exit 0
fi

[ -n "$URL" ] && [ -n "$SITE" ] && [ -n "$AFTER" ] || stop "needs --url, --site-file and --after (see the top of this file)."
case "$URL" in https://*) ;; *) stop "--url must start with https://";; esac
HOST="${URL#https://}"; HOST="${HOST%%/*}"
BACKUP="${WQ_BACKUP_DIR:-/root}/wowquester_go_live_${STAMP}.tgz"

say "1/6 Checks (nothing is changed yet)"
[ -f "$CONF" ] || stop "$CONF not found."
[ -f "$SNIP" ] || stop "$SNIP not installed (docs/install.md step 8)."
[ -f "$SITE" ] || stop "$SITE not found."
for s in "${SERVICES[@]}"; do systemctl cat "$s.service" >/dev/null 2>&1 || stop "$s.service is not installed."; done
[ "$(grep -cE '^[[:space:]]*public_base_url[[:space:]]*=' "$CONF" || true)" = 1 ] \
  || stop "$CONF should have exactly one public_base_url line."
CURRENT="$(grep -E '^[[:space:]]*public_base_url[[:space:]]*=' "$CONF" | sed -E 's/^[^"]*"([^"]*)".*/\1/')"
echo "public_base_url now: $CURRENT"
echo "will become:         $URL"
if grep -qF "$LINE" "$SITE"; then
  echo "$SITE already includes QuestersBoard"
else
  [ "$(grep -cF "$AFTER" "$SITE" || true)" = 1 ] || stop "$SITE should have exactly one line: $AFTER"
fi
nginx -t -q || stop "nginx -t fails before any change. Nothing changed."
echo "ok"
echo
echo "In the Discord developer portal (OAuth2 > Redirects) this must already be listed:"
echo "    $URL/wow/quester/oauth/callback"
read -r -p "Type yes when it is saved there: " ans
[ "$ans" = yes ] || stop "Nothing changed."

say "2/6 Backup"
tar -czPf "$BACKUP" "$SITE" "$CONF"; chmod 600 "$BACKUP"
echo "saved $BACKUP"
restore() {
  tar -xzPf "$BACKUP"
  nginx -t -q && systemctl reload nginx || true
  echo "the previous files are back (backup $BACKUP)"
}

say "3/6 Config: public_base_url"
if [ "$CURRENT" != "$URL" ]; then
  tmp="$(mktemp)"
  sed -E "s#^([[:space:]]*public_base_url[[:space:]]*=[[:space:]]*)\"[^\"]*\"#\1\"$URL\"#" "$CONF" > "$tmp"
  diff -u "$CONF" "$tmp" | sed -n '3,$p' || true
  cat "$tmp" > "$CONF"; rm -f "$tmp"          # keeps owner and mode
fi
if ! "$ADMIN" --config "$CONF" check-config --quiet; then
  restore; stop "the new config doesn't validate. Everything was put back."
fi
echo "ok"

say "4/6 Nginx include"
if ! grep -qF "$LINE" "$SITE"; then
  tmp="$(mktemp)"
  awk -v a="$AFTER" -v l="$LINE" '{ print } index($0, a) { match($0, /^[ \t]*/); print substr($0, 1, RLENGTH) "# QuestersBoard (/wow/quester/, /wow/chronicle/): its own service on a unix socket"; print substr($0, 1, RLENGTH) l }' "$SITE" > "$tmp"
  diff -u "$SITE" "$tmp" | sed -n '3,$p' || true
  cat "$tmp" > "$SITE"; rm -f "$tmp"
fi
if ! nginx -t -q; then
  nginx -t || true
  restore; stop "nginx -t failed with the include. Everything was put back."
fi
systemctl reload nginx
echo "Nginx reloaded"

say "5/6 Restart and health"
if ! restart_and_health; then
  echo "The services didn't come back healthy. To undo:  sudo $0 --rollback $BACKUP"
  exit 1
fi

say "6/6 Checking https://$HOST"
FAIL=0
check() {  # check NAME URL EXPECTED_CODE [TEXT]
  local body code; body="$(mktemp)"
  code="$(curl -s -o "$body" -w '%{http_code}' "$2" || true)"
  if [ "$code" = "$3" ] && { [ -z "${4:-}" ] || grep -qF "$4" "$body"; }; then echo "ok    $3  $1"
  else echo "CHECK $1 answered $code (expected $3${4:+ with \"$4\"})"; FAIL=1; fi
  rm -f "$body"
}
check "status.json, public"   "$URL/wow/quester/status.json" 200 '"generated_at"'
check "Chronicle archive"     "$URL/wow/chronicle/"          200
check "Privacy page"          "$URL/wow/quester/privacy"     200 "Privacy"
loc="$(curl -s -o /dev/null -w '%{redirect_url}' "$URL/wow/quester/login" || true)"
case "$loc" in
  *"redirect_uri=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$URL/wow/quester/oauth/callback")"*)
    echo "ok    302  Discord login sends people back to $URL" ;;
  *) echo "CHECK login redirect doesn't name $URL/wow/quester/oauth/callback"; FAIL=1 ;;
esac
echo
if [ "$FAIL" = 0 ]; then
  echo "Live. Next: merge the site's prototype into main so the WoW page links here."
  echo "Undo at any time:  sudo $0 --rollback $BACKUP"
else
  echo "A check above didn't match. Undo with:  sudo $0 --rollback $BACKUP   (or send me the output)"
fi
