# Rollback

Three independent layers. Use the smallest one that fixes the problem.

## 1. Code (seconds, no data change)

Every install is a separate directory under `/opt/wowquester/releases/`; `current` is a symlink.

```sh
sudo /opt/wowquester/current/deploy/rollback.sh                 # back to the previous release
sudo /opt/wowquester/current/deploy/rollback.sh /opt/wowquester/releases/<release>
```

It checks the target's config validation, switches the link atomically, restarts the running units and
runs the health check. `install.sh` calls it automatically if an upgrade isn't healthy within a minute.

## 2. Schema (minutes, after a fresh backup)

Only needed when a release with a new migration has to be undone and the old code can't run on the new schema.

```sh
sudo systemctl start wowquester-backup.service                 # fresh encrypted backup first
sudo -u wowquester wowquester-admin migrate down <revision> --sql | less   # review
sudo systemd-run --pipe --wait -p User=wowquester \
  -p LoadCredentialEncrypted=wowquester.db-admin-password:/etc/credstore.encrypted/wowquester.db-admin-password \
  /usr/local/sbin/wowquester-admin migrate down <revision>
sudo /opt/wowquester/current/deploy/rollback.sh <release that matches revision>
```

Every migration has a tested downgrade. Downgrading past `0001` removes all WoWQuester tables; use a
restore instead if you want the data.

## 3. Data (restore)

See `backup_restore.md`. Production restores need `WOWQUESTER_APPROVED_CHANGE=restore-production`.

## Removing WoWQuester entirely

Nothing else on the host depends on it.

```sh
sudo systemctl disable --now wowquester-web.socket wowquester-web wowquester-bot wowquester-worker wowquester-backup.timer
# remove the `include snippets/wowquester_locations.conf;` line(s), then:
sudo nginx -t && sudo systemctl reload nginx
# revert the WoW page patch through the site's normal flow; its static schedule list still works without WoWQuester
```

The database, `/etc/wowquester`, credentials and backups stay until you delete them deliberately.
