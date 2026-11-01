#!/usr/bin/env bash
# Install or upgrade WoWQuester from this checkout. Run with sudo from the repo root:
#   sudo ./deploy/install.sh            # build a release, switch "current", install units (never enables/starts)
#   sudo ./deploy/install.sh --dry-run  # print what would happen
# It does NOT: run database migrations, touch Nginx/DNS/firewall, register Discord commands,
# install system packages, or edit any other service. Those are separate, approved steps
# (docs/install.md). If the services are already running they are restarted on the new release
# and rolled back automatically if health fails.
set -euo pipefail

APP=wowquester
USER_NAME=wowquester
BASE=/opt/wowquester
ETC=/etc/wowquester
UNITS=(wowquester-web.socket wowquester-web.service wowquester-bot.service wowquester-worker.service)
SERVICES=(wowquester-web wowquester-bot wowquester-worker)
ADMIN_WRAPPER=/usr/local/sbin/wowquester-admin
PY=python3.12
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/src/wowquester/__init__.py")"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REL="$BASE/releases/${VERSION}-${STAMP}"

run() { echo "+ $*"; [[ $DRY == 1 ]] || "$@"; }
die() { echo "error: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -n "$VERSION" ]] || die "cannot read version"
command -v $PY >/dev/null || die "$PY not found"
$PY -c 'import ensurepip' 2>/dev/null || die "python3.12-venv is not installed (needs approval: apt install python3.12-venv)"
command -v systemd-creds >/dev/null || die "systemd-creds not found"
[[ "$(systemctl --version | awk 'NR==1{print $2}')" -ge 254 ]] || die "systemd >= 254 required (ImportCredential=)"

echo "== wowquester $VERSION -> $REL"

# 1. One service account for the three units (no login, no home, no other groups).
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  run useradd --system --user-group --no-create-home --home-dir /var/lib/$APP --shell /usr/sbin/nologin \
      --comment "WoWQuester" "$USER_NAME"
fi

# 2. Directories. Config root:wowquester 0640; credentials live in /etc/credstore.encrypted (root 0700).
run install -d -o root -g root -m 0755 "$BASE" "$BASE/releases"
run install -d -o root -g "$USER_NAME" -m 0750 "$ETC"
# /etc/credstore.encrypted is shared with other services: create it if missing, never re-own or re-mode it.
[[ -d /etc/credstore.encrypted ]] || run install -d -o root -g root -m 0700 /etc/credstore.encrypted
run install -d -o root -g root -m 0700 /var/backups/$APP
if [[ ! -e "$ETC/config.toml" ]]; then
  run install -o root -g "$USER_NAME" -m 0640 "$REPO/deploy/config.example.toml" "$ETC/config.toml"
  echo "!! edit $ETC/config.toml (guild_id, application_id, role IDs, channel IDs) before starting"
fi

# 3. Build the release: hash-pinned dependencies, hash-pinned build backend, then the package
#    itself without deps. A failed build removes its half-built release directory.
cleanup_failed() { [[ $DRY == 1 ]] || { echo "!! build failed; removing $REL" >&2; rm -rf "$REL"; }; }
trap cleanup_failed ERR
run install -d -o root -g root -m 0755 "$REL"
run cp -a "$REPO/src" "$REPO/deploy" "$REPO/pyproject.toml" "$REPO/requirements.lock" "$REPO/alembic.ini" \
          "$REPO/LICENSE" "$REPO/README.md" "$REL/"
run $PY -m venv "$REL/venv"
run "$REL/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir --require-hashes --no-deps -r "$REL/requirements.lock"
run "$REL/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir --require-hashes --no-deps -r "$REL/deploy/build_requirements.lock"
run "$REL/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir --no-deps --no-build-isolation "$REL"
run rm -rf "$REL/src/wowquester.egg-info" "$REL/build"
run chown -R root:root "$REL"
run chmod -R go-w "$REL"
run "$REL/venv/bin/wowquester-admin" --config "$ETC/config.toml" check-config --quiet

trap - ERR

# 4. Switch "current" atomically, remembering the previous release.
PREV=""
[[ -L "$BASE/current" ]] && PREV="$(readlink -f "$BASE/current")"
run ln -sfn "$REL" "$BASE/current.new"
run mv -T "$BASE/current.new" "$BASE/current"
[[ -n "$PREV" ]] && { echo "$PREV" > "$BASE/previous.tmp" && mv "$BASE/previous.tmp" "$BASE/previous"; } || true

# 5. Admin wrapper and units (installed, NOT enabled or started).
WRAP="$(mktemp)"
cat > "$WRAP" <<EOF2
#!/bin/sh
export WOWQUESTER_CONFIG=\${WOWQUESTER_CONFIG:-$ETC/config.toml}
exec $BASE/current/venv/bin/wowquester-admin "\$@"
EOF2
run install -o root -g root -m 0755 "$WRAP" "$ADMIN_WRAPPER"
rm -f "$WRAP"
for u in "${UNITS[@]}"; do run install -o root -g root -m 0644 "$REPO/deploy/systemd/$u" "/etc/systemd/system/$u"; done
run systemctl daemon-reload
for u in "${UNITS[@]}"; do run systemd-analyze verify "/etc/systemd/system/$u" || echo "warning: $u verify reported issues"; done

# 6. Restart on the new release if running; roll back on failure.
running=()
for s in "${SERVICES[@]}"; do systemctl is-active --quiet "$s" && running+=("$s"); done
if (( ${#running[@]} )); then
  run systemctl restart "${running[@]}"
  ok=0
  for _ in $(seq 1 30); do
    sleep 2
    if "$REPO/deploy/health.sh" --quiet --deploy; then ok=1; break; fi
  done
  if [[ $ok == 1 ]]; then
    "$REPO/deploy/health.sh" --deploy || true
  elif [[ -n "$PREV" ]]; then
    echo "!! unhealthy after upgrade; rolling back to $PREV" >&2
    run "$REPO/deploy/rollback.sh" "$PREV"
    exit 1
  fi
fi

echo "== done. Next (each needs approval): migrate, credentials, nginx include, enable units. See docs/install.md"
