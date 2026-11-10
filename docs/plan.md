# QuestersBoard plan and architecture

Status: implemented and tested locally. Nothing has been deployed. Every production step in
`docs/install.md` needs separate approval.

## Findings that shaped the design

**Existing host and site (inspected before coding).**
- The community site is plain static HTML (no Hugo) published from Git by server-side timers.
  Routine chronicle publication therefore must not touch the site repository at all: QuestersBoard
  serves `/wow/chronicle/` dynamically from its own database through the existing Nginx.
- The WoW page already has a hand-written schedule list. It keeps that list as the no-JS /
  service-down fallback; `site_widget/wow_quester.js` replaces it with upcoming QuestersBoard events
  when `/wow/quester/upcoming.json` answers. That page change is a separate site patch that goes
  through the site's normal prototype review.
- Other community bots and services on the host have their own users, units and paths. QuestersBoard
  gets its own OS user, units, database, socket and config directory, and its units hide the other
  services' paths (`InaccessiblePaths=`, plus a host-specific isolation drop-in).
- The site's shared schedule data file drives other games' announcers. QuestersBoard does not write it;
  WoW events come only from QuestersBoard, so they are never double-announced.

**Blizzard API (checked 2026-10-31).**
- WoW: Forever has no published API namespace yet; community developers are still asking Blizzard
  for one ([Blizzard forum: Forever API namespace](https://us.forums.blizzard.com/en/blizzard/t/world-of-warcraft-forever-api-namespace/59774),
  [When will we gain API access to Forever APIs](https://us.forums.blizzard.com/en/blizzard/t/when-will-we-gain-api-access-to-forever-apis/59595),
  [In the name of the WoW dev community](https://us.forums.blizzard.com/en/blizzard/t/in-the-name-of-the-wow-dev-community/60236)).
  A community library notes Forever has no profile namespace ([Guildbook](https://github.com/Guildbook/guildbook)).
- `classic-ptr` namespaces are test-realm only ([Blizzard forum: new APIs for testing](https://us.forums.blizzard.com/en/wow/t/new-apis-now-available-for-testing/1645500)).
  Config validation rejects any `ptr` namespace.
- Classic namespaces follow `static-classic-{region}` style names ([@blizzard-api/classic-wow](https://www.npmjs.com/package/@blizzard-api/classic-wow)),
  but Forever is a different ruleset, so they are not substituted.
- Ownership needs the user-authorized `wow.profile` scope ([Blizzard API update](https://us.forums.blizzard.com/en/blizzard/t/world-of-warcraft-api-update-visions-of-nzoth/3461)).
- Forever is realmless (rulesets instead) and launches 2026-11-04 ([WoW news](https://worldofwarcraft.blizzard.com/en-us/news/24304075),
  [Blizzard news](https://news.blizzard.com/en-us/article/24315643/return-to-azeroth-in-world-of-warcraft-forever)).
  Characters: Forever has a ruleset and no realm; Retail requires a realm.

Consequence: every edition has `api_status` per capability (`verified | unverified | unsupported`)
and per-namespace config. Forever ships as `unsupported` everywhere; the UI says "no Blizzard API for
this edition yet" and the code makes zero requests. When Blizzard publishes namespaces, an admin runs
`questersboard-admin blizzard probe forever --region us`, fills the namespaces and flips the status: no code change.

## Architecture

```
Discord ──interactions──> questersboard-bot ─┐            (gateway; Guilds intent only)
                                          │ SQL (questersboard_app)
Browser ──Nginx /wow/quester/, /wow/chronicle/ ──unix socket──> questersboard-web ─┤
                                          │                     (FastAPI + Jinja2, minimal JS)
                                          ▼
                                   PostgreSQL "questersboard"
                                          ▲  outbox rows written in the same transaction
                                          │ SQL (questersboard_worker)
Discord REST <── posts, edits, DMs ── questersboard-worker  (reminders, overrun nudges, retention)
Blizzard API <── lookups (web), reference import (worker/CLI)
```

- **One codebase, three units**, one config (`/etc/questersboard/config.toml`), per-unit credentials.
- **Outbox**: every side effect (event post, DM) is an outbox row committed with the change that
  caused it. The worker delivers at least once with idempotency keys; event posts are one canonical
  message per event, edited in place and recreated if deleted.
- **Projections**: public pages and feeds read only `public_events` and `public_chronicle`, which hold
  counts and approved, privacy-checked text. The readonly role can only see those.
- **Scheduling enforcement** in the database (see below), so the bot and web can't race each other.
- Not used, on purpose: React, Redis, Celery, Kubernetes, separate microservices.

## Scheduling enforcement

Two independent safeguards, both transactional:

1. **Community capacity** (`event_reservations`): at most `[scheduling] max_simultaneous_wow_events`
   overlapping QuestersBoard events, and nothing may overlap an exclusive event. A trigger takes a global
   advisory lock and does a peak sweep over overlapping intervals. An exclusion constraint alone can
   only express "no overlap" (a limit of 1), so it isn't used for capacity.
2. **Member conflicts** (`member_reservations`): one person can't organize, co-host or be confirmed in
   two overlapping WoW events, across all their characters. This *is* a pure no-overlap rule, so it is
   a `btree_gist` exclusion constraint on `(member_id, tstzrange [start,end))`.

Only QuestersBoard events are written there. Other Discord scheduled events are never read. Drafts,
pending, tentative and waitlisted signups reserve nothing. Intervals are half-open UTC with the event's
IANA zone kept for display, recurrence and DST.

## State machines

**Event:** `draft → scheduled → in_progress → completed`, and `draft|scheduled|in_progress → cancelled`.
Publishing reserves; completion and cancellation release. Rescheduling re-runs both checks and, with
`reschedule_requires_reconfirm`, flags confirmed players to reconfirm. A confirmed player whose new
time collides with another of their WoW events is moved to tentative (no reservation) and told why. Overruns are never auto-extended: managers get one nudge and can
extend explicitly. "Copy" creates a clean draft (no signups, no post, no chronicle).

**Signup:** `pending | confirmed | tentative | waitlisted → withdrawn | rejected`. Open policy confirms
directly when a slot is free, else waitlists; approval policy creates `pending`. A unique partial index
blocks duplicates; role capacity is checked under a row lock. Attendance (`attended | absent | unknown`)
is a separate table, recorded by managers after the event.

**Chronicle entry:** `draft → pending_review → published`; `pending_review → changes_requested | rejected
→ edited → pending_review`; `published → unpublished`. Material edits of a published story create a new
revision that goes back through review while the old approved version stays public. Trusted authors
self-publish their own entries (still audited, still privacy-scanned). Every revision is kept.

## Delivery phases (all delivered; phases were order, not scope)

1. Foundation: config/secrets validation, DB roles and migration, models, audit, outbox, admin CLI.
2. Roster: editions, rulesets/realms, race/class catalog, characters, Blizzard boundary.
3. Scheduling: events, roles, signups, capacity and member conflicts, lifecycle, Discord post and bot.
4. Chronicle: drafts, co-authors, review, consent, projections, public pages, feeds, uploads.
5. Enhancements: recurring series, calendars, recommendations, opt-in AI wording, Battle.net link.
6. Operations: systemd units, Nginx snippet, backups, restore drill, rollback, docs, previews.

The requirement-by-requirement status is in [`requirements_checklist.md`](requirements_checklist.md).
