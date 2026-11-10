# Site widget

`wow_quester.js` adds three things to a static page, all from public QuestersBoard JSON:

- **Upcoming list** (`[data-wq-upcoming]`, `<member_path>/upcoming.json`): title, times, edition,
  ruleset, type, link. The container's existing children are the static fallback.
- **Community status card** (`[data-wq-status]`, `<member_path>/status.json`, polled every 60 s):
  guild events (live / next), event board, bot, Discord connection, posts & reminders (worker). Rows
  start as Unknown; a missing, old (`expiry.public_json_seconds`) or failed report never shows green.
- **Next event card** (`[data-wq-next]`, same status.json): the launched event, else the next one.
  "Live now" only after an organiser launches it; past its planned end it reads "running over"
  (events are never ended automatically). Shows signed-up/capacity, what roles are still needed, and
  the voice head count once per-event voice channels exist (FU-1).

Elements with `data-wq-until="<ISO instant>"` are removed after that instant (a launch-day fallback,
for example). Without JavaScript they stay. See the header comment in the file for markup. The exact
patch for a specific community's WoW page is kept with that site's deployment notes, not here.
