# Changelog

## Unreleased

No database migration.

- **Event posts**: a blank line between headings (Time, Duration, Meet at, Going, ...).
  `[event_posts] field_spacing = false` keeps the tight layout. Applies to new edits; run
  `wowquester-admin refresh-posts --yes` to re-space current posts.

## 2026-11-04 (event posts)

Needs database migration `0004` (adds columns only; reversible). Host config: add `[event_posts]` and,
when threads are approved, `[event_threads]`; `difficulties` per edition is optional.

- **Event posts redesigned** (the approved mock): the side accent is the event's faction colour (Horde
  red, Alliance blue, gold for "any"; `[event_posts] faction_colors`); finished and cancelled posts keep
  the state colour. Under the title: difficulty and level range ("Normal · Level 15–21"), the
  description, then **Objective**. Time, Duration and Repeat ("Every week on Tuesday · 2 of 4") are
  stacked; tentative players are listed by name when they agreed to be named; waitlist and pending
  counts sit under "Also". The footer starts with "Hosted by <character>".
- **New event fields**: Difficulty (per-edition list, `[[editions]] difficulties`, default Normal only),
  lowest and highest level, Objective (up to 500 characters), Hosted by (one of the hosts' own
  characters), and Cover art (PNG, JPEG or WebP, re-encoded to WebP). Without a cover the post uses the
  activity's or the community's default art (`[event_posts] default_cover`, installed with
  `wowquester-admin default-cover FILE`). Covers are served at `<member_path>/covers/<name>` so Discord
  can show them.
- **Chronicle**: a new story's "The plan" starts from the event's Objective. Authors can pick one of the
  story's screenshots as the banner ("Use as banner"); otherwise the story shows the event's cover art.
  The public story shows the difficulty and level line.
- **Event chat threads** (off by default, `[event_threads] enabled`): one thread per event (public from
  the post, or private). Signed-up players (confirmed, tentative, waitlisted) and hosts are added; people
  who leave are removed; reminders (with open spots) are also posted there; the thread is locked and
  archived `archive_after_hours` (24) after the event ends or is cancelled. `discord-permissions` checks
  the thread permissions when enabled. FIXTURE-tested only: live Discord threads are unverified.
- **Current events**: `wowquester-admin refresh-posts` lists the upcoming and running posts that would
  be re-rendered in the new layout; `--yes` queues the edits (in place, nobody is pinged).
- **Blizzard dungeon/raid details** (`[blizzard] instance_details`, default on): the reference import also
  reads each instance's difficulties and minimum level; the form then marks difficulties the instance
  doesn't offer and suggests its minimum level. FIXTURE-tested only: live responses are unverified, and
  Classic coverage is unknown.
- **Fix**: "Where" suggestions on the event form never appeared (the form asked for the wrong list).

## 2026-11-04

No database migration. Host config: set Retail's `catalog = "retail"`; `common_timezones` is optional.

- **Reconfirm only when needed.** A date, time or length change still asks everyone to reconfirm
  (optional `[scheduling] end_change_notice_only_minutes`, default 0 = off, makes small end-only changes a
  notice). An edition, ruleset, faction, realm or region change re-checks each signup's character: ones
  that still fit are left alone and get a notice; ones that don't become tentative (their spot goes to the
  next fitting waitlisted player) and are DMed to pick another character. The organizer sees who is
  affected before saving. Changing only the faction now applies without resending the edition.
- python-dateutil allowed up to 4.x (was <3), replacing Dependabot PR #1. WoWQuester only uses its recurrence rules; the full suite passes on 2.9, 3.0 and 4.0. requirements.lock still pins 2.9.0.post0, so installs are unchanged until the lock is refreshed. Dependabot PRs now target prototype.
- **Race and class are real dropdowns** (iPad/iPhone): Safari's typed-suggestion lists only showed
  entries matching what was already in the field, so after the first pick no list appeared. Changing
  the race now clears a class that race can't be, and says so. "Other (type it)" keeps free text.
  The lists filter each other: pick a class first and only races that can be it are offered, and the
  other way round. The first choice in each list ("Clear class (show all races)") widens it again.
- **Retail race/class list**: packaged seed for Midnight's 26 races (marked unverified); Retail
  previously had none until a Blizzard import.
- **Time zone pickers** on My settings and the event form: the IANA time zone database installed on
  the server (no outside API), grouped by region with current UTC offsets, community default and
  `[community] common_timezones` first. "Community default" can be chosen again in My settings.
- **My time zone** now sets the starting zone of events you create (it was saved but unused).
- **Bot matches the site**: the `/quest join` role picker shows each role's fill ("Tank · 1 of 1
  taken", "Full: you'd be waitlisted"); `/character add` offers only races that can be the class typed;
  `/quest create` prefills your saved time zone and accepts a city ("New York") or IANA name;
  `/character list` and the join picker use catalog names ("Mag'har Orc Death Knight", "RP-PvP");
  `/quest list` follows the same member, channel and DM rules as the other commands. No command
  re-registration needed (the command definitions are unchanged).

## 2026-11-02

Needs database migration `0003` (installed by `deploy/install.sh`) and a few host config edits.

- **Attendance**: the menus now show the saved value. Before, every save reset anyone not changed to
  Unknown, which also dropped them from the chronicle.
- **Roles**: presets are Tank, Healer, Damage (Support, Guide, Learner and RP role removed; custom roles
  still allowed). The signup form shows each role's fill ("Tank · 1 of 1 taken (full, you'd be
  waitlisted)") and open spots. People who joined before roles existed are listed under "No role picked
  yet", asked by DM to pick one, and the change form no longer shows the first role as if chosen.
- **Group sizes**: per activity type in `[signups.party_size_max]` (quest/dungeon/arena 5, raid and
  battleground 40, others 80). Participant limit can't exceed it; role counts can't exceed the limit.
- **Editions**: Forever gains the RP-PvP ruleset. Example config adds Classic Era (incl. Hardcore and
  Season of Discovery realms), Burning Crusade Anniversary and Mists of Pandaria Classic, with packaged
  race/class seeds and "Where" suggestions (zones, dungeons, raids, battlegrounds) marked unverified.
- **Co-hosts by name**: type Discord usernames, comma separated; IDs and mentions still work. Usernames
  are saved at sign-in (`members.username`, cleared on account removal).
- **Notices**: when a host or co-host edits an event, the other hosts get a DM listing what changed.
  Bot DMs and review posts no longer show Discord link preview cards.
- **Chronicle**: every attendee can write their own story; all published stories appear on the event's
  one chronicle page (host, co-hosts, then others). The guided answers always appear as sections. Every
  save is a new version with time and author in History. Co-authors are ticked from the event's people
  and can be removed. Unpublishing DMs the author with the reason; review decisions include the note.

- **Login**: first-time Discord logins no longer fail with `access_denied`. The quiet login
  (`prompt=none`) only works for accounts that already authorized the app; on `access_denied` the
  callback now asks once more with Discord's normal Authorize screen. Pressing Cancel there shows
  "Login not finished" with a retry button instead of a generic failure.

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
- **Phones**: a Menu button on every WoWQuester page (the community stylesheet hides the menu under
  760px until it is opened), a login button on the logged-out home page, and `?v=` cache-busting on
  WoWQuester's CSS and script.
- **Login return address**: rebuilt from the member-area prefix and a checked remainder (CodeQL
  `py/url-redirection`); `tests/test_login_next.py`.
- **Public repository**: `CONTRIBUTING.md`; `SECURITY.md` points to private vulnerability reporting.
- **Go live**: `deploy/go_live.sh` switches the public address and adds the live Nginx include with
  checks, a backup, automatic restore on a failed `nginx -t`, live-host checks and `--rollback`.
- **Repository**: tests workflow (full suite against PostgreSQL with production roles; database tests
  must run, not skip), Dependabot, SECURITY.md, sanitization check with a private denylist.
