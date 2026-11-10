#!/usr/bin/env bash
# Full health check (DB + outbox + web socket) with the app DB credential loaded for this one run.
# The credential is decrypted by systemd into a private, transient unit; it is never on the command line.
#   sudo ./deploy/health.sh [--quiet] [--deploy]
# --deploy (used by install.sh/rollback.sh): dead outbox jobs are reported but don't fail the check,
# because a Discord delivery that gave up says nothing about whether the new release works.
set -euo pipefail
CRED=/etc/credstore.encrypted/questersboard.db-app-password
SOCK=/run/questersboard-web.sock
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
quiet=0; extra=()
for a in "$@"; do
  case "$a" in
    --quiet) quiet=1 ;;
    --deploy) extra+=(--deploy) ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done
args=(systemd-run --quiet --pipe --wait --collect
      -p "LoadCredentialEncrypted=questersboard.db-app-password:$CRED"
      /usr/local/sbin/questersboard-admin health --socket "$SOCK" "${extra[@]}")
if (( quiet )); then "${args[@]}" >/dev/null 2>&1; else "${args[@]}"; fi
