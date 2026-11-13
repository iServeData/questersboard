# Installing QuestersBoard

Each numbered step changes the host and should be reviewed before you run it. Steps are ordered so the community site and other services keep running
throughout; QuestersBoard starts in the prototype environment first and goes live only after review.

Target: Ubuntu 24.04 with systemd ≥ 254 (for `ImportCredential=`), Python 3.12, PostgreSQL 16, the
existing Nginx.

## 0. Preflight (read-only)

```sh
python3.12 --version; systemctl --version | head -1; nginx -v
dpkg -l postgresql-16 python3.12-venv 2>/dev/null | tail -n +6
sudo ss -ltnp | grep -E ':5432\b' || true          # nothing should listen beyond localhost
sudo -u postgres psql -Atc "select rolname from pg_roles where rolname='root'" 2>/dev/null || true
```

## 1. Packages (approval: system packages)

If the preflight shows PostgreSQL is not installed and nothing listens on port 5432, the full
`pg_hba.conf` can be installed as is; otherwise merge the QuestersBoard lines (`db_hardening_checklist.md`).

```sh
sudo apt install postgresql-16 python3.12-venv gnupg
```

## 2. PostgreSQL hardening and roles (approval: database)

Review `docs/db_hardening_checklist.md` first. On the host, from a checkout of the `main` branch:

```sh
ORG=your-github-org     # the GitHub organization that hosts this repository
git clone -b main "https://github.com/$ORG/questersboard.git" ~/questersboard
cd ~/questersboard
less deploy/postgres/apply_hardening.sh deploy/postgres/pg_hba.conf deploy/postgres/postgresql_questersboard.conf
sudo deploy/postgres/apply_hardening.sh
```

The script backs up the existing `pg_hba.conf` (as `pg_hba.conf.pre_questersboard`), installs the proposed
`pg_hba.conf` and `conf.d/50-questersboard.conf`, restarts `postgresql@16-main`, generates four random
passwords in `/root/questersboard_db/` (0700, until step 4), creates the roles and the `questersboard` database
with statement logging off for that session, and finishes with a read-only verification. Re-run the
verification any time with `sudo deploy/postgres/apply_hardening.sh --check`. Undo instructions are at the
top of the script.

## 3. Release, service user and units (approval: install)

```sh
sudo ./deploy/install.sh --dry-run    # review
sudo ./deploy/install.sh              # builds /opt/questersboard/releases/<version>-<stamp>, links current
```

It creates the `questersboard` system user, `/etc/questersboard/` (installs the example config only if none
exists), the release venv from `requirements.lock` with `--require-hashes`, `/usr/local/sbin/questersboard-admin`,
and the units (the four services plus the timed-refresh service and timer). It does **not** enable or start them, migrate, touch Nginx, or register commands.

Host-specific isolation drop-in (keeps QuestersBoard away from other services' files; kept outside the repo):

```sh
for u in web bot worker refresh; do
  sudo install -D -m 0644 10-isolation.conf /etc/systemd/system/questersboard-$u.service.d/10-isolation.conf
done
sudo systemctl daemon-reload
```

## 4. Config and credentials (approval: secrets)

Credentials are encrypted with the host key `/var/lib/systemd/credential.secret` (`--with-key=host`), which
works on any host. They can't be decrypted on another machine, which is why the plain values must also be
kept offline with the backup key.

Edit `/etc/questersboard/config.toml`: guild, application and channel IDs, role IDs, `public_base_url`,
branding. Then install credentials (see `configuration.md` for the list):

```sh
# 4a. Field key (generated on the host, never displayed) and database passwords from step 2
sudo install -m 0600 /dev/null /root/questersboard_db/field-key
sudo sh -c '/opt/questersboard/current/venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > /root/questersboard_db/field-key'
sudo systemd-creds encrypt --with-key=host --name=questersboard.field-key /root/questersboard_db/field-key /etc/credstore.encrypted/questersboard.field-key
for r in admin app worker; do
  sudo systemd-creds encrypt --with-key=host --name=questersboard.db-$r-password /root/questersboard_db/$r /etc/credstore.encrypted/questersboard.db-$r-password
done

# 4b. Discord secrets, typed into a hidden prompt (not echoed, not in shell history)
sudo -v
for n in discord-bot-token discord-client-secret; do
  read -rsp "$n: " v; echo
  printf '%s' "$v" | sudo systemd-creds encrypt --with-key=host --name=questersboard.$n - /etc/credstore.encrypted/questersboard.$n
  unset v
done

# 4c. Match the other credentials' mode, then check each decrypts (prints lengths only)
# (globs run inside sudo sh -c: the directory is root-only, so your own shell can't expand them)
sudo sh -c 'chmod 600 /etc/credstore.encrypted/questersboard.*'
sudo sh -c 'for f in /etc/credstore.encrypted/questersboard.*; do n=${f##*/}; printf "%s: " "$n"; systemd-creds decrypt --name="$n" "$f" - 2>/dev/null | wc -c; done'
# expected: field-key 45, db-*-password 65 each; Discord values: bot token ~70, client secret 32

# 4d. Keep an offline copy of the field key and all four database passwords (backup_restore.md), then:
sudo sh -c 'shred -u /root/questersboard_db/* && rmdir /root/questersboard_db'
sudo -u questersboard questersboard-admin check-config
```

`systemd-creds` may warn that `/var/lib/systemd/credential.secret` "is not located on encrypted media". The
credentials protect against copying files off the host, not against root on the host. `/etc/credstore.encrypted/` is shared with other services; the installer
creates it only if it is missing and never changes its owner or mode.

## 5. Discord application (approval: Discord)

In the Discord developer portal for the QuestersBoard application:
- OAuth2 redirect: `<public_base_url>/wow/quester/oauth/callback`; scope `identify` only.
- Bot: no privileged intents (QuestersBoard uses the Guilds and Voice States intents only). Optional
  item cards (`[item_links]`) need Message Content switched on under Bot > Privileged Gateway Intents,
  and Manage Webhooks in the watched channels.
- Invite with scopes `bot applications.commands` and permissions **84992**
  (View Channel 1024 + Send Messages 2048 + Embed Links 16384 + Read Message History 65536),
  limited to the event channel (and review channel) by channel overrides.

Optional Battle.net sync (ownership check plus "add my characters"): create an API client at
https://develop.battle.net/access/clients, register `<public_base_url>/wow/quester/battlenet/callback`
as its redirect URL, install its ID and secret as the `blizzard-client-id` and `blizzard-client-secret`
credentials (plus `field-key` for the encrypted list), then set `[blizzard] enabled = true` and
`battlenet_link_enabled = true`. For WoW: Forever the edition also needs the `forever` namespaces and
`api_status` from `deploy/config.example.toml`; check them with `questersboard-admin blizzard probe forever --region us`.

## 6. Schema migration (approval: production database migration)

```sh
sudo -u questersboard questersboard-admin migrate up --sql | less        # review the exact SQL
sudo systemd-run --pipe --wait -p User=questersboard \
  -p LoadCredentialEncrypted=questersboard.db-admin-password:/etc/credstore.encrypted/questersboard.db-admin-password \
  /usr/local/sbin/questersboard-admin migrate up
sudo systemd-run --pipe --wait -p User=questersboard \
  -p LoadCredentialEncrypted=questersboard.db-app-password:/etc/credstore.encrypted/questersboard.db-app-password \
  /usr/local/sbin/questersboard-admin seed-catalog          # race/class catalog, as the app role
```

## 7. Start services (approval: services)

```sh
sudo systemctl enable --now questersboard-web.socket questersboard-worker.service questersboard-bot.service
sudo curl -s --unix-socket /run/questersboard-web.sock http://questersboard/healthz; echo   # first request starts the web service
systemctl is-active questersboard-web.service questersboard-worker.service questersboard-bot.service
# full check (DB + outbox + web), with the app credential loaded for this one run:
sudo ./deploy/health.sh        # systemd-run with LoadCredentialEncrypted, as install.sh and rollback.sh use
sudo journalctl -u questersboard-bot -u questersboard-worker -u questersboard-web --since "5 min ago" --no-pager | tail -30
```

The bot logs in with its token but does nothing visible until it is invited to the server; it never
registers slash commands on its own (that is step 9). Backups are a separate step once the backup key exists:
`install.sh` does not install `deploy/backup/questersboard-backup.{service,timer}`; see `backup_restore.md`.

## 8. Nginx: prototype first (approval: Nginx)

One instance serves one host, so while testing on the prototype set `[community] public_base_url` to the
prototype host and add `https://PROTOTYPE_HOST/wow/quester/oauth/callback` as a second OAuth2 redirect in the
Discord portal (keep the live one). The prototype server block is already behind its sign-in gate; a
server-level `auth_request` is inherited by the QuestersBoard locations.

```sh
sudo install -m 0644 -o root -g root deploy/nginx/questersboard_locations.conf /etc/nginx/snippets/questersboard_locations.conf
# in the prototype server block (and in the template that generates it, if any):
#     include /etc/nginx/snippets/questersboard_location[s].conf;
# the [s] glob keeps `nginx -t` passing on hosts without the snippet.
sudo nginx -t && sudo systemctl reload nginx
```

Check: without a gate cookie, `https://PROTOTYPE_HOST/wow/quester/upcoming.json` redirects to the sign-in;
after signing in it returns JSON, and `/wow/chronicle/` shows the archive. The live host is unchanged
(its `/wow/quester/` is still the static site's 404). Rollback: remove the include line, `nginx -t`, reload.

## 8b. Upgrades with a new migration

`install.sh` installs a new release but never migrates. When a release adds a migration (0002 adds
`service_heartbeats` and `public_event_voice` for status.json), review and apply it as in step 6,
before restarting the services:

```sh
sudo -u questersboard questersboard-admin migrate up 0001:0002 --sql | less   # review, no password needed
# then the step 6 migrate command (approval: production database migration)
sudo systemctl restart questersboard-web.service questersboard-worker.service questersboard-bot.service
sudo curl -s --unix-socket /run/questersboard-web.sock http://questersboard/wow/quester/status.json; echo
```

`[status]` in config.toml is optional; the defaults are in `deploy/config.example.toml`.

## 8c. Hosts installed under the old name (wowquester)

Hosts installed before the rename run `deploy/rename_host.sh` once, from the checkout, after a backup,
with the services stopped. It renames the database and roles (SCRAM passwords survive a rename), the
pg_hba.conf lines, the Linux account, `/etc`, `/var/lib` and `/var/backups` folders and config.toml paths,
re-encrypts each `wowquester.<name>` credential as `questersboard.<name>`, swaps the units and admin
wrapper, and runs `install.sh` (new release in `/opt/questersboard`). Web addresses don't change.

```sh
sudo ./deploy/rename_host.sh                 # dry run: every step plus the config.toml diff
sudo systemctl stop wowquester-web.socket wowquester-web wowquester-bot wowquester-worker
sudo ./deploy/rename_host.sh --apply         # approval: database + services
sudo systemctl start questersboard-web.socket questersboard-web questersboard-worker questersboard-bot
sudo ./deploy/rename_host.sh --nginx --apply # approval: Nginx (new snippet, include line, nginx -t, reload)
sudo ./deploy/health.sh
```

Until the Nginx step, the site returns 502 (Nginx still points at the old socket), so run them back to
back. `sudo ./deploy/rename_host.sh --rollback` undoes everything; the old release in `/opt/wowquester`
and the old credential files are kept for that. Remove them once the rename has run for a while.

## 9. Slash commands (approval: register live Discord commands)

```sh
sudo -u questersboard questersboard-admin sync-commands            # dry-run: prints what would change
sudo -u questersboard env QUESTERSBOARD_APPROVED_CHANGE=sync-commands questersboard-admin sync-commands --yes
```

## 10. WoW page schedule (approval: site change, through the site's prototype → main flow)

Apply the site patch kept with the private deployment notes: it wraps the existing `#schedule` list
with `data-wq-upcoming` (the static list stays as the fallback) and adds `site_widget/wow_quester.js`.

## 11. Go live (approval: production)

Add the same `include` to the live site's `server {}` block, switch `public_base_url` and the Discord
redirect to the live host, restart the three services, `nginx -t`, reload.

No firewall or DNS changes are needed: the web service listens on a unix socket, PostgreSQL on localhost.
