#!/usr/bin/env bash
# Show QuestersBoard's own offline pages (instead of the site's plain 502/503/504) while QuestersBoard
# isn't running. Installs the repo's deploy/nginx/questersboard_locations.conf over the live snippet,
# keeping a backup, checks it with nginx -t, then reloads Nginx (or puts the backup back on failure).
#   sudo ./deploy/enable_offline_pages.sh
# Needs the pages first: sudo ./deploy/install.sh writes them to /opt/questersboard/offline.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$HERE/nginx/questersboard_locations.conf
DST=/etc/nginx/snippets/questersboard_locations.conf
PAGES=/opt/questersboard/offline
[[ -f $PAGES/502.html ]] || { echo "no offline pages in $PAGES yet; run sudo ./deploy/install.sh first" >&2; exit 1; }
[[ -f $DST ]] || { echo "$DST isn't installed; see docs/install.md step 8" >&2; exit 1; }
if cmp -s "$SRC" "$DST"; then
  echo "snippet already up to date"
else
  BAK=$DST.bak.$(date +%Y%m%d%H%M%S)
  cp -p "$DST" "$BAK"
  echo "backup: $BAK"
  diff -u "$BAK" "$SRC" || true
  install -m 0644 "$SRC" "$DST"
  if nginx -t 2>&1; then
    systemctl reload nginx
    echo "nginx reloaded"
  else
    cp -p "$BAK" "$DST"
    echo "nginx -t failed; the previous snippet is back and Nginx wasn't reloaded" >&2
    exit 1
  fi
fi
URL=$(sed -n 's/^public_base_url *= *"\(https\?:\/\/[^"]*\)".*/\1/p' /etc/questersboard/config.toml | head -1)
if [[ -n $URL ]]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' "${URL%/}/wow/quester-offline/502.html" || true)
  echo "offline page check: ${URL%/}/wow/quester-offline/502.html -> HTTP $code (200 is right)"
fi
