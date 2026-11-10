# Changelog

## Unreleased

- **Import character details from Blizzard.** On My characters, each character has "Import details
  from Blizzard" with a checkbox per group (overview, guild, gear, talents, professions, stats,
  portrait, PvP, progress), all ticked by default, plus a "Who can see it" choice for each: Only me,
  My guild, or All QuestersBoard members. Sharing beyond "Only me" applies only while the character
  is verified through Battle.net, and only takes effect as the member and guild pages arrive (see
  docs/character_details_plan.md). Details are deleted after `data_ttl_days`, on Remove imported
  details, when the character is removed, archived or renamed, and on Remove me. The privacy page
  says so. Host: **database migration 0009** (new `character_profiles` table) and a new optional
  `[blizzard] render_image_hosts` setting (portrait image hosts allowed by the page's security policy).
- **probe-character** also checks the character's guild and guild roster pages (sizes only, never
  other players' names). `--detail PAGE` prints one page's structure.

- **Character page probe.** `questersboard-admin blizzard probe-character EDITION --name "First Last"`
  asks Blizzard for one public character's summary and 15 detail pages (equipment, specializations,
  professions, media, statistics, PvP, achievements, titles, reputations, dungeons, raids, quests,
  mounts, appearance, hunter pets) and prints each page's status and field names. Read-only: no
  database, no cache, nothing saved. Used to decide what a future character import can offer.

- **Difficulty lists per edition.** `difficulties = []` hides the event form's Difficulty choice for
  editions with a single difficulty; the example config now uses it for WoW: Forever and Classic Era
  (TBC Anniversary, Mists Classic and Retail keep their lists). Saving an event in such an edition
  clears an old difficulty. Host: edit `[[editions]] difficulties` in config.toml and restart; no
  database migration.

- **Renamed to QuestersBoard.** The app was called WoWQuester; Blizzard's API terms don't allow its
  trademarks in an application's name. Pages, messages, docs and the repository now say QuestersBoard.
  Entries below keep the old name.

- **Internal names renamed to questersboard** (web addresses under `/wow/quester/` are unchanged). The
  Python package and commands (`questersboard-admin`), services (`questersboard-web/bot/worker`, socket
  `/run/questersboard-web.sock`, backup timer), Linux account, folders (`/etc`, `/opt`, `/var/lib`,
  `/var/backups`), PostgreSQL database and roles (`questersboard_*`), credential names
  (`questersboard.<name>`), the Nginx snippet and `QUESTERSBOARD_*` variables. For one release the old
  `WOWQUESTER_*` variables and `wowquester.<name>` credentials are still read if the new ones are missing.
  Existing hosts run `deploy/rename_host.sh` once (dry run by default, `--apply`, then `--nginx --apply`;
  `--rollback` undoes it); see docs/install.md. Discord button IDs (`wq:`), page CSS classes and the site
  widget file name are unchanged, so existing posts keep working.

- **Battle.net sync: placeholder names.** Characters Battle.net lists under a temporary name (letters plus
  a code, e.g. awaiting a forced rename) are shown but can't be ticked, with a note to rename them in game
  and sync again. Realm names on the list read "Dark Iron" rather than "dark-iron".

**Database migration 0008** (worker grants only; run `migrate up` before restarting).

- **Blizzard API terms.** Blizzard data is kept no longer than `[blizzard] data_ttl_days` (30, the most
  the terms allow): the worker deletes each Battle.net character list once the pick window
  (`battlenet_import_minutes`) has passed, clears Blizzard checks (verified / API matched) that weren't
  refreshed in time (the character stays, self-reported), and deletes cached API answers older than that.
  Pages that show Blizzard data name Blizzard as the source and say WoWQuester isn't endorsed by it; the
  privacy page adds the 30-day rule, "as is", Blizzard's privacy policy and that we never see Battle.net
  passwords; the terms say WoWQuester isn't meant for anyone under 13. The worker can see when a list was
  made, not what's in it.

- **Battle.net character sync, including WoW: Forever.** Sync from Battle.net on My characters reads the
  account's character list once (user token used once, never stored). Characters already on WoWQuester
  are marked verified and get their level updated; the rest are listed to tick and add, verified, with
  the ruleset prefilled from Blizzard where it says (Forever ruleset IDs in `ruleset_api_ids`). The list
  is kept encrypted and usable for `[blizzard] battlenet_import_minutes` (60). Forever is realmless: the
  edition now uses `profile-forever-{region}`, `dynamic-forever-{region}` and `static-forever-{region}`,
  and Check with Blizzard looks up `/profile/wow/character/{name}-{surname}`. These namespaces come from
  community testing; Blizzard hasn't documented them. Namespaces may now contain `.` and `_` so the static
  one can be pinned to a game version. No database migration. Live sign-in is untested here (fixtures only).

- **Tentative to confirmed**: tentative members can now say they're in, with **I'm in** on the event page
  or by pressing **Join** again in Discord (previously "already signed up", so the only way was Leave and
  join again). Full events waitlist them; approval events send the request to the host.
- **Fix: event voice channels weren't created** (Discord 403 50013 on Start). Discord only lets a bot attach
  permissions it holds server-wide when creating a channel, and only administrators may set Manage
  Permissions that way. The channel is now created bare in the category (copying its permissions,
  including the bot's category-only Manage Permissions), then locked: the bot keeps View and Connect,
  @everyone is hidden, staff roles are added, the category's other overwrites are removed, and signed-up
  players are added. A second pass a few seconds later re-applies everything, because Discord can apply
  changes on a brand-new channel out of order. If locking fails, the channel is deleted. Until the lock
  lands (well under a second) the channel has the category's permissions. No Discord settings change.

**Database migration 0007** (events.publish_requested_at).

- **Everyone can publish events** (`[publishing] open_to_all = true`). Members who joined the server less
  than `probation_days` (7) ago are on probation: they're told their events need approval first and use
  Send for publishing. The event_creator role always publishes. Join dates come from Discord.
- **Send for publishing**: members without the event-creator role send their draft to the moderators,
  who see it under "Waiting to be published" on the home page and get a staff-channel notice
  (`[discord] publish_request_channel_id`, falling back to `review_channel_id`). The owner gets a DM
  when it's published. Previously these drafts were invisible to moderators.

**Database migration 0006** (guest voice links, guests and voice bans; run `migrate up` before restarting).
**New slash commands** `/voice invite|kick|ban|unban`: registered only by the approved sync-commands step.
**Proposed Nginx rule** for short guest links (`/v/<code>`) in `deploy/nginx/wowquester_locations.conf`.

- **Leaving during an event** no longer disconnects you: you keep your seat until you hang up but can't
  rejoin unless you join again. `[voice] remove_on_leave = true` brings back the old behaviour.
- **Guest voice links** (`/voice invite`, optional DM on Start with `[voice] invite_dm`): someone outside the
  event signs in with Discord, is added to the server if needed, and can join that event's voice until it
  ends. No WoWQuester profile is made. The bot's role needs Create Invite.
- **Host voice moderation**: `/voice kick`, `/voice ban` (rest of the event), `/voice unban`; the server
  owner, bots, `protected_role_ids`, staff and admin roles can't be targeted.
- **How it works** page (`/wow/quester/help`), linked in the footer; the privacy page covers guest IDs and
  voice moderation records.

**Database migration 0005** (adds the voice columns to events; run `migrate up` before restarting).

- **Event voice channels (FU-1)**, off until `[voice] enabled = true` and `category_id` are set. Start
  opens one private voice channel per event; signed-up players, the creator and co-hosts get access, and
  `event_moderator` roles plus `[voice] staff_role_ids` can join without signing up. Joining later grants
  access; leaving removes it and disconnects the person (`remove_on_leave`). End or Cancel deletes the
  channel; an empty channel is deleted `empty_teardown_minutes` after the scheduled end (the event keeps
  running; hosts can reopen). The post gets a **Join voice** button; the event page shows a voice link.
  The bot counts people in these channels (numbers only) for the status card.
- **Organizer in Discord**: hosts now get Start event / End event (with a confirm) / Reopen voice buttons
  in the private Organizer reply. Others still get the web link.
- `wowquester-admin discord-permissions` checks the voice category when voice is on.
- **Text boxes** (chronicle story, event description): dragging the corner now only makes them taller, so they
  can't spill over the side column.
- **Published chronicle pages** use the full page width (same margin on both sides) instead of a 760px column.
- **Search and link previews for the Chronicle**: public pages now carry a description, a canonical URL and
  Open Graph / Twitter card tags, so links shared in Discord and elsewhere show a title, summary and image.
  A story uses its summary and banner or first approved screenshot; other pages use the new `[chronicle]`
  `description` and `social_image*` settings (optional; empty = no image). New
  `PUBLIC_PATH/sitemap.xml` lists the archive and every published story (`[chronicle] sitemap`, default on);
  point the site's robots.txt at it. Error and notice pages are now `noindex`. No migration.

- **Extend on a started event**: extending (or shortening) an event that has already started now only
  changes the end time and updates the post. Nobody is asked to reconfirm, nobody is moved to
  tentative, and no "event changed" DMs go out. Starting an event also clears any open "please
  reconfirm" requests left over from an earlier reschedule. Moving a scheduled event still asks
  everyone to reconfirm as before.

## Earlier (efc27ca)

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
