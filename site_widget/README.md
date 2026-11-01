# Site widget

`wow_quester.js` fills a container on a static page with upcoming WoWQuester events from
`<member_path>/upcoming.json` (public projection only: title, times, edition, ruleset, type, link).
The container's existing children are the static fallback and remain when the app is unreachable
or nothing is scheduled. See the header comment in the file for markup. The exact patch for a
specific community's WoW page is kept with that site's deployment notes, not in this repository.
