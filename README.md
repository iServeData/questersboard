# WoWQuester

WoWQuester is a Discord-connected World of Warcraft companion for one gaming community:

- **Character roster** per member (WoW: Forever and Retail), with optional Blizzard checks.
- **Event scheduler** with role slots, open or approval signups, waitlists, reminders and a
  community-wide limit on simultaneous WoW events (default 1) plus per-member conflict checks.
- **Moderated chronicle**: after an event, organizers write what happened; reviewers approve it;
  approved stories appear on the public site with an archive, story pages, a JSON feed and RSS/Atom.
  Players can opt out of being named. Nothing is published without review (or a trusted author).

It runs as three small services on one host (web, Discord bot, worker) sharing one PostgreSQL
database and one config file. The community's static website stays static: the WoW page reads
upcoming events from a small JSON endpoint with a static fallback.

| Path | What it is |
| --- | --- |
| `src/wowquester/` | Application (domain logic, web app, bot, worker, admin CLI) |
| `deploy/` | Example config, systemd units, Nginx snippet, PostgreSQL roles/hardening, backup scripts |
| `site_widget/` | Drop-in script for the site's WoW page schedule |
| `docs/` | Plan, configuration, install, operations, guides, matrices, test results |
| `tests/` | pytest suite (needs a disposable local PostgreSQL; see `docs/testing.md`) |
| `tools/` | Local preview server (fixture data) and the repository sanitization check |

Start with [`docs/plan.md`](docs/plan.md), then [`docs/install.md`](docs/install.md).

## Quick local run (fixture data, no Discord)

```sh
uv venv -p 3.12 .venv && uv pip install -r requirements.lock && uv pip install -e . --no-deps
# a disposable PostgreSQL with the four roles (docs/testing.md), then:
WOWQUESTER_PREVIEW_DB=postgresql+psycopg://wowquester_app:${WQT_APP_PW}@127.0.0.1:5433/wowquester \
WOWQUESTER_PREVIEW_ADMIN_DB=postgresql+psycopg://wowquester_admin:${WQT_ADMIN_PW}@127.0.0.1:5433/wowquester \
.venv/bin/python tools/preview_server.py --port 8740 --reset
```

## Ground rules baked in

- No secrets in the repository or in `config.toml`; secrets are systemd credentials.
- Community-specific names, IDs and URLs live only in the host's `/etc/wowquester/config.toml`.
- WoW: Forever has no published Blizzard API namespace yet. WoWQuester says so instead of
  falling back to Retail or Classic data, and never uses `classic-ptr` namespaces.
- A Blizzard lookup proves a character exists, not that a member owns it. Ownership needs the
  optional Battle.net login.

License: MIT.
