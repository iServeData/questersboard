# Follow-ups

Approved ideas that are not built yet. Each one needs its own prototype pass, tests and approval
before it reaches main.

## FU-1 Launch voice channel per event

Status: follow-up (not started). Requested during install, after go-live of the core release.

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

### Settings (planned, `[voice]` in config.toml)

| key | example | meaning |
|---|---|---|
| `enabled` | `false` | feature switch |
| `category_id` | `""` | Discord category the channels are created in |
| `name_template` | `"{title}"` | channel name, trimmed to Discord's 100-character limit |
| `user_limit` | `0` | 0 = no cap; otherwise the maximum number of people in the channel |
| `empty_teardown_minutes` | `5` | how long the channel must be empty, after the scheduled end, before it is deleted |

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
- Open: should a member who cancels their RSVP be removed from the channel, or only lose the
  ability to rejoin?
- Open: should event moderators who did not RSVP be able to join to help?
