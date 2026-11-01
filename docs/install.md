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
sudo apt install postgresql-16 python3.12-venv gnupg
```

## 2. PostgreSQL hardening and roles (approval: database)

Review `docs/db_hardening_checklist.md` first. Then:

```sh
sudo install -o postgres -g postgres -m 0640 deploy/postgres/pg_hba.conf /etc/postgresql/16/main/pg_hba.conf
sudo install -o postgres -g postgres -m 0644 deploy/postgres/postgresql_wowquester.conf \
     /etc/postgresql/16/main/conf.d/50-wowquester.conf
sudo systemctl restart postgresql
# four random passwords, kept only as encrypted credentials (step 4) and passed to psql once:
umask 077; for r in admin app worker readonly; do openssl rand -base64 33 > /root/wq_$r; done
sudo -s     # root shell, so the passwords go through the environment, not the command line
set -a; WQ_ADMIN_PW=$(cat /root/wq_admin) WQ_APP_PW=$(cat /root/wq_app) \
        WQ_WORKER_PW=$(cat /root/wq_worker) WQ_READONLY_PW=$(cat /root/wq_readonly); set +a
sudo -u postgres --preserve-env=WQ_ADMIN_PW,WQ_APP_PW,WQ_WORKER_PW,WQ_READONLY_PW \
     psql -v ON_ERROR_STOP=1 -f deploy/postgres/roles.sql
unset WQ_ADMIN_PW WQ_APP_PW WQ_WORKER_PW WQ_READONLY_PW; exit
```

If the host already runs PostgreSQL for something else, merge the `pg_hba.conf` lines instead of
replacing the file (the WoWQuester lines are marked) and check `listen_addresses` stays `localhost`.

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

Edit `/etc/wowquester/config.toml`: guild, application and channel IDs, role IDs, `public_base_url`,
branding. Then install credentials (see `configuration.md` for the list):

```sh
for n in discord-bot-token discord-client-secret field-key; do
  sudo systemd-creds encrypt --name=wowquester.$n - /etc/credstore.encrypted/wowquester.$n
done
for r in admin app worker; do
  sudo systemd-creds encrypt --name=wowquester.db-$r-password /root/wq_$r /etc/credstore.encrypted/wowquester.db-$r-password
done
sudo shred -u /root/wq_admin /root/wq_app /root/wq_worker /root/wq_readonly   # readonly is only needed by tooling
sudo -u wowquester wowquester-admin check-config
```

## 5. Discord application (approval: Discord)

In the Discord developer portal for the WoWQuester application:
- OAuth2 redirect: `<public_base_url>/wow/quester/oauth/callback`; scope `identify` only.
- Bot: no privileged intents (WoWQuester uses the Guilds intent only).
- Invite with scopes `bot applications.commands` and permissions **84992**
  (View Channel 1024 + Send Messages 2048 + Embed Links 16384 + Read Message History 65536),
  limited to the event channel (and review channel) by channel overrides.

Optional Battle.net ownership link: register `<public_base_url>/wow/quester/battlenet/callback`
in the Blizzard developer portal and set `[blizzard] battlenet_link_enabled = true`.

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
sudo wowquester-admin health --socket /run/wowquester-web.sock
sudo systemctl enable --now wowquester-backup.timer      # after the backup recipient key is set up
```

## 8. Nginx: prototype first (approval: Nginx)

Include `deploy/nginx/wowquester_locations.conf` inside the **prototype** site's `server {}` block
(already behind its sign-in gate), with `public_base_url` pointing at the prototype host:

```sh
sudo install -m 0644 deploy/nginx/wowquester_locations.conf /etc/nginx/snippets/wowquester_locations.conf
# add:  include snippets/wowquester_locations.conf;   to the prototype server block
sudo nginx -t && sudo systemctl reload nginx
```

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
