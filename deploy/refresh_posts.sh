#!/usr/bin/env bash
# Edit the Discord posts of upcoming and running events and open board posts in place (after an install
# changes how posts look). Nothing new is posted and nobody is pinged.
#   sudo ./deploy/refresh_posts.sh [--dry-run]
# Default: queued at once (the worker applies them within a minute). --dry-run only lists them.
set -euo pipefail
CRED=/etc/credstore.encrypted/questersboard.db-app-password
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
yes=(--yes)
while (( $# )); do
  case "$1" in
    --dry-run) yes=() ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
exec systemd-run --quiet --pipe --wait --collect -p User=questersboard \
  -p "LoadCredentialEncrypted=questersboard.db-app-password:$CRED" \
  /usr/local/sbin/questersboard-admin refresh-posts "${yes[@]}"
