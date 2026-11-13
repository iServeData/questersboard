#!/usr/bin/env bash
# Turn on the timed refresh from Blizzard: enables questersboard-refresh.timer (installed, but not
# enabled, by install.sh). The schedule itself is [blizzard] auto_refresh_hours in config.toml.
#   sudo ./deploy/enable_auto_refresh.sh          # enable and show the next run
#   sudo ./deploy/enable_auto_refresh.sh --off    # disable the timer again
set -euo pipefail
U=/etc/systemd/system
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -e $U/questersboard-refresh.timer ]] || { echo "questersboard-refresh.timer isn't installed; run sudo ./deploy/install.sh first" >&2; exit 1; }
if [[ ${1:-} == --off ]]; then
  systemctl disable --now questersboard-refresh.timer
  echo "timed refresh off"
  exit 0
fi
# The host's isolation drop-in for the other units (docs/install.md), if there is one, applies here too.
if [[ -e $U/questersboard-worker.service.d/10-isolation.conf && ! -e $U/questersboard-refresh.service.d/10-isolation.conf ]]; then
  install -D -m 0644 "$U/questersboard-worker.service.d/10-isolation.conf" "$U/questersboard-refresh.service.d/10-isolation.conf"
  echo "copied the isolation drop-in to questersboard-refresh.service"
fi
systemctl daemon-reload
systemctl enable --now questersboard-refresh.timer
echo "timed refresh on"
/usr/local/sbin/questersboard-admin refresh-details --next || true
systemctl list-timers --no-pager questersboard-refresh.timer
