#!/usr/bin/env bash
# Full health check (DB + outbox + web socket) with the app DB credential loaded for this one run.
# The credential is decrypted by systemd into a private, transient unit; it is never on the command line.
#   sudo ./deploy/health.sh [--quiet]
set -euo pipefail
CRED=/etc/credstore.encrypted/wowquester.db-app-password
SOCK=/run/wowquester-web.sock
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
args=(systemd-run --quiet --pipe --wait --collect
      -p "LoadCredentialEncrypted=wowquester.db-app-password:$CRED"
      /usr/local/sbin/wowquester-admin health --socket "$SOCK")
if [[ "${1:-}" == "--quiet" ]]; then "${args[@]}" >/dev/null 2>&1; else "${args[@]}"; fi
