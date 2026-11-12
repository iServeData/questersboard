#!/usr/bin/env bash
# Look up recent item cards again and edit them in place on Discord (after an install changes how cards
# look, or once Blizzard adds an item). Nothing new is posted and nobody is pinged.
#   sudo ./deploy/refresh_item_cards.sh [--hours N] [--dry-run]
# Default: cards from the last 24 hours, queued at once (the worker applies them within a minute).
set -euo pipefail
CRED=/etc/credstore.encrypted/questersboard.db-app-password
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
hours=24; yes=(--yes)
while (( $# )); do
  case "$1" in
    --hours) [[ ${2:-} =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "--hours needs a number" >&2; exit 2; }; hours=$2; shift ;;
    --dry-run) yes=() ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
exec systemd-run --quiet --pipe --wait --collect -p User=questersboard \
  -p "LoadCredentialEncrypted=questersboard.db-app-password:$CRED" \
  /usr/local/sbin/questersboard-admin refresh-item-cards --hours "$hours" "${yes[@]}"
