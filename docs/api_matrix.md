# HTTP routes, Discord surface and Blizzard namespaces

## Web routes

Prefixes come from `[web] member_path` (default `/wow/quester`) and `[web] chronicle_path`
(default `/wow/chronicle`). Nginx proxies both to the unix socket; `/wow/chronicle/` allows GET/HEAD only.
All POSTs need a session, a CSRF token and a same-origin `Origin`/`Referer`; all are rate limited.

### Public (no login; projection tables only)

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/wow/chronicle/` | archive of published stories |
| GET | `/wow/chronicle/{slug}/` | story page |
| GET | `/wow/chronicle/media/{name}` | approved images (WebP), including a story's chosen banner |
| GET | `/wow/quester/covers/{name}` | event cover art (WebP; uuid or configured default names only); public so Discord can show it |
| GET | `/wow/chronicle/feed.json` | JSON Feed 1.1 |
| GET | `/wow/chronicle/feed.xml` | RSS 2.0 |
| GET | `/wow/chronicle/feed.atom` | Atom |
| GET | `/wow/chronicle/latest.json` | latest stories for site widgets |
| GET | `/wow/quester/upcoming.json` | upcoming and launched events (counts only) for the WoW page; CORS for the site origin; cached 60 s |
| GET | `/wow/quester/status.json` | Community status + Next event cards: bot/worker check-ins, Discord connection, the launched or next event (phase, role counts, needs, voice head count). No names or IDs; CORS for the site origin; cached `[status] cache_seconds` |
| GET | `/wow/quester/calendar.ics` | public calendar of scheduled events (`[calendar] public_enabled`) |
| GET | `/wow/quester/privacy` | public privacy page, generated from `[community]`, `[retention]`, `[uploads]`, `[legal]` and which integrations are on |
| GET | `/wow/quester/terms` | public terms of use; contact line from `[legal] contact` |
| GET | `/wow/quester/calendar/{token}.ics` | private calendar (hashed, revocable token); `no-referrer` |
| GET | `/healthz` | `{"status":"ok"}` or 503 `db_unavailable`; not rate limited |

### Members (Discord login)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/wow/quester/login`, `/wow/quester/oauth/callback` | Discord OAuth (`identify`), single-use state |
| POST | `/wow/quester/logout` | end session |
| GET | `/wow/quester/` | home: upcoming, my signups, recommendations |
| GET/POST | `/wow/quester/events/new` | create draft (or a recurring series) |
| GET | `/wow/quester/events/{id}` | event page |
| GET/POST | `/wow/quester/events/{id}/edit` | edit; for series: this one or this and future |
| POST | `/wow/quester/events/{id}/{action}` | `join tentative leave reconfirm change publish start end cancel discard extend copy attendance` |
| POST | `/wow/quester/signups/{id}/{decision}` | `approve waitlist reject role` |
| GET/POST | `/wow/quester/characters` | list / add or edit |
| POST | `/wow/quester/characters/{id}/remove` | archive a character |
| POST | `/wow/quester/characters/{id}/lookup` | "Check with Blizzard" (existence only) |
| GET | `/wow/quester/characters/battlenet?edition=&region=` | start optional ownership link |
| GET | `/wow/quester/battlenet/callback` | Battle.net OAuth (`wow.profile`), single-use state; shows verified and addable characters |
| POST | `/wow/quester/characters/battlenet/import` | add ticked characters from the member's own encrypted list (`pick`, `ruleset_<n>`), within `battlenet_import_minutes` |
| POST | `/wow/quester/characters/battlenet/unlink` | forget the Battle.net snapshot |
| GET/POST | `/wow/quester/me` | preferences, consent default, time zone |
| POST | `/wow/quester/me/calendar` | create (replacing any old one) or revoke the private calendar link |
| GET | `/wow/quester/chronicle/{id}` | chronicle editor / review view |
| POST | `/wow/quester/chronicle/{id}/{action}` | `save submit coauthors suggest clear upload remove_upload banner review unpublish remove_me` |
| GET | `/wow/quester/uploads/{id}.webp` | unpublished upload preview (authors/reviewers only) |
| GET | `/wow/quester/review` | review queue (reviewers) |
| GET | `/wow/quester/api/options?edition=&kind=&q=` | autocomplete (race, class, quest, location…); always allows free text |

## Discord

| Surface | Detail |
| --- | --- |
| Intents | Guilds only; no privileged intents |
| Bot permissions | 84992 = View Channel, Send Messages, Embed Links, Read Message History |
| Slash commands | `/quest list · my · join · leave · create` (create saves a draft), `/character add · list · edit · remove`; edition autocomplete |
| Components | persistent buttons on the canonical event post (`wq:ev:<action>:<event_id>`), re-registered at startup |
| Messages | worker only, via REST: one canonical post per event (edited in place, recreated if deleted); DMs for reminders, approvals, review results; `allowed_mentions` empty |
| Event threads (off by default) | `POST /channels/{ch}/messages/{msg}/threads` (public) or `POST /channels/{ch}/threads` type 12 (private); `PUT`/`DELETE /channels/{thread}/thread-members/{user}`; `PATCH /channels/{thread}` `archived`+`locked`. Needs Create Public/Private Threads, Send Messages in Threads, Manage Threads. FIXTURE-tested; live unverified |
| Registration | `wowquester-admin sync-commands` is a dry run unless `--yes` and `WOWQUESTER_APPROVED_CHANGE=sync-commands` |

## Blizzard

| Edition | Namespace config | Character lookup | Ownership | Reference data |
| --- | --- | --- | --- | --- |
| Retail | `profile-{region}`, `static-{region}` | `/profile/wow/character/{realm}/{name}` (realm required) | `/profile/user/wow` with `wow.profile` | `/data/wow/playable-race/index`, `/data/wow/playable-class/index`, … |
| WoW: Forever | empty: no published namespace | **unsupported**: refused, zero requests | **unsupported** | packaged seed catalog, labelled `unverified` |
| Instance details | `static-{region}` | | | `/data/wow/journal-instance/{id}`: `modes`, `minimum_level`, `category` (`[blizzard] instance_details`); FIXTURE-tested, live unverified |
| any `*ptr*` | rejected by config validation | | | |

OAuth: client credentials for game data (token cached until expiry); authorization code for the
optional ownership link (user token used once, never stored). Retries with backoff on 429/5xx
honouring `Retry-After`; results cached in `api_cache`.
