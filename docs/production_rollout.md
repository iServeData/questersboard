# Production rollout

What is left between "tested on the prototype host" and "serving the public site". Each step needs its own
approval (see `install.md` for the full commands). Names below are placeholders: `LIVE_HOST` is the public
site, `PROTOTYPE_HOST` the gated prototype.

One QuestersBoard instance serves one host (`[community] public_base_url`). Going live moves it from the
prototype to the live host; the prototype keeps its gate but its QuestersBoard links then lead to the live host.

| # | Step | Where | Approval | Rollback |
| --- | --- | --- | --- | --- |
| 1 | Host checkout follows `main`: `git -C ~/wowquester switch main && git pull`, then `sudo ./deploy/install.sh` | host | install | `switch prototype`, reinstall |
| 2 | Tidy test data: cancel test events on their event pages and remove test characters, so the first public card isn't a test | QuestersBoard | none (your data) | - |
| 3 | Backups: create the GPG key, install the public key, enable `wowquester-backup.timer`, run one backup and the restore drill (`backup_restore.md`) | host | backups | disable the timer |
| 4 | Discord portal: add `https://LIVE_HOST/wow/quester/oauth/callback` as an OAuth2 redirect (keep the prototype one until step 9) | Discord | Discord | remove the redirect |
| 5 | Config: `[community] public_base_url = "https://LIVE_HOST"`; restart web, bot, worker | host | production config | previous value, restart |
| 6 | Nginx: add `include /etc/nginx/snippets/wowquester_location[s].conf;` to the live `server {}` (and the template that generates it); `nginx -t`, reload | host | Nginx | remove the line, reload |
| 7 | Site: merge the site's `prototype` into `main` (WoW page cards, Muster list, "Using QuestersBoard", links to `/wow/quester/` and `/wow/chronicle/`). Do this after step 6 so those links never 404 | site repo | publish | revert the merge on `main` |
| 8 | Checks: `deploy/health.sh`; `https://LIVE_HOST/wow/quester/status.json` returns JSON without a sign-in; Discord login works; a published event card links to `LIVE_HOST`; `/wow/` status card shows Online | host, browser | none | steps 6, 5 |
| 9 | Discord: limit slash commands to the bot channel (Integrations); announce in Discord; optionally remove the prototype OAuth redirect | Discord | publish | - |

Steps 5, 6 and 8 are one command, `deploy/go_live.sh` (checks first, backs up, changes, `nginx -t` with
automatic restore, restarts, then checks the live host):

```sh
sudo ./deploy/go_live.sh --url https://LIVE_HOST --site-file /etc/nginx/sites-available/SITE \
     --after 'include /etc/nginx/snippets/AN_EXISTING_INCLUDE.conf;'
sudo ./deploy/go_live.sh --rollback /root/wowquester_go_live_STAMP.tgz     # undo
```

Not needed: firewall or DNS changes (the web service listens on a unix socket; PostgreSQL on localhost), new
migrations (0002 is the current head), new slash-command registration (unchanged since the last sync).

Still optional after launch: Blizzard checks (`[blizzard] enabled`), the AI wording helper (`[ai] enabled`),
RSVP-only voice channels (follow-up FU-1).
