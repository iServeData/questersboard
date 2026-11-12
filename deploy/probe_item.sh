#!/usr/bin/env bash
# Show what an item card would contain, read-only (nothing saved or posted).
#   sudo ./deploy/probe_item.sh "Item Name" [EDITION]
# EDITION defaults to forever; [item_links] fallbacks are searched after it.
set -euo pipefail
D=/etc/credstore.encrypted
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -n ${1:-} ]] || { echo "usage: sudo $0 \"Item Name\" [EDITION]" >&2; exit 2; }
args=()
for c in db-app-password blizzard-client-id blizzard-client-secret; do
  args+=(-p "LoadCredentialEncrypted=questersboard.$c:$D/questersboard.$c")
done
exec systemd-run --quiet --pipe --wait --collect -p User=questersboard "${args[@]}" \
  /usr/local/sbin/questersboard-admin blizzard probe-item "${2:-forever}" --name "$1"
