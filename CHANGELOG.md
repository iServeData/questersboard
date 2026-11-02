# Changelog

## 0.1.0 (2026-11-02)

First release, tested on the community's protected prototype host (Discord login, slash commands,
event cards, status cards). Not yet serving the public site; see `docs/production_rollout.md`.

- **Characters**: roster per member for WoW: Forever and Retail. Race and class suggestions follow
  edition, faction and race; free text accepted, only catalog-ruled-out pairs refused. Click a name to
  edit in place. Edits can't strand signups: one "Leave these events and save" step, or "Add it as a
  new character" once the edition is fixed by event history. Optional Blizzard checks (off by default).
- **Events**: drafts, publish, copy (same local time a week later), edit with reconfirmation, role
  slots with one blank row plus "Add more", open or approval signups, waitlists, co-hosts, repeating
  series, a community-wide limit on simultaneous WoW events and per-member conflict checks. The page
  explains why each character can or can't join. Organizer-started Live status with an overtime
  reminder and "Extend 30 min"; nothing closes on its own. Attendance after the event.
- **Discord**: `/quest list|my|join|leave|create` and `/character add|list|edit|remove`, event cards
  with Join / Tentative / Leave buttons, reminders. `/character add` never loses what was typed: a
  missing or impossible race or class gets a picker, other mistakes reopen the form filled in. Replies
  carry no link previews.
- **Chronicle**: draft after each event, screenshots (re-encoded, metadata stripped), review by
  someone other than the author, trusted authors, public archive, story pages and JSON/RSS/Atom feeds.
  Nobody is named without opting in; "stop naming me" works on published stories.
- **Calendars**: public calendar feed and revocable private links.
- **Privacy**: public privacy and terms pages from config, with a sticky table of contents. Remove me:
  typed "remove me" confirmation, withdraws signups, cancels hosted events, deletes characters,
  anonymises Chronicle text and disconnects the login, then shows a receipt. `wowquester-admin
  erase-member` for organizers.
- **Site**: `status.json` for the community site's status and next-event cards, bot and worker
  heartbeats.
- **Operations**: `deploy/install.sh` (root-owned code, hash-pinned installs, rollback on failure),
  PostgreSQL roles and pg_hba hardening, systemd units with encrypted credentials, `deploy/health.sh`,
  outbox status and dead-letter handling, `wowquester-admin discord-permissions` (read-only permission
  report) and `sync-commands` (approval-gated).
- **Login return address**: rebuilt from the member-area prefix and a checked remainder (CodeQL
  `py/url-redirection`); `tests/test_login_next.py`.
- **Public repository**: `CONTRIBUTING.md`; `SECURITY.md` points to private vulnerability reporting.
- **Go live**: `deploy/go_live.sh` switches the public address and adds the live Nginx include with
  checks, a backup, automatic restore on a failed `nginx -t`, live-host checks and `--rollback`.
- **Repository**: tests workflow (full suite against PostgreSQL with production roles; database tests
  must run, not skip), Dependabot, SECURITY.md, sanitization check with a private denylist.
