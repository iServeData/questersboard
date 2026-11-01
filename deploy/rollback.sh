#!/usr/bin/env bash
# Point /opt/wowquester/current back at the previous (or a named) release and restart running units.
#   sudo ./deploy/rollback.sh                 # previous release
#   sudo ./deploy/rollback.sh <release-dir>   # one from /opt/wowquester/releases
# Code only. Schema rollback is separate: `wowquester-admin migrate down <rev>` (docs/rollback.md),
# and only after a fresh backup.
set -euo pipefail
BASE=/opt/wowquester
SERVICES=(wowquester-web wowquester-bot wowquester-worker)
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
TARGET="${1:-$(cat "$BASE/previous" 2>/dev/null || true)}"
[[ -n "$TARGET" && -x "$TARGET/venv/bin/python" ]] || { echo "no valid release to roll back to" >&2; ls -1 "$BASE/releases"; exit 1; }
[[ "$(readlink -f "$TARGET")" == "$BASE"/releases/* ]] || { echo "target must be under $BASE/releases" >&2; exit 1; }
CUR="$(readlink -f "$BASE/current")"
"$TARGET/venv/bin/wowquester-admin" --config /etc/wowquester/config.toml check-config --quiet
ln -sfn "$TARGET" "$BASE/current.new" && mv -T "$BASE/current.new" "$BASE/current"
echo "$CUR" > "$BASE/previous"
echo "current -> $TARGET (was $CUR)"
running=()
for s in "${SERVICES[@]}"; do systemctl is-active --quiet "$s" && running+=("$s"); done
if (( ${#running[@]} )); then
  systemctl restart "${running[@]}"
  sleep 5
  wowquester-admin health || { echo "unhealthy after rollback; journalctl -u wowquester-web -u wowquester-bot -u wowquester-worker" >&2; exit 1; }
fi
