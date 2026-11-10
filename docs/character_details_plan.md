# Character details, sharing and guild pages

Status: phases 1–5 built.

## Why

Members asked for more than level, race and class: gear, talents and professions, so guildmates can
critique builds and coordinate crafting. Blizzard's character pages provide all of this. A probe on
2026-11-10 found every page answering for a WoW: Forever US character (`blizzard probe-character`).
EU is unverified.

## Rules that hold in every phase

- **Opt in, per group.** Nothing is imported until the member presses Import. Every group is ticked by
  default and can be unticked. Unticked groups aren't even requested from Blizzard.
- **Sharing per group:** Only me, My guild, or All QuestersBoard members. Anything beyond "Only me"
  counts only while the character is verified as the member's through Battle.net. "My guild" also
  needs a guild on the imported profile. This is checked whenever details are shown, so losing the
  verification makes everything private at once.
- **No one else's data.** Guild pages list only characters whose owners imported and shared details.
  Blizzard's guild roster isn't stored and never shows other players' names.
- **Blizzard's 30-day limit.** Imported details are deleted `data_ttl_days` after the last import or
  refresh. They're also deleted on Remove imported details, when the character is removed, archived
  or renamed, and on Remove me.
- **Curated, not raw.** Only the fields listed below are stored, never the full Blizzard answer.

## Groups

| Group | Blizzard page | Stored |
|---|---|---|
| Overview | profile | level, average and equipped item level, achievement points |
| Guild | profile | guild ID and name |
| Gear | /equipment | slot, item name, item level, quality, item ID, enchant text |
| Talents | /specializations | per group: active flag, tree names, points, talent names and ranks |
| Professions | /professions | name, primary/secondary, skill and maximum |
| Stats | /statistics | a fixed list (health, attributes, armor, crit, hit, defenses, resistances) |
| Portrait | /character-media | image URLs on `[blizzard] render_image_hosts` only; QuestersBoard fetches the picture and serves it, so browsers never contact Blizzard |
| PvP | /pvp-summary | honorable kills, rank |
| Progress | /reputations, /quests/completed, /achievements, /collections/mounts, /hunter-pets | standings; counts of quests, achievements, mounts; pet name, level, family |

## Phases

1. **Probe** (done): `blizzard probe-character`, with `--detail PAGE` and guild/roster sizes.
   **Media probe**: `blizzard probe-media` checks item, icon, spell and talent-tree answers before the
   Armory-style gear icons, tooltips and talent trees are built (done; all answered on Forever US).
   It also follows one spec tree and its class tree, to read the layout for Build 2.
   **Build 1** (done): gear icons and item tooltips, talent icons and talent tooltips, all from
   Blizzard. Icons come through `/characters/<id>/icon/<group>/<ref>` with the same sharing check as
   the page; they are held in memory (`icon_cache_hours`, `icon_cache_entries`) and never stored.
   Existing imports need "Refresh from Blizzard" to get them.
   **Build 2** (pending the tree-layout probe): talent trees laid out like the in-game panel.
2. **Config** (done): Forever's comment records the US result; the status stays per edition.
3. **Import with checkboxes and sharing choices** (done): on My characters, per character. Stored in
   `character_profiles` (migration 0009).
4. **Members' view** (done): `/characters/<id>/view`, a read-only page showing only the groups shared
   with the viewer (the owner sees everything). Nothing shared with you: 404. `/players` lists
   characters with groups shared with all members.
5. **Guild roster page** (done): `/guilds/<edition>/<region>/<guild id>`, there automatically once a
   verified member shares guild membership, and visible only to members who have a verified character
   that shares the same guild (to see the page you must be on it). It lists the shared characters
   and filters by class, role, spec, level, item level and profession. Clicking a character opens the
   phase 4 view.

Later: refresh on a schedule. This needs the worker to call Blizzard, and the 30-day limit still applies.
