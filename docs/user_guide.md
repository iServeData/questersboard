# WoWQuester guide

## For members

**Discord or the web.**

| Command or button | Where you finish |
| --- | --- |
| `/quest list`, `/quest my`, `/quest join`, `/quest leave` | Discord |
| `/character list`, `/character remove` | Discord |
| Join / Tentative / Leave on event cards | Discord |
| `/quest create` | starts in Discord (draft); add roles and location and publish on the web |
| `/character add` | Discord for the basics; faction, level, spec, preferred role and focus on the web |
| `/character edit`, Organizer button | link to the web |
| Edit, start, end events; attendance; Chronicle; privacy; calendar link; Remove me | web only |

**Getting in.** Open the WoW page and choose "Sign up / my events", or use `/quest list` in Discord.
Log in with Discord. WoWQuester only learns your Discord ID, username and display name.

**Your characters** (My characters).
- WoW: Forever characters have a ruleset (no realm). Retail characters need a realm.
- To edit a character, click its name (or Edit): the form on the right fills in with its details. Change
  anything and press Save changes. Changing its name, edition, ruleset, realm, faction, race or class resets
  any Blizzard check. The edition can only change before the character has been in an event; changes that
  would stop it fitting events it is signed up for list those events with one "Leave these events and save"
  button. If the edition is locked, "Add it as a new character" keeps the original and adds the new one.
- Pick race and class from the suggestions or type your own. Suggestions follow the edition, faction and race you choose. WoWQuester only refuses a pairing its catalog rules out (for example a Gnome on the Horde, or a Tauren Mage in Forever) and tells you which options are valid. Anything not in the catalog is saved as typed.
- In Discord, `/character add` reads "Race and class" as one line ("orc warrior", "night elf druid", even
  "warrior orc"). If you type only one of them, or a pairing that can't exist, nothing is lost: the reply
  offers a picker for the missing or valid choice and saves as soon as you pick. Any other problem (a name
  with a number, an unknown ruleset) comes with an "Edit the form" button that reopens it filled in.
- Set the roles you can fill (tank, healer, damage, or your community's custom roles).
- "Check with Blizzard" (Retail only for now) confirms the character exists. It doesn't prove it's yours.
- "Prove a character is yours" (if your community turned it on): log in with Battle.net once. Matching
  characters show "verified as yours". "Forget my Battle.net link" removes that data any time.
- Forever shows "no Blizzard API for this edition yet". That's expected until Blizzard publishes one.

**Joining events.**
- Join with a character and (if the event has roles) a role. Open events confirm you straight away if a
  slot is free, otherwise you're waitlisted and moved up automatically. Approval events show "pending"
  until a host decides.
- Tentative means "maybe": it doesn't hold a slot.
- You can't be confirmed in two WoW events at the same time, whichever character you use.
- If an event moves, you're asked to reconfirm. If the new time clashes with another event you're in,
  you're switched to tentative and told why.
- If the organizer changes the edition, ruleset, faction or realm, characters that still fit stay
  signed up and you just get a note. If yours no longer fits, you're moved to tentative and asked to
  pick another character on the event page.
- Reminders arrive by Discord DM at the times your community set.

**Calendars.** "My events" → Personal calendar → "New private link" gives a calendar address for your
phone or desktop calendar. It is shown once. "Turn off my links" revokes it immediately. The public
"All guild events" calendar on the WoW page has no personal data.

**Privacy.** "Name my characters in public rosters and chronicle stories by default" can be changed in
My events → Privacy, and per event when you join. If you opt out, stories describe you neutrally
("a healer"). On a published story, "Stop naming me in this story" takes effect immediately.

**Remove me.** My settings → "Remove me from WoWQuester…" lists exactly what will happen, then asks
you to type `remove me`. Confirming withdraws you from every event, cancels upcoming events you host
(the people signed up are told), deletes your characters, replaces your names in Chronicle stories
with a neutral description, and disconnects your Discord login. A receipt shows what was done. It is
immediate and can't be undone; logging in again starts an empty profile.

### Privacy and terms

The footer of every WoWQuester page links to **Privacy** (`/wow/quester/privacy`) and **Terms**
(`/wow/quester/terms`). Both are public, so they can also be given to the Discord Developer Portal.
The privacy page is built from the live configuration: retention periods, upload limits and the
login session length come from config, and Blizzard and AI sections appear only when those
integrations are switched on. Set `[legal] contact`, `operator` and `effective_date` for your
community, and keep `backup_keep_days` equal to `WOWQUESTER_BACKUP_KEEP_DAYS`.

## For hosts (event creators and co-hosts)

1. **Plan an event**: title, WoW edition, ruleset (Forever) or realm (Retail), faction, activity, start/end in
   your time zone, optional roles (Tank / Healer / Damage or custom, with counts), open or approval signup,
   optional repeat. It is saved as a draft: drafts hold no time slot. Group sizes are enforced: a quest,
   dungeon or arena holds 5, a raid or battleground 40, and the role counts can't add up to more than the
   participant limit. **Co-hosts** are typed as Discord usernames separated by commas
   (`@questgiver, @healbot`); a numeric Discord ID also works. A name is found if that person has signed in
   to WoWQuester once (or, when the bot can search the server, from Discord).
2. **Publish**: WoWQuester checks the community limit (by default only one WoW event at a time) and that
   you aren't already in another event then. If it clashes, it says with what. On publish, one post appears
   in the event channel with Join / Tentative / Leave buttons; it updates itself.
3. **Before the event**: approve, waitlist or reject pending signups; change someone's role; add co-hosts.
   Editing the time re-checks everything and asks players to reconfirm. Changing the edition, ruleset,
   faction or realm first shows which signed-up characters would no longer fit; saving asks only those
   players to pick another character. When a co-host (or the host) edits
   the event, the other hosts get a DM listing what changed. Adding roles to an event that people already
   joined asks them (by DM and on the event page) to pick one.
   Players see how many spots each role has left ("Tank · 1 of 1 taken") so they can pick another role to
   balance the group.
4. **Run it**: Start when you begin. End when you're done. If it runs long you get one reminder; use
   "Extend 30 min" if needed. Nothing is extended automatically.
5. **Afterwards**: record attendance (attended / absent / unknown) and write the chronicle. The menus show
   what you saved last time, so saving again only changes what you change.
6. **Copy** makes a fresh draft of the same event, without signups. **Repeating events**: when editing one
   occurrence, choose "only this one" or "this and future".

Co-hosts can run the event but aren't chronicle co-authors unless the author adds them.

## Chronicle (authors and reviewers)

- When an event ends, a chronicle draft is created for the host, and co-hosts and everyone who took part
  get a DM inviting their own story ("Write your story" on the event page; anyone marked absent can't).
  Each story is reviewed on its own. The event has **one** chronicle page and card: the host's story
  first, then co-hosts', then everyone else's in the order they were published.
- The host chooses the **outcome** (Completed, Partly completed, Not completed, Social): WoWQuester never
  guesses it. Other stories need at least "What happened" or a written story.
- The guided answers become the sections of the story: **The plan**, **What happened** (also the summary
  on the chronicle card), **Best moments**, then **The story** if you write one.
- Add screenshots (PNG/JPEG/WebP; they're re-encoded and their metadata removed). Co-authors are ticked
  from the people at the event and can be unticked to remove them; they get a DM when added.
  "Suggest wording" (if enabled) proposes text you can edit; it is never published by itself.
- Every save that changes something is a new version (v1, v2, ...); History shows when and by whom.
- Submit for review. Reviewers approve & publish, request changes, or reject with a note. You can't review
  your own story. Trusted authors can publish their own, but the privacy check still runs.
- Editing a published story creates a new version for review; the current one stays public until the new
  one is approved. Reviewers can unpublish at any time; the author gets a DM with the reason.
- DMs from the bot carry plain links, without Discord's link preview card.
- Published stories appear at `/wow/chronicle/`, in the WoW page's chronicle widget, and in the RSS, Atom
  and JSON feeds. No site update or deployment is involved.

## For admins

- All settings live in `/etc/wowquester/config.toml` (see `configuration.md`). Discord role IDs per
  capability are in `[roles]` and apply within about 30 seconds.
- Day-to-day commands are in `operations.md`. Installation and every production change are in `install.md`.
- When Blizzard publishes a Forever API, run the probe, fill the namespaces, set the statuses, restart.
  No code change is needed.
