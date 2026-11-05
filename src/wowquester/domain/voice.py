"""Per-event voice channels ([voice] in config.toml; docs/followups.md FU-1).

Lifecycle (the worker does every Discord change, from the outbox):
* Start opens one voice channel in [voice] category_id. Only people signed up ([voice] member_states),
  the creator and co-hosts get a member permission; [voice] staff_role_ids (and the event_moderator
  roles when moderators_can_join is on) get a role permission, so they can help without signing up.
* Joining after Start grants access straight away; leaving removes the access and, with
  remove_on_leave, disconnects the person if they are in the channel (staff are never disconnected).
* End or Cancel deletes the channel right away. If it has been empty for empty_teardown_minutes after
  the scheduled end, it is deleted too; the event keeps running and a host can reopen voice.
* The bot only counts people in these channels (no names or IDs are stored) for the status card.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Event, EventCohost, Member, PublicEvent, ServiceHeartbeat, Signup
from . import audit, outbox, scheduling
from .actor import Actor
from .errors import Invalid
from .permissions import is_event_manager, require_event_manager, require_member
from .roster import ensure_member
from .timeutil import now

# Discord permission bits
STREAM = 1 << 9
VIEW = 1 << 10
SEND = 1 << 11
HISTORY = 1 << 16
CONNECT = 1 << 20
SPEAK = 1 << 21
MOVE_MEMBERS = 1 << 24
USE_VAD = 1 << 25
MANAGE_CHANNELS = 1 << 4
MANAGE_ROLES = 1 << 28
PARTICIPANT = VIEW | CONNECT | SPEAK | STREAM | USE_VAD | SEND | HISTORY
BOT = PARTICIPANT | MANAGE_CHANNELS | MANAGE_ROLES | MOVE_MEMBERS


def channel_name(cfg: Config, ev: Event) -> str:
    return cfg["voice"]["name_template"].format(title=ev.title, id=ev.id)[:100] or f"event-{ev.id}"


def creation_overwrites(cfg: Config, bot_user_id: str) -> list[dict]:
    """Hidden from everyone; the bot keeps what it needs to manage the channel; staff roles may join."""
    ows = [{"id": cfg.guild_id, "type": 0, "allow": "0", "deny": str(VIEW | CONNECT)},
           {"id": bot_user_id, "type": 1, "allow": str(BOT), "deny": "0"}]
    ows += [{"id": rid, "type": 0, "allow": str(PARTICIPANT), "deny": "0"} for rid in sorted(cfg.voice_staff_roles())]
    return ows


def managers(s: Session, ev: Event) -> set[int]:
    return {ev.creator_id} | set(s.scalars(select(EventCohost.member_id).where(EventCohost.event_id == ev.id)))


def wanted_members(s: Session, cfg: Config, ev: Event) -> set[int]:
    """Member ids who should have their own access: signed up in [voice] member_states, plus hosts."""
    states = cfg["voice"]["member_states"]
    return set(s.scalars(select(Signup.member_id).where(Signup.event_id == ev.id, Signup.state.in_(states)))) | \
        managers(s, ev)


def can_join(s: Session, cfg: Config, actor: Actor, ev: Event) -> bool:
    """Signed up (in member_states), a host, or holding a staff role."""
    if actor.role_ids & cfg.voice_staff_roles():
        return True
    if actor.member_id is None:
        return False
    return actor.member_id in wanted_members(s, cfg, ev)


def is_manager(s: Session, cfg: Config, actor: Actor, ev: Event) -> bool:
    return is_event_manager(cfg, actor, ev.creator_id, scheduling.cohost_ids(s, ev.id))


def link(cfg: Config, channel_id: str) -> str:
    return f"https://discord.com/channels/{cfg.guild_id}/{channel_id}"


def request_sync(s: Session, cfg: Config, ev: Event) -> None:
    if cfg["voice"]["enabled"] and (ev.state == "in_progress" or ev.discord_voice_channel_id):
        outbox.enqueue(s, "discord.sync_voice", str(ev.id), {"event_id": ev.id})


def reopen(s: Session, cfg: Config, actor: Actor, event_id: int) -> Event:
    """A host reopens voice after it was removed for being empty (the event is still running)."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = s.get(Event, event_id, with_for_update=True)
    if ev is None:
        raise Invalid("That event doesn't exist.")
    require_event_manager(cfg, actor, ev.creator_id, scheduling.cohost_ids(s, ev.id))
    if not cfg["voice"]["enabled"]:
        raise Invalid("Event voice channels are turned off.")
    if ev.state != "in_progress":
        raise Invalid("Voice opens when the event is started.")
    if ev.discord_voice_channel_id:
        raise Invalid("The voice channel is already open.")
    ev.voice_closed_at = None
    audit.record(s, actor, "event.voice_reopen", "event", ev.id)
    request_sync(s, cfg, ev)
    return ev


# --------------------------------------------------------------------------- head counts (bot)

def record_counts(s: Session, counts: dict[str, int], at: datetime | None = None) -> int:
    """The bot reports how many people (not bots) are in each event voice channel it can see.

    Stores only the number: voice_empty_since for the empty-channel rule, and public_event_voice for
    the status card. Returns how many events were updated."""
    at = at or now()
    if not counts:
        return 0
    n = 0
    for ev in s.scalars(select(Event).where(Event.discord_voice_channel_id.in_(list(counts)))):
        c = max(0, int(counts[ev.discord_voice_channel_id]))
        if c == 0 and ev.voice_empty_since is None:
            ev.voice_empty_since = at
        elif c > 0:
            ev.voice_empty_since = None
        if s.get(PublicEvent, ev.public_id) is not None:
            s.execute(text("""
                INSERT INTO public_event_voice (public_id, in_voice, observed_at) VALUES (:p, :c, :at)
                ON CONFLICT (public_id) DO UPDATE SET in_voice = EXCLUDED.in_voice, observed_at = EXCLUDED.observed_at
            """), {"p": ev.public_id, "c": c, "at": at})
        n += 1
    return n


def open_channels(s: Session) -> list[str]:
    return list(s.scalars(select(Event.discord_voice_channel_id).where(Event.discord_voice_channel_id.is_not(None))))


def bot_watching(s: Session, cfg: Config, at: datetime | None = None) -> bool:
    """True while the bot is connected and checking in, so its empty-channel readings can be trusted."""
    hb = s.get(ServiceHeartbeat, "bot")
    at = at or now()
    return hb is not None and hb.state == "connected" and \
        at - hb.beat_at <= timedelta(seconds=cfg["status"]["stale_after_seconds"])


def due_for_teardown(s: Session, cfg: Config, at: datetime | None = None) -> list[int]:
    """Running events whose channel has been empty for empty_teardown_minutes after the scheduled end."""
    at = at or now()
    wait = timedelta(minutes=cfg["voice"]["empty_teardown_minutes"])
    out = []
    for ev in s.scalars(select(Event).where(Event.state == "in_progress", Event.discord_voice_channel_id.is_not(None),
                                            Event.voice_empty_since.is_not(None), Event.end_at < at)):
        if at - max(ev.voice_empty_since, ev.end_at) >= wait:
            out.append(ev.id)
    return out


def discord_ids(s: Session, member_ids) -> dict[int, str]:
    return dict(s.execute(select(Member.id, Member.discord_id).where(
        Member.id.in_(set(member_ids)), Member.erased_at.is_(None))).all())
