# Configuration

All three services (`wowquester-web`, `wowquester-bot`, `wowquester-worker`) and the admin CLI read
one file: `/etc/wowquester/config.toml` (override with `WOWQUESTER_CONFIG`). Start from
`deploy/config.example.toml`; every key is commented there.

```
/etc/wowquester/                 root:wowquester  0750
├── config.toml                  root:wowquester  0640   settings, no secrets
├── backup_recipient             root:root        0644   GnuPG public-key fingerprint (backups)
└── secrets.env                  root:wowquester  0640   ONLY if systemd credentials are unavailable
/etc/credstore.encrypted/
└── wowquester.<name>            root:root        0600   host-key encrypted credentials
```

## Validation

Every service runs `wowquester-admin check-config --quiet` before starting (`ExecStartPre=`). On any
problem it prints what is wrong (section and key, never a value) and exits **78**; systemd's
`RestartPreventExitStatus=78` stops a restart loop. Checks include: required sections, quoted Discord
IDs, IANA time zones, positive limits and retention windows, `[web] bind` (`fd:N`, `unix:/path` or a
loopback address only), no `ptr` Blizzard namespaces, and edition/ruleset/role references. A missing
credential stops the service that needs it when it starts, with the credential's name only.

Run it by hand after editing (without `--quiet` it also lists which credentials are present, by fingerprint):

```sh
sudo -u wowquester /opt/wowquester/current/venv/bin/wowquester-admin check-config
```

## Live vs restart

| Section | Applies |
| --- | --- |
| `[roles]`, `[scheduling]`, `[signups]`, `[reminders]`, `[chronicle]`, `[rate_limits]` | **Live**: re-read about every 30 s when the file changes, only if the whole file validates. A bad edit is logged and the old settings stay in force. |
| everything else (`[community]`, `[discord]`, `[web]`, `[database]`, `[blizzard]`, `[ai]`, `[uploads]`, `[feeds]`, `[calendar]`, `[branding]`, `[retention]`, `[[editions]]`, presets, activities) | **Restart**: `sudo systemctl restart wowquester-web wowquester-bot wowquester-worker`. The services log which changed sections still need a restart. |

Each applied change is recorded in `config_changes` (section hashes, not values).

## Key settings

- **Roles** (`[roles]`): Discord role IDs (quoted strings) per capability: `event_creator`,
  `event_moderator`, `exclusive_designator`, `chronicle_editor`, `chronicle_reviewer`,
  `trusted_author`, `admin`. Removing a role in Discord takes effect at the member's next action;
  web sessions re-check roles (see `docs/permissions_data_model.md`).
- **Scheduling**: `max_simultaneous_wow_events` (default 1), duration limits, how far ahead,
  overrun grace, `reschedule_requires_reconfirm`, maximum recurrence count.
- **Editions** (`[[editions]]`): name, `uses_realms`, rulesets, regions, factions, max level, catalog,
  Blizzard namespaces with `{region}`, and `api_status` per capability. Forever ships realmless with
  empty namespaces and every capability `unsupported`. When Blizzard publishes Forever namespaces:
  `wowquester-admin blizzard probe forever --region us`, fill the namespaces, set statuses, restart.
- **Branding**: site name, nav links, the site's stylesheet and body class, so member pages and the
  chronicle look like the rest of the site. Community-specific values live only on the host.
- **AI** (`[ai]`): off by default. When on, only publication-safe text is sent (no Discord IDs,
  no opted-out names), output is only ever a draft suggestion, and any failure falls back to the
  built-in template.

## Credentials

| Name | Used by | Purpose |
| --- | --- | --- |
| `discord-bot-token` | web, bot, worker | gateway (bot), REST posts/DMs (worker), guild-member checks (web) |
| `discord-client-secret` | web | Discord login (OAuth `identify` scope) |
| `field-key` | web, bot, worker | Fernet key for encrypted fields |
| `db-app-password` | web, bot | `wowquester_app` |
| `db-worker-password` | worker | `wowquester_worker` |
| `db-admin-password` | admin CLI, backup | `wowquester_admin` (migrations; never loaded by a service) |
| `blizzard-client-id`, `blizzard-client-secret` | web, worker | Blizzard API (optional) |
| `ai-api-key` | worker | AI wording (optional) |

Install each one without it touching the command line or shell history:

```sh
sudo systemd-creds encrypt --name=wowquester.<name> - /etc/credstore.encrypted/wowquester.<name>
# type or paste the value, then Ctrl-D
```

Generate the field key with
`python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'`.
Lose it and the encrypted fields (Battle.net snapshots) can't be read; keep an offline copy with the
backup private key (see `backup_restore.md`).

Fallback: if `systemd-creds` isn't available, put `WOWQUESTER_SECRET_<NAME>=` lines (names in
`deploy/secrets.env.example`) in `/etc/wowquester/secrets.env` and add
`EnvironmentFile=/etc/wowquester/secrets.env` via a drop-in. That is weaker (every unit sees every
secret), so it is a fallback only.

For local development, `.env.example` lists the same variables with empty values.
