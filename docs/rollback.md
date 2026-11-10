# Rollback

Three independent layers. Use the smallest one that fixes the problem.

## 1. Code (seconds, no data change)

Every install is a separate directory under `/opt/questersboard/releases/`; `current` is a symlink.

```sh
sudo /opt/questersboard/current/deploy/rollback.sh                 # back to the previous release
sudo /opt/questersboard/current/deploy/rollback.sh /opt/questersboard/releases/<release>
```

It checks the target's config validation, switches the link atomically, restarts the running units and
runs the health check. `install.sh` calls it automatically if an upgrade isn't healthy within a minute.

## 2. Schema (minutes, after a fresh backup)

Only needed when a release with a new migration has to be undone and the old code can't run on the new schema.

```sh
sudo systemctl start questersboard-backup.service                 # fresh encrypted backup first
sudo -u questersboard questersboard-admin migrate down <revision> --sql | less   # review
sudo systemd-run --pipe --wait -p User=questersboard \
  -p LoadCredentialEncrypted=questersboard.db-admin-password:/etc/credstore.encrypted/questersboard.db-admin-password \
  /usr/local/sbin/questersboard-admin migrate down <revision>
sudo /opt/questersboard/current/deploy/rollback.sh <release that matches revision>
```

Every migration has a tested downgrade. Downgrading past `0001` removes all QuestersBoard tables; use a
restore instead if you want the data.

## 3. Data (restore)

See `backup_restore.md`. Production restores need `QUESTERSBOARD_APPROVED_CHANGE=restore-production`.

## Removing QuestersBoard entirely

Nothing else on the host depends on it.

```sh
sudo systemctl disable --now questersboard-web.socket questersboard-web questersboard-bot questersboard-worker questersboard-backup.timer
# remove the `include snippets/questersboard_locations.conf;` line(s), then:
sudo nginx -t && sudo systemctl reload nginx
# revert the WoW page patch through the site's normal flow; its static schedule list still works without QuestersBoard
```

The database, `/etc/questersboard`, credentials and backups stay until you delete them deliberately.
