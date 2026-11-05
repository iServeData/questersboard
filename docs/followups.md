# Follow-ups

Approved ideas that are not built yet. Each one needs its own prototype pass, tests and approval
before it reaches main.

## FU-1 Launch voice channel per event

Status: **built** (off until `[voice] enabled = true`). Requested during install, after go-live of the core
release. Live Discord behaviour is UNVERIFIED until it is switched on; the tests use a FIXTURE Discord.

Decisions: a member who cancels during the event is **removed from voice** (`remove_on_leave = true`), and
`event_moderator` roles plus any `staff_role_ids` (e.g. admins) can join without signing up.

### Behaviour

- **Launch.** The event's owning stakeholders (creator, co-hosts, `event_moderator` roles) get a
  Launch button. It uses the existing Start transition (allowed from 2 hours before start) and also
  creates one voice channel for the event in a configured category.
- **No automatic end.** The event stays in progress until an owning stakeholder clicks End event,
  because groups often play past the scheduled end. This is already how the core release behaves:
  the worker only DMs the owners once after `overrun_grace_minutes` and never ends the event.
- **End event removes the channel.** When an owning stakeholder ends or cancels the event, the voice
  channel is deleted right away, even if people are still in it.
- **Empty after the scheduled end.** If the channel has had nobody in it for more than 5 minutes
  after the scheduled end time, the bot deletes the channel. The event itself stays in progress
  until someone clicks End event.
- **RSVP-only access.** Join voice is shown only to members who have RSVPed. Members can RSVP
  until the event is ended (by the End event button or the bot's End command). RSVPing after launch
  grants access straight away.
- **Participant permissions in the channel.** RSVPed members can connect, speak, use video and
  screen share, and use the voice channel's text chat (send messages, read history).
- **One channel per event.** The channel ID is stored with the event, so repeated clicks or a bot
  restart can't create duplicates. If the bot was offline when it should have deleted a channel,
  it deletes the channel when it comes back.

### Settings (`[voice]` in config.toml, restart)

| key | example | meaning |
|---|---|---|
| `enabled` | `false` | feature switch |
| `category_id` | `""` | Discord category the channels are created in |
| `name_template` | `"{title}"` | channel name, trimmed to Discord's 100-character limit |
| `user_limit` | `0` | 0 = no cap; otherwise the maximum number of people in the channel |
| `empty_teardown_minutes` | `5` | how long the channel must be empty, after the scheduled end, before it is deleted |
| `member_states` | `["confirmed", "tentative"]` | which signups get access (creator and co-hosts always do) |
| `moderators_can_join` | `true` | `[roles] event_moderator` roles can join every event voice channel without signing up |
| `staff_role_ids` | `[]` | more server roles (e.g. admins) that can join every event voice channel |
| `remove_on_leave` | `true` | disconnect someone who leaves the event while they are in its channel (staff are never disconnected) |

### Discord requirements and open questions

- **Manage Channels**, granted only on the configured category (not server-wide), so the bot can
  create and delete its channels.
- **Manage Permissions on its own channels.** RSVP-only access needs per-member permission
  overwrites on each channel. Discord only lets a bot grant permissions it holds itself, so the bot
  also needs Connect, Speak, Video (stream), Send Messages and Read Message History in that category.
  It should not need server-wide Manage Roles.
- **Voice-state events** (non-privileged `voice_states` intent), only for the "empty for 5 minutes"
  rule. The bot then receives join/leave events for the server's voice channels; it should keep
  only the counts for its own channels and store nothing else.
- **Status card hook (built).** The bot writes the head count to `public_event_voice`
  (public_id, in_voice, observed_at; no names or IDs). `status.json` shows it only while the event is
  launched and the count is newer than `[status] voice_stale_after_seconds`, so the site's Next event
  card picks it up without a site change. The row goes when the event's public projection does.
- **Move Members** on the category, so people who cancel can be disconnected.
- Decided: a member who cancels is removed from the channel (`remove_on_leave`).
- Decided: event moderators and `staff_role_ids` can join without signing up.

### How it works (built)

- Start (web, or **Organizer → Start event** in Discord) opens the channel; the worker creates it hidden
  from everyone, then gives each signed-up member, the creator and co-hosts their own access. Staff roles
  get a role permission when the channel is created.
- The event post gets a **Join voice** button. Signed-up members and staff get a private link to the
  channel; anyone else is told to press Join first. The web event page shows the same link.
- The worker keeps access in step after every signup change; repeated syncs and restarts never make a
  second channel. End or Cancel deletes it.
- The bot counts people in these channels (numbers only) for the status card and the empty rule. The
  worker only removes an empty channel while the bot is connected and checking in, so it never guesses.
- After an empty removal, the event keeps running and the thread says so; a host can use
  **Organizer → Reopen voice** (or Reopen voice on the web page). If someone deletes the channel by hand in
  Discord, it is treated the same way.
