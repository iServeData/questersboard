# Configuration

All three services (`questersboard-web`, `questersboard-bot`, `questersboard-worker`) and the admin CLI read
one file: `/etc/questersboard/config.toml` (override with `QUESTERSBOARD_CONFIG`). Start from
`deploy/config.example.toml`; every key is commented there.

```
/etc/questersboard/                 root:questersboard  0750
├── config.toml                  root:questersboard  0640   settings, no secrets
├── backup_recipient             root:root        0644   GnuPG public-key fingerprint (backups)
└── secrets.env                  root:questersboard  0640   ONLY if systemd credentials are unavailable
/etc/credstore.encrypted/
└── questersboard.<name>            root:root        0600   host-key encrypted credentials
```

## Validation

Every service runs `questersboard-admin check-config --quiet` before starting (`ExecStartPre=`). On any
problem it prints what is wrong (section and key, never a value) and exits **78**; systemd's
`RestartPreventExitStatus=78` stops a restart loop. Checks include: required sections, quoted Discord
IDs, IANA time zones, positive limits and retention windows, `[web] bind` (`fd:N`, `unix:/path` or a
loopback address only), no `ptr` Blizzard namespaces, and edition/ruleset/role references. A missing
credential stops the service that needs it when it starts, with the credential's name only.

Run it by hand after editing (without `--quiet` it also lists which credentials are present, by fingerprint):

```sh
sudo -u questersboard /opt/questersboard/current/venv/bin/questersboard-admin check-config
```

## Live vs restart

| Section | Applies |
| --- | --- |
| `[roles]`, `[scheduling]`, `[signups]`, `[reminders]`, `[chronicle]`, `[rate_limits]`, `[event_posts]` | **Live**: re-read about every 30 s when the file changes, only if the whole file validates. A bad edit is logged and the old settings stay in force. |
| everything else (`[community]`, `[discord]`, `[web]`, `[database]`, `[blizzard]`, `[ai]`, `[uploads]`, `[feeds]`, `[calendar]`, `[branding]`, `[retention]`, `[event_threads]`, `[voice]`, `[item_links]`, `[[editions]]`, presets, activities) | **Restart**: `sudo systemctl restart questersboard-web questersboard-bot questersboard-worker`. The services log which changed sections still need a restart. |

Each applied change is recorded in `config_changes` (section hashes, not values).

## Key settings

- **Roles** (`[roles]`): Discord role IDs (quoted strings) per capability: `event_creator`,
  `event_moderator`, `exclusive_designator`, `chronicle_editor`, `chronicle_reviewer`,
  `trusted_author`, `admin`. Removing a role in Discord takes effect at the member's next action;
  web sessions re-check roles (see `docs/permissions_data_model.md`).
- **Scheduling**: `max_simultaneous_wow_events` (default 1), duration limits, how far ahead,
  overrun grace, `reschedule_requires_reconfirm`, `end_change_notice_only_minutes` (default 0; e.g. 30
  means moving only the end time by up to 30 minutes sends a notice without asking for reconfirmation),
  maximum recurrence count.
- **Time zones** (`[community]`): `default_timezone` (IANA name) and optional `common_timezones`, the
  zones listed first in the time zone pickers. The full list is the server's IANA tzdata (no API calls).
- **Editions** (`[[editions]]`): name, `uses_realms`, rulesets, regions, factions, max level, catalog,
  Blizzard namespaces with `{region}`, and `api_status` per capability. Forever ships realmless with
  empty namespaces and every capability `unsupported`. When Blizzard publishes Forever namespaces:
  `questersboard-admin blizzard probe forever --region us`, fill the namespaces, set statuses, restart.
  Forever rulesets are `normal`, `pvp`, `rp`, `rp_pvp` (add `hardcore` when it opens). Ruleset labels
  come from `ruleset_labels` (optional) or built-ins (`rp_pvp` shows as RP-PvP).
  The example also has three Classic editions that use realms: `classic_era` (Era, Hardcore, Season of
  Discovery; level 60, `classic1x` namespaces), `classic_anniversary` (Burning Crusade Anniversary; 70,
  `classicann`) and `mists_classic` (90, `classic`). Their race/class and "Where" lists are packaged seeds
  marked unverified: after adding an edition run `questersboard-admin seed-catalog`. Editions load at restart.
  Retail uses `catalog = "retail"` (packaged race/class seed, unverified); a Blizzard import adds
  dungeons and raids once the API is enabled.
- **Difficulties** (`[[editions]] difficulties`): the event form's Difficulty list per edition, e.g.
  Retail `["Normal", "Heroic", "Mythic", "Mythic+", "Raid Finder"]`. `[]` hides the choice for editions
  with a single difficulty (WoW: Forever, Classic Era); saving an event there clears an old value.
  Optional; when the key is missing the list is Normal only. Loads at restart. When a
  Blizzard import has read an instance's modes, the form marks the ones that instance doesn't offer.
- **Event posts** (`[event_posts]`, live): `faction_colors` (hex per `horde`, `alliance`, `any`),
  `field_spacing` (default true: a blank line between the post's headings),
  `default_cover` (a file in `<[uploads] storage_dir>/covers`, installed with
  `questersboard-admin default-cover FILE`) and optional `activity_covers` per activity type. A missing file
  just means no image. `role_icons` maps a role key to one of the bot's own Discord emoji names
  (default `{ tank = "qb_tank", healer = "qb_healer", damage = "qb_damage" }`; `{}` turns icons off on the
  post and the event page) and `role_icon_position` puts the icon before the role's heading (`"heading"`,
  default) or at the start of its line of names (`"line"`). Upload the pictures with
  `questersboard-admin role-emojis`; until then roles are plain text.
- **Event chat threads** (`[event_threads]`, restart): `enabled` (default false), `private` (invite-only
  threads), `archive_after_hours` (24), `reminders_in_thread` (true), `name_suffix`. The bot needs Create
  Public (or Private) Threads, Send Messages in Threads and Manage Threads in the event channel; run
  `questersboard-admin discord-permissions` to check.
- **Event voice channels** (`[voice]`, restart): `enabled` (default false), `category_id`, `name_template`
  (`{title}`, `{id}`), `user_limit` (0 = no cap), `empty_teardown_minutes` (5), `member_states`
  (confirmed, tentative), `moderators_can_join` (true), `staff_role_ids` (more server roles, such as admins,
  that can join every event voice channel), `remove_on_leave` (false: someone who leaves the event may stay
  while connected but can't rejoin; true disconnects them). The bot needs, on that category only:
  Manage Channels, Manage Permissions, Move Members, View Channel, Connect, Speak, Video, Use Voice Activity,
  Send Messages and Read Message History. `questersboard-admin discord-permissions` checks them.
  Guest links and host moderation: `guest_invites` (true; `/voice invite`), `max_guests_per_invite` (5, 1-25),
  `invite_dm` (`off` | `hosts` | `everyone`: DM guest links on Start), `guest_link_template` (empty = the
  site's `/v/{code}` page under member_path; e.g. `https://short.example.org/v/{code}` with the `/v/` Nginx
  rule), `protected_role_ids` (roles hosts can't `/voice kick` or `/voice ban`; the server owner, bots,
  `staff_role_ids` and `[roles] admin` are always protected). Guests need the bot's role to have **Create
  Invite** (server level) so they can be added to the server; the permissions check reports it.
- **Group sizes** (`[signups.party_size_max]`): the largest group per activity type (quest 5, dungeon 5,
  arena 5, raid 40, battleground 40; others 80). The participant limit can't go above it, a blank limit
  becomes it, and an event's role "How many" numbers can't add up to more than the limit. Live setting.
- **Role presets** (`[[role_presets]]`): Tank, Healer, Damage. Hosts can still add custom roles.
- **Branding**: site name, nav links, the site's stylesheet and body class, so member pages and the
  chronicle look like the rest of the site. Community-specific values live only on the host.
- **AI** (`[ai]`): off by default. When on, only publication-safe text is sent (no Discord IDs,
  no opted-out names), output is only ever a draft suggestion, and any failure falls back to the
  built-in template.
- **Item cards** (`[item_links]`): off by default. When on, a message in a `watch_channels` channel
  that names items in [square brackets] gets one card per item (at most `max_per_message`), posted by a
  webhook the bot creates in that channel (or `post_channel`) called `webhook_name`. Webhook posts,
  because Discord refuses bot posts in a channel linked to in-game guild chat (403, code 20062). The card
  has no link; its details come from Blizzard's item data for `edition`/`region` (exact name, any letter
  case; with several items of that name the lowest ID wins, searching at most `search_pages` pages of
  100). `cooldown_minutes` skips the same name in the same channel; `say_not_found` adds a line for
  names with no item; `ignore_bots` skips other bots (not webhooks). Only the names and message IDs are
  queued, never the message text or author. Needs the Message Content intent (Developer Portal), View
  Channel, Read Message History and Manage Webhooks in the channel, and `[blizzard] enabled`. Restart
  section.

## Credentials

| Name | Used by | Purpose |
| --- | --- | --- |
| `discord-bot-token` | web, bot, worker | gateway (bot), REST posts/DMs (worker), guild-member checks (web) |
| `discord-client-secret` | web | Discord login (OAuth `identify` scope) |
| `field-key` | web, bot, worker | Fernet key for encrypted fields |
| `db-app-password` | web, bot | `questersboard_app` |
| `db-worker-password` | worker | `questersboard_worker` |
| `db-admin-password` | admin CLI, backup | `questersboard_admin` (migrations; never loaded by a service) |
| `blizzard-client-id`, `blizzard-client-secret` | web, worker | Blizzard API (optional) |
| `ai-api-key` | worker | AI wording (optional) |

Install each one without it touching the command line or shell history:

```sh
sudo systemd-creds encrypt --with-key=host --name=questersboard.<name> - /etc/credstore.encrypted/questersboard.<name>
# type or paste the value, then Ctrl-D
```

Generate the field key with
`python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'`.
Lose it and the encrypted fields (Battle.net snapshots) can't be read; keep an offline copy with the
backup private key (see `backup_restore.md`).

Fallback: if `systemd-creds` isn't available, put `QUESTERSBOARD_SECRET_<NAME>=` lines (names in
`deploy/secrets.env.example`) in `/etc/questersboard/secrets.env` and add
`EnvironmentFile=/etc/questersboard/secrets.env` via a drop-in. That is weaker (every unit sees every
secret), so it is a fallback only.

For local development, `.env.example` lists the same variables with empty values.
