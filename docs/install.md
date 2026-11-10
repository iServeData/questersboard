# Installing WoWQuester

**Nothing in this file has been run on the production host.** Each numbered step changes the host
and needs explicit approval. Steps are ordered so the community site and other services keep running
throughout; WoWQuester starts in the prototype environment first and goes live only after review.

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

Preflight on 2026-10-31: Python 3.12.3, systemd 255, nginx 1.24.0 and `python3.12-venv` present;
PostgreSQL not installed and nothing on port 5432, so the full `pg_hba.conf` can be installed (no merge).

```sh
sudo apt install postgresql-16          # python3.12-venv and gnupg are already present
```

## 2. PostgreSQL hardening and roles (approval: database)

Review `docs/db_hardening_checklist.md` first. On the host, from a checkout of the `prototype` branch:

```sh
ORG=your-github-org     # the GitHub organization that hosts this repository
git clone -b prototype "https://github.com/$ORG/wowquester.git" ~/wowquester   # private: uses your GitHub login
cd ~/wowquester
less deploy/postgres/apply_hardening.sh deploy/postgres/pg_hba.conf deploy/postgres/postgresql_wowquester.conf
sudo deploy/postgres/apply_hardening.sh
```

The script backs up the existing `pg_hba.conf` (as `pg_hba.conf.pre_wowquester`), installs the proposed
`pg_hba.conf` and `conf.d/50-wowquester.conf`, restarts `postgresql@16-main`, generates four random
passwords in `/root/wowquester_db/` (0700, until step 4), creates the roles and the `wowquester` database
with statement logging off for that session, and finishes with a read-only verification. Re-run the
verification any time with `sudo deploy/postgres/apply_hardening.sh --check`. Undo instructions are at the
top of the script.

## 3. Release, service user and units (approval: install)

```sh
sudo ./deploy/install.sh --dry-run    # review
sudo ./deploy/install.sh              # builds /opt/wowquester/releases/<version>-<stamp>, links current
```

It creates the `wowquester` system user, `/etc/wowquester/` (installs the example config only if none
exists), the release venv from `requirements.lock` with `--require-hashes`, `/usr/local/sbin/wowquester-admin`,
and the four units. It does **not** enable or start them, migrate, touch Nginx, or register commands.

Host-specific isolation drop-in (keeps WoWQuester away from other services' files; kept outside the repo):

```sh
for u in web bot worker; do
  sudo install -D -m 0644 10-isolation.conf /etc/systemd/system/wowquester-$u.service.d/10-isolation.conf
done
sudo systemctl daemon-reload
```

## 4. Config and credentials (approval: secrets)

The host has no usable TPM (`systemd-creds has-tpm2` → partial), so credentials are bound to the host key
`/var/lib/systemd/credential.secret` (`--with-key=host`). They can't be decrypted on another machine, which is
why the plain values must also be kept offline with the backup key.

Edit `/etc/wowquester/config.toml`: guild, application and channel IDs, role IDs, `public_base_url`,
branding. Then install credentials (see `configuration.md` for the list):

```sh
# 4a. Field key (generated on the host, never displayed) and database passwords from step 2
sudo install -m 0600 /dev/null /root/wowquester_db/field-key
sudo sh -c '/opt/wowquester/current/venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > /root/wowquester_db/field-key'
sudo systemd-creds encrypt --with-key=host --name=wowquester.field-key /root/wowquester_db/field-key /etc/credstore.encrypted/wowquester.field-key
for r in admin app worker; do
  sudo systemd-creds encrypt --with-key=host --name=wowquester.db-$r-password /root/wowquester_db/$r /etc/credstore.encrypted/wowquester.db-$r-password
done

# 4b. Discord secrets, typed into a hidden prompt (not echoed, not in shell history)
sudo -v
for n in discord-bot-token discord-client-secret; do
  read -rsp "$n: " v; echo
  printf '%s' "$v" | sudo systemd-creds encrypt --with-key=host --name=wowquester.$n - /etc/credstore.encrypted/wowquester.$n
  unset v
done

# 4c. Match the other credentials' mode, then check each decrypts (prints lengths only)
# (globs run inside sudo sh -c: the directory is root-only, so your own shell can't expand them)
sudo sh -c 'chmod 600 /etc/credstore.encrypted/wowquester.*'
sudo sh -c 'for f in /etc/credstore.encrypted/wowquester.*; do n=${f##*/}; printf "%s: " "$n"; systemd-creds decrypt --name="$n" "$f" - 2>/dev/null | wc -c; done'
# expected: field-key 45, db-*-password 65 each; Discord values: bot token ~70, client secret 32

# 4d. Keep an offline copy of the field key and all four database passwords (backup_restore.md), then:
sudo sh -c 'shred -u /root/wowquester_db/* && rmdir /root/wowquester_db'
sudo -u wowquester wowquester-admin check-config
```

`systemd-creds` warns that `/var/lib/systemd/credential.secret` "is not located on encrypted media". That is
expected on this host (no TPM, no disk encryption): the credentials protect against copying files off the
host, not against root on the host. `/etc/credstore.encrypted/` is shared with other services; the installer
creates it only if it is missing and never changes its owner or mode.

## 5. Discord application (approval: Discord)

In the Discord developer portal for the WoWQuester application:
- OAuth2 redirect: `<public_base_url>/wow/quester/oauth/callback`; scope `identify` only.
- Bot: no privileged intents (WoWQuester uses the Guilds intent only).
- Invite with scopes `bot applications.commands` and permissions **84992**
  (View Channel 1024 + Send Messages 2048 + Embed Links 16384 + Read Message History 65536),
  limited to the event channel (and review channel) by channel overrides.

Optional Battle.net sync (ownership check plus "add my characters"): create an API client at
https://develop.battle.net/access/clients, register `<public_base_url>/wow/quester/battlenet/callback`
as its redirect URL, install its ID and secret as the `blizzard-client-id` and `blizzard-client-secret`
credentials (plus `field-key` for the encrypted list), then set `[blizzard] enabled = true` and
`battlenet_link_enabled = true`. For WoW: Forever the edition also needs the `forever` namespaces and
`api_status` from `deploy/config.example.toml`; check them with `wowquester-admin blizzard probe forever --region us`.

## 6. Schema migration (approval: production database migration)

```sh
sudo -u wowquester wowquester-admin migrate up --sql | less        # review the exact SQL
sudo systemd-run --pipe --wait -p User=wowquester \
  -p LoadCredentialEncrypted=wowquester.db-admin-password:/etc/credstore.encrypted/wowquester.db-admin-password \
  /usr/local/sbin/wowquester-admin migrate up
sudo systemd-run --pipe --wait -p User=wowquester \
  -p LoadCredentialEncrypted=wowquester.db-app-password:/etc/credstore.encrypted/wowquester.db-app-password \
  /usr/local/sbin/wowquester-admin seed-catalog          # race/class catalog, as the app role
```

## 7. Start services (approval: services)

```sh
sudo systemctl enable --now wowquester-web.socket wowquester-worker.service wowquester-bot.service
sudo curl -s --unix-socket /run/wowquester-web.sock http://wowquester/healthz; echo   # first request starts the web service
systemctl is-active wowquester-web.service wowquester-worker.service wowquester-bot.service
# full check (DB + outbox + web), with the app credential loaded for this one run:
sudo ./deploy/health.sh        # systemd-run with LoadCredentialEncrypted, as install.sh and rollback.sh use
sudo journalctl -u wowquester-bot -u wowquester-worker -u wowquester-web --since "5 min ago" --no-pager | tail -30
```

The bot logs in with its token but does nothing visible until it is invited to the server; it never
registers slash commands on its own (that is step 9). Backups are a separate step once the backup key exists:
`install.sh` does not install `deploy/backup/wowquester-backup.{service,timer}`; see `backup_restore.md`.

## 8. Nginx: prototype first (approval: Nginx)

One instance serves one host, so while testing on the prototype set `[community] public_base_url` to the
prototype host and add `https://PROTOTYPE_HOST/wow/quester/oauth/callback` as a second OAuth2 redirect in the
Discord portal (keep the live one). The prototype server block is already behind its sign-in gate; a
server-level `auth_request` is inherited by the WoWQuester locations.

```sh
sudo install -m 0644 -o root -g root deploy/nginx/wowquester_locations.conf /etc/nginx/snippets/wowquester_locations.conf
# in the prototype server block (and in the template that generates it, if any):
#     include /etc/nginx/snippets/wowquester_location[s].conf;
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
sudo -u wowquester wowquester-admin migrate up 0001:0002 --sql | less   # review, no password needed
# then the step 6 migrate command (approval: production database migration)
sudo systemctl restart wowquester-web.service wowquester-worker.service wowquester-bot.service
sudo curl -s --unix-socket /run/wowquester-web.sock http://wowquester/wow/quester/status.json; echo
```

`[status]` in config.toml is optional; the defaults are in `deploy/config.example.toml`.

## 9. Slash commands (approval: register live Discord commands)

```sh
sudo -u wowquester wowquester-admin sync-commands            # dry-run: prints what would change
sudo -u wowquester env WOWQUESTER_APPROVED_CHANGE=sync-commands wowquester-admin sync-commands --yes
```

## 10. WoW page schedule (approval: site change, through the site's prototype → main flow)

Apply the site patch kept with the private deployment notes: it wraps the existing `#schedule` list
with `data-wq-upcoming` (the static list stays as the fallback) and adds `site_widget/wow_quester.js`.

## 11. Go live (approval: production)

Add the same `include` to the live site's `server {}` block, switch `public_base_url` and the Discord
redirect to the live host, restart the three services, `nginx -t`, reload.

No firewall or DNS changes are needed: the web service listens on a unix socket, PostgreSQL on localhost.
