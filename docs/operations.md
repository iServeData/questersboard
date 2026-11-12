# Operations

## Services

| Unit | Runs | DB role | Notes |
| --- | --- | --- | --- |
| `questersboard-web.socket` | `/run/questersboard-web.sock` (group `www-data`, 0660) | | Nginx connects here |
| `questersboard-web.service` | FastAPI app (socket-activated, `fd:3`) | `questersboard_app` | member pages, chronicle, feeds, calendars |
| `questersboard-bot.service` | Discord gateway | `questersboard_app` | slash commands and buttons only |
| `questersboard-worker.service` | outbox, reminders, overrun nudges, projections, housekeeping | `questersboard_worker` | all Discord posts and DMs |
| `questersboard-backup.timer` | nightly encrypted backup | `questersboard_admin` | |

All run as the `questersboard` system user with systemd sandboxing (read-only system, private tmp, no new
privileges, restricted address families, `InaccessiblePaths=` for other services). Each unit only receives
the credentials it needs.

```sh
systemctl status 'questersboard-*'
journalctl -u questersboard-web -u questersboard-bot -u questersboard-worker --since -1h
sudo /opt/questersboard/current/deploy/health.sh       # DB + outbox + web socket (loads the app credential)
sudo -u questersboard questersboard-admin check-config    # after editing config.toml
```

## Routine tasks

| Task | How |
| --- | --- |
| Change roles, limits, reminders, chronicle rules | edit `config.toml`; live sections apply within ~30 s (see `configuration.md`) |
| Other config changes | edit, `check-config`, `systemctl restart questersboard-web questersboard-bot questersboard-worker` |
| See stuck Discord deliveries (with the last error for each dead one) | `questersboard-admin outbox status` |
| Retry dead deliveries (after fixing the cause) | `questersboard-admin outbox retry-dead` |
| Give up on dead deliveries that no longer matter | `questersboard-admin outbox dismiss-dead` |
| Health check | `sudo ./deploy/health.sh` fails on dead deliveries; `install.sh`/`rollback.sh` use `--deploy`, which only warns, so a Discord permission problem can't roll back a working release |
| Revoke someone's private calendar link | `questersboard-admin calendar-revoke <discord id>` |
| Check WoW channel permissions and slash-command registration (read-only) | `questersboard-admin discord-permissions` (needs the `discord-bot-token` credential; `--category <id>` to pick another category) |
| Remove a member who can't log in (same as "Remove me") | `questersboard-admin erase-member <discord id>` shows the plan; add `--yes` to carry it out |
| Prune old audit rows / signups / sessions | `questersboard-admin prune` (admin role; monthly is plenty) |
| Upload the role icons as the bot's Discord emojis | `questersboard-admin role-emojis` lists what is uploaded (read-only, needs the `discord-bot-token` credential); after approval `QUESTERSBOARD_APPROVED_CHANGE=role-emojis questersboard-admin role-emojis --yes` uploads the missing ones (never replaces); `--art DIR` for your own `<role key>.png` |
| Re-render current events' Discord posts (after a layout change) | `questersboard-admin refresh-posts` lists them (dry run); `--yes` queues in-place edits (app role credential, like `seed-catalog`) |
| Update item cards already posted (after an upgrade, or once Blizzard adds an item that wasn't found) | `questersboard-admin refresh-item-cards` lists the cards from the last 24 hours (`--hours N` for more; dry run); `--yes` looks the names up again and edits those cards in place. Nothing new is posted and nobody is pinged; a card someone removed stays removed. Older cards are only kept as long as `[retention]` keeps finished jobs (app role plus the Blizzard credentials, like `refresh-details`) |
| Refresh everyone's imported Blizzard details (after an upgrade changes how they're stored) | `questersboard-admin refresh-details` lists them (dry run); `--yes` refreshes each one with its owner's same groups and sharing, pausing `[blizzard] operator_refresh_pause_seconds` between characters (app role plus the Blizzard credentials). Characters without imported details are left alone; nothing is posted to Discord |
| Install the default event cover art | `questersboard-admin default-cover /path/to/art.png` (re-encoded to `covers/default_cover.webp` under `[uploads] storage_dir`) |
| Re-import Blizzard reference data | `questersboard-admin blizzard import-reference retail --region us` |
| Check whether Forever's API exists yet | `questersboard-admin blizzard probe forever --region us` (reports, changes nothing) |
| See which character pages Blizzard serves (equipment, talents, professions…) | `questersboard-admin blizzard probe-character forever --name "First Last" --region us` (Retail/Classic add `--realm`; `--detail specializations` shows one page's structure); read-only, uses the Blizzard credentials, saves nothing. In a guild, it also checks the guild and roster pages (sizes only) |
| Check whether gear icons, item tooltips and talent trees come straight from Blizzard | `questersboard-admin blizzard probe-media forever --name "First Last" --region us` (Retail/Classic add `--realm`); read-only, uses the Blizzard credentials, saves nothing. Icons are downloaded once from `[blizzard] render_image_hosts` only, to check type and size, then discarded. Add `--detail recipes` to show only the known-recipe checks (recipe data, what it makes, icon, and the item found by name) |
| Gear or talent icons missing | Press "Refresh from Blizzard" on My characters (imports before Build 1 have no icons). Icons need `[blizzard] detail_icons = true`; each import looks up at most `icon_lookups` icons. The site fetches them from `render_image_hosts` and keeps them in memory only (`icon_cache_hours`, up to 24; `icon_cache_entries`), so a restart refetches. Tooltip links follow `item_link_template`/`spell_link_template` per edition |
| Gear shows a race outline with "No picture from Blizzard yet" | Usually Blizzard hasn't made the character's picture yet (it does so some time after the character logs out in game); the next Refresh from Blizzard or `refresh-details` asks again. If Blizzard does have one (`blizzard probe-media`), the site fetches portraits itself from `[blizzard] render_image_hosts`: check the host can reach render.worldofwarcraft.com over HTTPS; the browser never needs to. The outlines are in `web/static/silhouettes/`, drawn by `tools/make_silhouettes.py` |
| Recipes show as plain names without icons | Press "Refresh from Blizzard" (recipes imported before recipe icons have none). Each import looks up at most `[blizzard] recipe_lookups` new recipes; later refreshes fill in the rest. A recipe with no crafted item and no item of the same name stays plain and is retried after a week. `blizzard probe-media … --detail recipes` shows what Blizzard serves for one recipe |
| A region shows "Not connected" right after a sync | "Connected" means characters verified through Battle.net in that region (or a list from the last `battlenet_import_minutes`). Lists are deleted after that and the verified mark after `data_ttl_days` (30); no Battle.net token is kept. Sync again |
| Battle.net panel lists only one game version, or names one as skipped | One login reads every edition with `ownership` on in that region. A skipped one didn't answer; sync again later. The multi-edition read uses one user token for every profile namespace: fixture-tested, unverified live |
| Picker sharing default / region names | `[blizzard] picker_default_share` (`"private"`, `"guild"` or `"public"`, default `"guild"`) and `[blizzard] region_labels` (e.g. `{ us = "Americas & Oceania" }`) |
| Change the page footer (wordmark, disclaimer, credits, footer links) | `[branding] wordmark`, `footer_notes`, `footer_credits`, `footer_nav` in `config.toml`, then restart the web service. Lines are plain text: a string, or `{ text, link, url, after }` for one link |
| Change the submenu under the main menu | `[branding] subnav_lead_label`, `subnav_lead` (list of `{ label, url }`, `[]` hides it), `subnav_label`, `subnav_label_url` in `config.toml`, then restart the web service. The Quests, Characters, Chronicle and Help dropdowns follow the app's pages |
| Which build is running | The footer's "Commit abc1234 · date" comes from `BUILD` in the release directory, written by `install.sh` from the checkout it installs; "Commit local" means it couldn't read git. What changed for members is on the Changelog page (`src/questersboard/web/changelog.toml`) |
| Check what an item card would show | `questersboard-admin blizzard probe-item forever --name "Item Name"`; read-only, uses the Blizzard credentials, saves nothing. Searches that edition, then `[item_links] fallbacks`, printing each search page, the exact-name matches, which edition answered and the card's JSON (`[item_links] region`) |
| Item cards don't appear | `journalctl -u questersboard-bot` names a refused Message Content intent (the bot stops with exit 78 until it's on in the Developer Portal or `[item_links] enabled = false`). `questersboard-admin outbox status` shows `item_links.post` errors: `403 50013` means the bot lacks Manage Webhooks (or View Channel) there. A deleted webhook is recreated on the next card. Names Blizzard doesn't serve get `not_found_text` (`say_not_found`). Blizzard's Forever item data is missing some real items (live check: item 13079 answers 404), so those come from `fallbacks` (Classic Era by default, footer says so) until Blizzard adds them; `blizzard probe-item` shows the search |
| Backup now | `sudo systemctl start questersboard-backup.service` |
| Restore drill | `backup_restore.md` |

## Health and failure behaviour

- **Discord down or rate-limited**: actions still succeed in the database; posts and DMs wait in the
  outbox and retry with backoff, honouring Discord's `retry_after`. After repeated failures a job is marked
  dead and shown by `outbox status`.
- **Event post deleted in Discord**: the worker recreates it on the next update.
- **Blizzard down**: lookups show "Blizzard's API didn't answer", nothing changes; planning continues with
  free text.
- **Database down**: `/healthz` returns 503; the WoW page keeps its static schedule; the bot answers
  "Something went wrong on our side. Please try again in a moment." and logs the error.
- **Bad config edit**: live reload keeps the old settings and logs the problem; a restart refuses to start
  (exit 78) instead of running with a broken config.
- **Event runs long**: managers get one DM after the grace period; nothing is extended automatically.

## Logs

journald only. Secrets are wrapped so accidental logging shows a fingerprint; Discord tokens and
`Authorization` headers are redacted by a log filter. Audit history is in the `audit_log` table
(who, what, when, before/after summary), not in journald.
