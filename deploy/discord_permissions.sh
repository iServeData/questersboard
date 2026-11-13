#!/usr/bin/env bash
# Read-only check of the WoW category's Discord permissions (nothing is changed in Discord).
#   sudo ./deploy/discord_permissions.sh [--category CATEGORY_ID]
set -euo pipefail
D=/etc/credstore.encrypted
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
exec systemd-run --quiet --pipe --wait --collect -p User=questersboard \
  -p "LoadCredentialEncrypted=questersboard.discord-bot-token:$D/questersboard.discord-bot-token" \
  /usr/local/sbin/questersboard-admin discord-permissions "$@"
