# Changelog

## Unreleased

- **`discord-permissions`: who else can get in.** A new last section checks every server role, not only the
  ones in config: roles with Administrator (which Discord lets past every channel permission, invite-only
  event chat and event voice) and, when the bot may list members (Server Members Intent), the people in them
  and the server owner; for each private channel in the WoW category, which other roles and people (without
  Administrator) can see or join it; which roles can open every invite-only event chat (Manage Threads); and
  who can join event voice. Still GET only.
- **Add one event to your calendar.** Event pages have an Add to calendar menu (scheduled and running
  events): `<member path>/events/<id>/calendar.ics` (one event, same UID as the feeds so it doesn't
  duplicate; same access as the event page), a Google Calendar link, the guild feed (if
  `[calendar] public_enabled`) and, when signed in, a link to your private feed on Your account.
  `upcoming.json` and `status.json` events gain `ics_url` and `google_calendar_url` so a site can link
  the next event. No config or database change.
- **Invite-only event chats.** `[event_threads] private = true` keeps each event's chat thread to the people
  signed up and the hosts (and moderators with Manage Threads); the event post stays visible to everyone.
  New `[event_threads] member_states` (default `confirmed`, `tentative`, `waitlisted`: Join, Tentative and
  the waitlist) decides who is added. The opening message and Help say who can see the chat (private or
  public). `discord-permissions` now checks thread permissions for members (Send Messages in Threads,
  Create Public Threads), lists who can see every private chat (Manage Threads), flags private threads in
  an Announcement channel (Discord doesn't allow them), checks `[item_links]` channels, and shows three
  thread columns. New `deploy/discord_permissions.sh` runs it. No database migration.
- **Fix: Refresh from Blizzard reused the profile cache.** The character page's Refresh from Blizzard
  and Refresh all from Blizzard returned cached character pages (up to `cache_hours_profile`, 6 hours)
  while saying "just now". They now read character pages afresh for that request only; page views and
  lookups still use the cache. No config or database change.
- **Timed refresh from Blizzard.** `[blizzard] auto_refresh_hours` (default 4; 0 = off; 1, 2, 3, 4, 6,
  8, 12 or 24) refreshes everyone's Blizzard data on their behalf every that many hours from midnight
  server time: imported details with each owner's groups and sharing, and Blizzard checks (with the
  verified mark) kept current for characters without details. Character pages are read afresh, not from
  the profile cache. A character Blizzard no longer has loses its Blizzard data and stays as entered,
  self-reported (Blizzard's API terms). New `questersboard-refresh.service` and `.timer` (hourly; the
  service checks the setting), installed by `install.sh` but not enabled:
  `sudo ./deploy/enable_auto_refresh.sh`. `sudo ./deploy/refresh_details.sh` runs it now.
  `questersboard-admin refresh-details` now covers Blizzard checks too, runs one at a time, and has
  `--scheduled` and `--next`. Help, privacy (with a Recent changes entry) and What's new updated.
  No database migration.
- **Event page: voice while it opens.** Right after an event starts, people who can join voice see
  "Voice: Opening in Discord; reload this page in a moment for the link" until the channel exists; then
  the existing "Join the voice channel in Discord" link replaces it.
- **Item cards in Discord (off by default).** `[item_links]`: an item name in [square brackets] in a
  watched channel gets a card in the item's quality colour with its Blizzard tooltip (binding, type,
  damage, stats, requirements, sell price) and icon, and no link. Cards are posted by a webhook the bot
  creates in the channel ("QuestersBoard Items"), since Discord refuses bot posts in channels linked to
  in-game guild chat and webhook posts aren't relayed to the game. One card message per player message
  (up to `max_per_message` items), a per-name cooldown, a search of `fallbacks` (Classic Era by default) when Forever's
  data lacks the item, a footer naming the source ("Source: Blizzard · WoW: Forever API", or the fallback
  with "not in WoW: Forever yet; may differ"), "No item called … found in Blizzard's public game data."
  when none has it (`not_found_text`), recipe cards with "Creates: …" and the reagents when Blizzard
  sends them (no "Non-equippable" line), a link back to the message only when cards go to `post_channel`, and
  the card is deleted with the player's message. Only names and message IDs are queued. Needs the
  Message Content intent and Manage Webhooks, and a restart. `questersboard-admin blizzard probe-item`
  shows what a card would contain (`sudo ./deploy/probe_item.sh "Item Name"`). Profession-window links ("Leatherworking: Dark Leather Cloak") show
  the item made, "Crafted with Leatherworking (skill)" and the reagents from the recipe item when there
  is one (`professions`, `crafted_text`); recipes that make no item show the recipe item. Up to 10
  search pages per name by default (`search_pages`, up to 25). `sudo ./deploy/refresh_item_cards.sh` (or `questersboard-admin refresh-item-cards`) looks recent cards up again
  and edits them in place (dry run unless `--yes`). Webhook URLs are redacted from logs and `httpx` request lines are no
  longer logged. Privacy page: an "Item cards" paragraph and a Recent changes entry, shown only when on.

No database migration. No config change needed: the role icons are on by default and appear once the
emojis are uploaded.

- **Submenu with section dropdowns.** Every page, member and public, has a row under the main menu in
  the site's own submenu style: the site's lead group (`[branding] subnav_lead_label`, `subnav_lead`,
  by default "WoW" Schedule when `game_page_url` is set), the product label, then Quests (Events, Plan
  an event, My signups), Characters (Mine, Players, the member's guild rosters, Who can craft),
  Chronicle (Stories, Review queue for reviewers) and Help (How it works, Privacy, Terms of use, What's
  new). Tap opens a section on touch screens, hover with a mouse; Esc or tapping elsewhere closes it.
  The current section and page get the "›" marker. Quests and Chronicle left the main menu, which keeps
  the site's links and the account menu; the roster and Shared characters buttons on My characters,
  character pages and rosters are gone, since the Characters menu has them.
- **Same header on every page.** How it works, Privacy, Terms, Changelog and the Chronicle now read the
  login cookie (read-only: never renewed or written), so a signed-in member sees their account menu and
  their full submenu (guild roster, Review queue) there too, instead of the logged-out header. Those
  signed-in responses are `Cache-Control: private, no-store`; every public page sends `Vary: Cookie`, and
  the logged-out page is unchanged. The "›" marker on the current section no longer nudges the row.

- **Role icons on event posts.** A shield, a healing cross and crossed swords (QuestersBoard's own
  artwork, `web/static/roles/`) sit before each role's heading on the Discord post ("🛡 Tank (1/2)"; `[event_posts]
  role_icons`, role key -> emoji name; `role_icon_position = "heading"` or `"line"` for the start of the
  names line). They are the
  application's own emojis, so they work in every server the bot is in without Use External Emojis. The
  worker looks them up at most every 30 minutes; a role without an uploaded emoji, or a failed lookup,
  stays plain text. The event page shows the same icons. `role_icons = {}` turns them off.
- **`questersboard-admin role-emojis`** lists which icons are uploaded and which would be (read-only);
  `--yes` with `QUESTERSBOARD_APPROVED_CHANGE=role-emojis` uploads the missing ones. It never replaces
  or deletes an emoji. `--art DIR` uploads your own `<role key>.png` pictures (for custom roles too).
  FIXTURE-tested only: the live upload and how Discord shows the icons are unverified.

## 2026-11-11

- **One account menu.** Every member page has a menu under the member's name: the Discord login, each
  Battle.net region (connected, last synced, how many characters verified, Sync now, Disconnect, add
  another region), My characters, My signups, My settings, Sharing and privacy, How it works, Log out
  and Log out on all devices (deletes every web session of the member). Public pages stay session-free.
  "Connected" means the member has characters verified through Battle.net in that region (or a list
  from the last `battlenet_import_minutes`): QuestersBoard still never keeps a Battle.net token.
- **One Battle.net login per region.** `/characters/battlenet?region=us` reads the account's character
  list for every edition with `ownership` switched on in that region, with the same user token, and
  stores one encrypted list `{"editions": {...}}` (old one-edition lists still read). `?edition=` still
  narrows it to one. An edition that doesn't answer is named and skipped. Unverified against the live
  API: that one login's token is accepted for every profile namespace (expected, as the token isn't
  tied to a namespace).
- **Get started and the Battle.net panel.** With no characters, My characters shows a three-step start
  (region cards, Continue with Battle.net, or add one by hand); also at `/characters/start`. After the
  login, characters to add appear in a side panel (a full sheet on phones) grouped by edition and
  pre-ticked, with one "Who can see the ones you add" choice (`[blizzard] picker_default_share`, default
  `"guild"`); added characters have every section brought in (up to `refresh_all_max`). Region names
  come from `[blizzard] region_labels`.
- **My characters: list and detail.** Searchable character cards (Verified / Not verified and a sharing
  chip) beside the selected character: Open full page, Refresh from Blizzard (or Verify with Battle.net),
  a ⋯ menu (Edit roles and notes, what to bring in, Remove), and **Who can see what** with segmented
  Only me / Guild / All members controls that save as you click (`POST /characters/{id}/sharing`, no
  Blizzard calls). Asking for more than Only me on an unverified character is refused with an
  explanation instead of reverting silently. A Battle.net status bar (or a Connect banner) sits on top.
  Phones show the list, then the character on its own. The All characters panel and the sync panel at
  the bottom are gone; Refresh all from Blizzard moved to the page header.
- **Disconnect per region.** `/characters/battlenet/unlink` takes `region`; characters there drop to
  self-reported but keep their sharing choices (they only apply again once verified).
- **Footer and changelog page.** Every page ends with a footer like the host site's: wordmark,
  `[branding] footer_notes` and `footer_credits` lines, `footer_nav` links, the QuestersBoard links and
  the running build ("Commit abc1234 · Nov 11, 2026"). `deploy/install.sh` writes the build stamp into
  each release as `BUILD`; a checkout without one asks git, otherwise "local". The members' changelog
  is at `<member path>/changelog`, from `src/questersboard/web/changelog.toml` (one card per release,
  newest open).
- **Sortable tables that stay in view.** Guild roster and Shared characters keep the filter bar and
  column headings in view while scrolling; every heading sorts (in the browser, or by link with
  `?sort=<column>&dir=desc` without scripts). Phones fold the filters into "Filter and sort" with a
  "Sort by" picker.
- **Recipe icons and tooltips.** At import, each known recipe is looked up: the recipe's crafted item
  (or else the item named exactly like the recipe), giving the icon, quality colour and an item tooltip;
  recipes without an item use the recipe's own icon if Blizzard has one. Results are kept across
  refreshes; each import looks up at most `[blizzard] recipe_lookups` new recipes (default 80), and
  recipes with nothing found are retried after a week. Off with `detail_icons = false`. The
  recipe-to-item link is unverified against WoW: Forever until `probe-media --detail recipes` is run.
- **Gear says "Item level"** instead of "ilvl".
- **Fix: race silhouettes were missing from installs.** The package only included files directly in
  `static/`; `static/silhouettes/` is now packaged.
- **Media probe checks a recipe.** `probe-media` now reads one known recipe: its recipe data, what it
  makes, its icon, and the item with the same name. `--detail recipes` shows only those checks.
- **Known recipes.** Each profession on a character page lists the recipes the character knows
  (collapsed under the skill line), from Blizzard's professions page, shared with the same people as
  Professions. `[blizzard] known_recipes_max` caps the list per profession (default 500).
- **Who can craft.** Guild roster and Shared characters have a "Who can craft" search: part of a
  recipe name, with suggestions, lists the characters that know matching recipes and which ones. Only
  recipes in Professions shared with the viewer are searched.
- **Race outlines when there's no picture.** The gear centre shows an outline of the character's race
  with "No picture from Blizzard yet" instead of a blank space; a ticked Portrait with nothing from
  Blizzard is kept so refreshes keep asking for it.

- **Top button stays at the bottom.** It no longer hides over the footer; the footer leaves room so
  the button (and the floating Add a character button) never covers its links.
- **Privacy and How it works updated.** Privacy explains refreshes (yours, and organiser refreshes after
  an update that keep each person's groups and sharing), that pictures and icons are shown from this
  site, the 30-day clock running from the last refresh, and adds a Recent changes section. How it works
  covers the Add a character button and dialog, detail tabs and trees, and logging out in game before
  refreshing.

- **`questersboard-admin refresh-details`.** Refreshes every character's imported Blizzard details from
  the server, as if each owner pressed Refresh from Blizzard (same groups, same sharing), so nobody
  comes back to trees stored by an older release. Dry run unless `--yes`; characters without imported
  details are left alone. New `[blizzard] operator_refresh_pause_seconds` (default 1) spaces the calls.

- **Talent trees centred.** Each tree's grid is now only as wide as the columns its talents use
  (the same width for every tree), so the talents sit in the middle of their frame instead of the left.
- **Top button alongside the floating Add button.** The Top button (shown after scrolling a little
  over a screen, hidden over the footer, as on the main site) takes the bottom-right corner, and the
  Add a character button steps up above it while it shows.

- **Add or edit a character in a dialog.** My characters drops the side form: an "Add a character"
  button sits at the top and a floating one stays at the bottom right while scrolling. Both open the
  same on-page dialog that Edit uses (filled in for that character). Without scripts the form shows
  in the page at `?add=1` / `?edit=N`, as before.
- **Talent trees no longer clip.** Trees centre safely in their frame (never pushed off the left),
  columns can shrink on narrow screens, and tooltips are no longer cut by a scrolling frame. Trees
  are also grouped more robustly when a tree has an empty column inside it.
- **Pets on a stand.** Pet pictures have their flat backdrop made transparent and stand on the same
  faint base as the character portrait.

- **Talent trees placed by their block of the class grid.** WoW: Forever's per-specialization pages
  each carry the next tree's leftmost column, which pushed a stray column off the side of a tree.
  Talents are now grouped by the run of class-grid columns they sit in (blocks split by empty
  columns), and each block takes the name of the page holding most of its talents; if the blocks don't
  line up with the pages, each page is drawn as sent. Trees no longer spill out of their boxes.
- **Refresh from Blizzard on the character page.** Owners can refresh from their character's page
  with the same groups and sharing ("Refreshed from Blizzard." afterwards).
- **One name for refreshing.** Everything that pulls data from Blizzard says "Refresh from Blizzard"
  ("Refresh all from Blizzard" at the top of My characters), and dates say "Last refreshed from
  Blizzard". The existence-only check is now "Check it exists on Blizzard" and only shows until details
  are imported.
- **Faction-coloured tabs.** The section tabs are red for Horde characters and blue for Alliance.

- **Section tabs.** A character's details open with Armory-style pill tabs (Character, Talents,
  Professions, Progress, Pets; only those with something to show). Arrow keys move between them, a
  single character's page remembers the tab in the address (#talents), and without JavaScript every
  section shows in order as before. Per-group sharing labels are unchanged.
- **Complete talent trees.** Each specialization is placed from its own leftmost column instead of
  fixed four-column blocks of the class grid, and every tree is as wide as the widest one (at least
  `talent_tree_columns`), so talents past the old block edge are no longer dropped. Ranks are matched
  by talent id, then by spell id, then by name.
- **Figurine stand.** The full-body render is transparent, so the grey box behind it is gone and the
  character stands on a faint round base. Other picture types (bust, avatar) show without the base.
- **Bigger pet pictures.** Pet pictures are cropped to the animal (Blizzard's creature pictures have it
  small on a plain backdrop) and shown as 160px cards with the name and family underneath.

- **Talent trees.** Where an edition sets `talent_tree_columns` (WoW: Forever: 4), Talents are drawn as
  one tree per specialization from Blizzard's talent-tree pages: each talent in its row and column
  with its icon, "rank/max", what it requires, and the current and next rank in its tooltip. Other
  editions keep the plain talent list. Tree icons follow the Talents sharing.
- **Hunter pet pictures.** When Blizzard lists a pet's creature display, its picture is shown beside
  the pet's name (fetched by QuestersBoard like gear icons, with the Pets group's sharing). Pets
  without one keep the text-only line. `probe-media` now checks a pet's display and picture too.
- **Refresh all and one sharing choice at the top of My characters.** One press re-imports every
  imported character (same groups, oldest first, up to `[blizzard] refresh_all_max` per press), and
  "Who can see it" can set every group of every character at once. Each character keeps its own
  Refresh and per-group choices for fine-tuning afterwards. The sharing dropdown has a clear chevron with
  Save beside it (inactive until a choice is made); Refresh all sits on its own row and keeps sharing.
- **Fixed-shape gear panel.** Every character shows the same left, right and weapon columns; empty
  slots appear as dimmed placeholders (hidden on phones), and the centre keeps its space even without
  a picture. Older imports are laid out by slot name. Weapon-row slots are set per edition with
  `gear_bottom_slots`.
- **Fresh pictures and icons after a refresh.** Picture and icon addresses now change when the
  character is refreshed, so browsers don't keep showing an old cached image.
- **Pets follow Progress sharing.** A newly imported Pets group starts with the character's Progress
  sharing choice instead of "Only me".

- **Pets are their own group.** Hunter pets (name, family, level) moved out of Progress into a Pets
  group with its own tick box and sharing choice. Pets imported earlier under Progress are no longer
  shown; tick Pets and refresh to bring them back.
- **Portrait fills the space.** Blizzard's full-body render is a large transparent canvas with the
  character small in the middle; QuestersBoard now crops it to the character before showing it.
- **Portrait between the gear columns.** When Gear and Portrait are both imported and visible to the
  viewer, the character's full-body picture sits between the two gear columns like the in-game panel,
  with weapons centred below; on phones it sits above the list. The Portrait group then says it's shown
  in Gear, so it isn't repeated. Someone who can see Gear but not Portrait sees no picture.
- **Tooltips on desktop.** A clicked tooltip now closes when the mouse leaves the item.
- **Reused login link.** Opening a Discord login link again while already signed in (refresh, back
  button, a second tab) now goes to Quests instead of showing "That login link expired". The reused
  code is ignored.
- **Gear and talent icons and tooltips.** Imported gear shows in two columns like the in-game
  character panel, with weapons below; each item has its Blizzard icon, quality colour and a tooltip
  (binding, type, armour, stats, requirements, item level, transmog, sell price) with a "View on
  Wowhead" link. Talents show as icon tiles with rank badges and a tooltip (rank, cast time,
  description). Tap opens a tooltip on phones; Escape or tapping elsewhere closes it. Icons are served
  by QuestersBoard after the same sharing check as the page, so the page policy stays `img-src 'self'`.
  New config: `[blizzard] detail_icons`, `icon_lookups`, `icon_cache_hours`, `icon_cache_entries`,
  `[rate_limits] icon_requests_per_minute`, and per edition `item_link_template`, `spell_link_template`.
  Existing imports need "Refresh from Blizzard".
- **Media probe follows the talent trees.** `probe-media` now also reads one spec tree and its class
  tree, to plan the in-game tree layout.
- **Media probe.** `questersboard-admin blizzard probe-media EDITION --name "First Last"` checks, read-only,
  whether gear icons, item tooltips, talent icons and talent trees can come straight from Blizzard: it
  takes one equipped item and one talent from the character and checks the item, item media, spell,
  spell media, talent and talent-tree answers. Icons are fetched once from allowed image hosts only to
  confirm type and size, then discarded. Nothing is saved.

- **Link previews for member pages.** Links to My characters, guild rosters, character pages and other
  member-only pages used to preview as Discord's own card, because the preview fetcher followed the
  login redirect. Known preview fetchers (`[web] link_preview_agents`, optional) now get a small card
  with the page's name and purpose, never member data, and it isn't indexed. People still go to login.
  How it works' preview text mentions character details and guild rosters.

- How it works has a new "Character details and guild pages" section (shown when Blizzard is enabled),
  and Remove me now mentions imported character details.

- After Import or Refresh from Blizzard (or Remove imported details), My characters returns to the top
  of that character's card with a "Saved" note, instead of below it. Roster tables on the guild and
  Shared characters pages line up with their headings again (a style name clashed with the event roster).

- Guild roster, character and Shared characters pages have a bar back to My characters and to your
  other roster pages. The import form has a "Set all" row that applies one "Who can see it" choice to
  every group (works without JavaScript too).

- **Character pages and guild roster.** "Open its character page" shows a character as others see it:
  only the groups shared with the viewer. A guild roster page (`/guilds/...`) appears once a verified
  member shares their guild, visible only to members verified in that guild, filterable by class,
  role, spec, profession, level and item level. "Shared characters" (`/players`) lists characters
  shared with all members.
- **Portraits are served by QuestersBoard.** The site fetches the picture from Blizzard (hosts on
  `[blizzard] render_image_hosts` only, images only, 2 MB cap) so browsers never contact Blizzard and
  the page security policy stays `img-src 'self'`. This fixes the empty portrait box.
- Stats are shown as labelled sections, hiding zeros, with % for crit, hit and defenses. Hunter pets
  show their family (re-import to fill it in).

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
