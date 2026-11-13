#!/usr/bin/env bash
# Refresh everyone's Blizzard character data now, on their behalf (the same run the timer does, without
# waiting for the hour): imported details with each owner's groups and sharing, and Blizzard checks.
#   sudo ./deploy/refresh_details.sh [--dry-run]
# Nothing is posted to Discord. A run already in progress (timer or manual) makes this one do nothing.
set -euo pipefail
D=/etc/credstore.encrypted
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
yes=(--yes)
case "${1:-}" in
  "") ;;
  --dry-run) yes=() ;;
  *) echo "unknown option: $1" >&2; exit 2 ;;
esac
args=()
for c in db-app-password blizzard-client-id blizzard-client-secret; do
  args+=(-p "LoadCredentialEncrypted=questersboard.$c:$D/questersboard.$c")
done
exec systemd-run --quiet --pipe --wait --collect -p User=questersboard "${args[@]}" \
  /usr/local/sbin/questersboard-admin refresh-details "${yes[@]}"
