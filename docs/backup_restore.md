# Backup and restore

## What a backup contains

`deploy/backup/backup.sh` writes one file, `/var/backups/wowquester/wowquester_<UTC stamp>.tar.gpg`:

- `db.dump`: `pg_dump -Fc --no-owner` of the `wowquester` database, as `wowquester_admin`
- `files.tar`: `/etc/wowquester/config.toml` and `/var/lib/wowquester/uploads`
- `SHA256SUMS` for both

The archive is encrypted with GnuPG to a **public** key (`/etc/wowquester/backup_recipient`, a
40-hex fingerprint; public key in `/etc/wowquester/backup_gnupg`). The private key stays off the host, so a
host compromise can't read old backups. Credentials are not in the backup: they are encrypted to the
host and useless elsewhere. Keep an offline copy of the **field key** and the four DB passwords
alongside the private key, or encrypted fields can't be read after a restore to a new host.

Backups older than 30 days are deleted (`WOWQUESTER_BACKUP_KEEP_DAYS`). The `wowquester-backup.timer`
runs nightly once enabled. Copy backups off the host with your existing off-site routine.

## One-time key setup (on an admin workstation, not the host)

```sh
gpg --quick-generate-key "QuestersBoard backups" ed25519 cert never
gpg --quick-add-key <FPR> cv25519 encr never
gpg --export --armor <FPR> > wowquester_backup_public.asc          # copy this to the host
# host:
sudo install -d -m 0700 /etc/wowquester/backup_gnupg
sudo gpg --homedir /etc/wowquester/backup_gnupg --import wowquester_backup_public.asc
echo <FPR> | sudo tee /etc/wowquester/backup_recipient
```

## Restore drill (safe, any time)

```sh
sudo gpg --import wowquester_backup_private.asc     # only for the drill
sudo /opt/wowquester/current/deploy/backup/restore.sh /var/backups/wowquester/wowquester_<stamp>.tar.gpg
sudo gpg --delete-secret-keys <FPR>
```

It decrypts, verifies checksums, recreates the scratch database `wowquester_restore_check` from
`database_init.sql`, restores into it as `wowquester_admin`, re-applies grants, checks the schema
revision and that the app role can read events, then drops the scratch database. Production is not touched.

Verified in the sandbox: encrypt → decrypt → checksum → restore with grants (`app_can_select_events = true`,
Alembic revision `0001`) → scratch database dropped.

## Production restore (approval: `restore-production`)

```sh
sudo env WOWQUESTER_APPROVED_CHANGE=restore-production \
  /opt/wowquester/current/deploy/backup/restore.sh <file.tar.gpg> --into-production
```

It stops the three services, takes a safety backup of the current database, recreates `wowquester`,
restores it (grants come with the dump), restores uploads, and leaves the services stopped. The backed-up
`config.toml` is not applied automatically; compare it with the live one first. Then start the services
and run `wowquester-admin health`.
