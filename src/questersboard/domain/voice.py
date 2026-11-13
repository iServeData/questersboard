"""Per-event voice channels ([voice] in config.toml; docs/followups.md FU-1).

Lifecycle (the worker does every Discord change, from the outbox):
* Start opens one voice channel in [voice] category_id. Only people signed up ([voice] member_states),
  the creator and co-hosts get a member permission; [voice] staff_role_ids (and the event_moderator
  roles when moderators_can_join is on) get a role permission, so they can help without signing up.
* Joining after Start grants access straight away. Leaving keeps the person in the channel if they are
  in it, but they can't rejoin unless they sign up again (remove_on_leave = true disconnects instead).
* Anyone with access can get a guest link (one per person per event) for someone they're playing with.
  The guest signs in with Discord on the site, is added to the server if needed, and gets access to
  this event's voice until it ends. Links, guests and bans are deleted when the event ends.
* Hosts, co-hosts and event moderators can kick (disconnect) or ban (for the rest of the event) anyone
  except the server owner, bots, the protected roles, and (for co-hosts) the event's creator.
* End or Cancel deletes the channel right away. If it has been empty for empty_teardown_minutes after
  the scheduled end, it is deleted too; the event keeps running and a host can reopen voice.
* The bot only counts people in these channels (no names or IDs are stored) for the status card.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from ..config import Config
from ..models import (Event, EventCohost, EventVoiceBan, EventVoiceGuest, EventVoiceInvite, Member, PublicEvent,
                      ServiceHeartbeat, Signup)
from . import audit, outbox, scheduling
from .actor import Actor
from .errors import Invalid, NotFound, PermissionDenied
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
LEFT = PARTICIPANT & ~CONNECT          # left the event: may stay if connected, can't rejoin
BOT = PARTICIPANT | MANAGE_CHANNELS | MANAGE_ROLES | MOVE_MEMBERS
OVERWRITE = {"in": (PARTICIPANT, 0), "left": (LEFT, 0), "banned": (0, VIEW | CONNECT)}
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"     # no 0/o, 1/l/i
CODE_LEN = 8


def channel_name(cfg: Config, ev: Event) -> str:
    return cfg["voice"]["name_template"].format(title=ev.title, id=ev.id)[:100] or f"event-{ev.id}"


BOT_SELF = PARTICIPANT | MANAGE_CHANNELS | MOVE_MEMBERS   # Manage Permissions comes copied from the category


def lock_ops(cfg: Config, bot_user_id: str, bot_role_ids, current: list[dict], keep=()) -> list[tuple]:
    """Make a channel that was created bare in the category (so it copied the category's permissions,
    including the bot role's Manage Permissions) private. Discord only lets a bot attach permissions it
    holds server-wide when creating a channel, and only administrators may set Manage Permissions, so the
    channel is created first and locked straight after. Order matters: the bot keeps View and Connect for
    itself before @everyone loses them (denying Connect on voice also takes away Manage Channels).
    Returns ("put", id, kind, allow, deny) and ("del", id) steps; overwrites in `keep` are left alone."""
    guild = cfg.guild_id
    staff = sorted(cfg.voice_staff_roles())
    own = next((o for o in current if str(o.get("id")) == bot_user_id), None)
    own_allow = int(own.get("allow") or 0) if own else 0      # keeps a copied Manage Permissions
    ops = [("put", bot_user_id, 1, own_allow | BOT_SELF, 0), ("put", guild, 0, 0, VIEW | CONNECT)]
    ops += [("put", rid, 0, PARTICIPANT, 0) for rid in staff]
    mine = {bot_user_id, guild, *staff, *(str(r) for r in bot_role_ids), *(str(k) for k in keep)}
    ops += [("del", str(o["id"])) for o in current if str(o.get("id")) not in mine]
    return ops


def managers(s: Session, ev: Event) -> set[int]:
    return {ev.creator_id} | set(s.scalars(select(EventCohost.member_id).where(EventCohost.event_id == ev.id)))


def wanted_members(s: Session, cfg: Config, ev: Event) -> set[int]:
    """Member ids who should have their own access: signed up in [voice] member_states, plus hosts."""
    states = cfg["voice"]["member_states"]
    return set(s.scalars(select(Signup.member_id).where(Signup.event_id == ev.id, Signup.state.in_(states)))) | \
        managers(s, ev)


def can_join(s: Session, cfg: Config, actor: Actor, ev: Event) -> bool:
    """Signed up (in member_states), a host, holding a staff role, or in as Support Admin."""
    if actor.role_ids & cfg.voice_staff_roles():
        return True
    if actor.discord_id in support_ids(s, ev):
        return True
    if actor.member_id is None:
        return False
    return actor.member_id in wanted_members(s, cfg, ev)


def is_manager(s: Session, cfg: Config, actor: Actor, ev: Event) -> bool:
    return is_event_manager(cfg, actor, ev.creator_id, scheduling.cohost_ids(s, ev.id))


def desired_overwrites(s: Session, cfg: Config, ev: Event, member_ids: dict[int, str],
                       have: dict[str, str]) -> dict[str, str]:
    """{discord_id: in | left | banned} for the channel, from signups, hosts, guests, bans and what was set."""
    want = {member_ids[m]: "in" for m in wanted_members(s, cfg, ev) if m in member_ids}
    for d in s.scalars(select(EventVoiceGuest.discord_id).where(EventVoiceGuest.event_id == ev.id)):
        want[d] = "in"
    if not cfg["voice"]["remove_on_leave"]:
        for d, st in have.items():
            if d not in want and st in ("in", "left"):
                want[d] = "left"
    for d in s.scalars(select(EventVoiceBan.discord_id).where(EventVoiceBan.event_id == ev.id)):
        want[d] = "banned"
    return want


def link(cfg: Config, channel_id: str) -> str:
    return f"https://discord.com/channels/{cfg.guild_id}/{channel_id}"


def request_sync(s: Session, cfg: Config, ev: Event) -> None:
    if cfg["voice"]["enabled"] and (ev.state == "in_progress" or ev.discord_voice_channel_id):
        outbox.enqueue(s, "discord.sync_voice", str(ev.id), {"event_id": ev.id})


def end_session(s: Session, ev: Event) -> None:
    """The event ended or was cancelled: guest links, guests and bans go with it."""
    for model in (EventVoiceInvite, EventVoiceGuest, EventVoiceBan):
        s.execute(delete(model).where(model.event_id == ev.id))


# --------------------------------------------------------------------------- guest links

def guest_url(cfg: Config, code: str) -> str:
    tpl = cfg["voice"]["guest_link_template"]
    if tpl:
        return tpl.replace("{code}", code)
    return cfg["community"]["public_base_url"].rstrip("/") + cfg["web"]["member_path"].rstrip("/") + f"/v/{code}"


def voice_open(ev: Event) -> bool:
    return ev.state == "in_progress" and bool(ev.discord_voice_channel_id)


def invite_for(s: Session, cfg: Config, actor: Actor, event_id: int) -> tuple[Event, str]:
    """The guest link for this person and event (made on first use, the same link after that)."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = s.get(Event, event_id)
    if ev is None:
        raise NotFound("Event not found.")
    if not cfg["voice"]["enabled"] or not cfg["voice"]["guest_invites"]:
        raise Invalid("Guest links are turned off.")
    if not voice_open(ev):
        raise Invalid("Guest links work while the event's voice channel is open.")
    if not can_join(s, cfg, actor, ev):
        raise PermissionDenied("Guest links are for people in the event. Join it first.")
    return ev, _code_for(s, ev, actor.member_id, actor)


def _code_for(s: Session, ev: Event, member_id: int, actor: Actor | None = None) -> str:
    inv = s.scalar(select(EventVoiceInvite).where(EventVoiceInvite.event_id == ev.id,
                                                  EventVoiceInvite.created_by == member_id))
    if inv is not None:
        return inv.code
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    s.add(EventVoiceInvite(code=code, event_id=ev.id, created_by=member_id))
    s.flush()
    if actor is not None:
        audit.record(s, actor, "voice.invite", "event", ev.id)
    return code


def invite_dms(s: Session, cfg: Config, ev: Event) -> int:
    """On Start, with [voice] invite_dm: DM hosts (or everyone with access) their guest link."""
    vc = cfg["voice"]
    if not vc["enabled"] or not vc["guest_invites"] or vc["invite_dm"] == "off":
        return 0
    people = managers(s, ev) if vc["invite_dm"] == "hosts" else wanted_members(s, cfg, ev)
    n = 0
    for mid in sorted(people):
        code = _code_for(s, ev, mid)
        outbox.enqueue(s, "dm", f"voice-invite:{ev.id}:{mid}", {"member_id": mid, "content": invite_dm_text(cfg, ev, code)})
        n += 1
    return n


def invite_dm_text(cfg: Config, ev: Event, code: str) -> str:
    n = cfg["voice"]["max_guests_per_invite"]
    return (f"**Guest voice link for {ev.title}**\n"
            f"{guest_url(cfg, code)}\n\n"
            "Playing with someone who isn't signed up? Send them this link. They sign in with Discord, join "
            f"the {cfg['community']['name']} server if they aren't in it yet, and can hop straight into this "
            "event's voice channel. No character or signup needed.\n"
            f"- Works for up to {n} {'person' if n == 1 else 'people'}, only until the event ends.\n"
            "- Hosts can remove anyone from the channel.\n"
            "- Only share it with people you're playing with. You can get it again with `/voice invite`.")


@dataclass
class Redeemed:
    event: Event
    channel_id: str


def check_invite(s: Session, cfg: Config, code: str, discord_id: str | None = None) -> Event:
    """The event behind a guest link, if the link still works. One message for every dead link.
    With discord_id: also check this person may use it (not banned, link not used up), without using it."""
    code = (code or "").strip().lower()
    inv = s.get(EventVoiceInvite, code) if code and len(code) <= 16 else None
    ev = s.get(Event, inv.event_id) if inv else None
    if ev is None or not cfg["voice"]["enabled"] or not cfg["voice"]["guest_invites"] or not voice_open(ev):
        raise NotFound("This voice link has ended or isn't valid. Links only work while the event is running.")
    if discord_id is not None:
        if s.get(EventVoiceBan, (ev.id, discord_id)) is not None:
            raise PermissionDenied("A host removed you from this event's voice.")
        if s.get(EventVoiceGuest, (ev.id, discord_id)) is None:
            used = s.scalar(select(func.count()).select_from(EventVoiceGuest).where(EventVoiceGuest.code == code))
            if used >= cfg["voice"]["max_guests_per_invite"]:
                raise PermissionDenied("This voice link has been used by as many people as it allows. "
                                       "Ask for a new one.")
    return ev


def redeem(s: Session, cfg: Config, code: str, discord_id: str) -> Redeemed:
    """A guest signed in with Discord: give them this event's voice."""
    ev = check_invite(s, cfg, code, discord_id)
    code = code.strip().lower()
    if s.get(EventVoiceGuest, (ev.id, discord_id)) is None:
        s.add(EventVoiceGuest(event_id=ev.id, discord_id=discord_id, code=code))
    request_sync(s, cfg, ev)
    return Redeemed(ev, ev.discord_voice_channel_id)


# --------------------------------------------------------------------------- host moderation

@dataclass
class Target:
    discord_id: str
    role_ids: frozenset
    is_owner: bool = False
    is_bot: bool = False


def moderate(s: Session, cfg: Config, actor: Actor, event_id: int, target: Target, action: str) -> Event:
    """kick = disconnect now (they can come back if they have access); ban = disconnect and keep out for the
    rest of this event; unban = lift it."""
    if action not in ("kick", "ban", "unban"):
        raise Invalid("Unknown action.")
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = s.get(Event, event_id, with_for_update=True)
    if ev is None:
        raise NotFound("Event not found.")
    require_event_manager(cfg, actor, ev.creator_id, scheduling.cohost_ids(s, ev.id))
    if ev.state != "in_progress":
        raise Invalid("That only works while the event is running.")
    if action != "unban":
        if target.discord_id == actor.discord_id:
            raise Invalid("You can't do that to yourself. Just leave the channel.")
        if target.is_owner or target.is_bot or target.role_ids & cfg.voice_protected_roles():
            raise PermissionDenied("You can't kick or ban the server owner, bots, admins or officers.")
        creator = s.get(Member, ev.creator_id)
        staff = bool(actor.role_ids & cfg.role_ids("event_moderator"))
        if creator is not None and creator.discord_id == target.discord_id and not staff:
            raise PermissionDenied("Co-hosts can't kick or ban the event's host.")
        if action == "kick" and not ev.discord_voice_channel_id:
            raise Invalid("The voice channel isn't open.")
    if action == "ban" and s.get(EventVoiceBan, (ev.id, target.discord_id)) is None:
        s.add(EventVoiceBan(event_id=ev.id, discord_id=target.discord_id, banned_by=actor.member_id))
    if action == "unban":
        s.execute(delete(EventVoiceBan).where(EventVoiceBan.event_id == ev.id,
                                              EventVoiceBan.discord_id == target.discord_id))
    s.flush()
    audit.record(s, actor, f"voice.{action}", "event", ev.id, target=target.discord_id)
    if action in ("ban", "unban"):
        request_sync(s, cfg, ev)
    if action in ("kick", "ban") and ev.discord_voice_channel_id:
        outbox.enqueue(s, "discord.voice_kick", f"{ev.id}:{target.discord_id}:{int(now().timestamp())}",
                       {"event_id": ev.id, "discord_id": target.discord_id})
    return ev


def event_for_command(s: Session, cfg: Config, actor: Actor, channel_id: str | None, manage: bool) -> int:
    """Which running event a /voice command means: the one whose voice chat it was typed in, otherwise the
    only running event (with voice open) this person can use or manage."""
    if channel_id:
        eid = s.scalar(select(Event.id).where(Event.discord_voice_channel_id == str(channel_id),
                                              Event.state == "in_progress"))
        if eid:
            return eid
    actor = ensure_member(s, actor)
    mine = []
    for ev in s.scalars(select(Event).where(Event.state == "in_progress", Event.discord_voice_channel_id.is_not(None))):
        if (is_manager(s, cfg, actor, ev) if manage else can_join(s, cfg, actor, ev)):
            mine.append(ev.id)
    if len(mine) == 1:
        return mine[0]
    if not mine:
        raise Invalid("You're not in a running event with an open voice channel." if not manage else
                      "You're not hosting a running event with an open voice channel.")
    raise Invalid("More than one event is running. Use this command in the event's voice channel chat.")


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


# --------------------------------------------------------------------------- Support Admin

# Support Admin rows live in event_voice_guests with this code (guest link codes are letters and digits
# only), so they get the event's voice like a guest and go when the event ends (end_session).
SUPPORT_CODE = "+support"


def is_support_admin(cfg: Config, actor: Actor) -> bool:
    return bool(actor.role_ids & cfg.support_admin_roles())


def support_ids(s: Session, ev: Event) -> set[str]:
    """Discord ids of the people in this event as Support Admin."""
    return set(s.scalars(select(EventVoiceGuest.discord_id).where(
        EventVoiceGuest.event_id == ev.id, EventVoiceGuest.code == SUPPORT_CODE)))


def _support_note(s: Session, cfg: Config, ev: Event, text: str) -> None:
    if cfg["support_admin"]["announce"] and cfg["event_threads"]["enabled"]:
        outbox.enqueue(s, "discord.thread_message", f"support:{ev.id}:{now().timestamp()}",
                       {"event_id": ev.id, "content": text})


def support_join(s: Session, cfg: Config, actor: Actor, event_id: int) -> Event:
    """An admin steps into the event's chat (and voice, while it's open) without signing up. Visible:
    the chat gets a note, it's in the audit log, and it isn't a signup (no roster spot or character)."""
    require_member(cfg, actor)
    if not is_support_admin(cfg, actor):
        raise PermissionDenied("Support Admin is for server admins. Use Join or Tentative to take part.")
    ev = s.get(Event, event_id, with_for_update=True)
    if ev is None or ev.state not in ("scheduled", "in_progress"):
        raise NotFound("This event isn't open.")
    if s.get(EventVoiceGuest, (ev.id, actor.discord_id)) is None:
        s.add(EventVoiceGuest(event_id=ev.id, discord_id=actor.discord_id, code=SUPPORT_CODE))
        audit.record(s, actor, "event.support_admin_join", "event", ev.id)
        name = _md_plain(actor.display_name or actor.username or "An admin")
        _support_note(s, cfg, ev, f"**{name}** joined as Support Admin (not signed up).")
        outbox.enqueue(s, "discord.render_event", str(ev.id), {"event_id": ev.id})   # chat and voice
    return ev


def support_leave(s: Session, cfg: Config, actor: Actor, event_id: int) -> Event:
    ev = s.get(Event, event_id, with_for_update=True)
    if ev is None:
        raise NotFound("This event isn't open.")
    row = s.get(EventVoiceGuest, (ev.id, actor.discord_id))
    if row is not None and row.code == SUPPORT_CODE:
        s.delete(row)
        audit.record(s, actor, "event.support_admin_leave", "event", ev.id)
        name = _md_plain(actor.display_name or actor.username or "An admin")
        _support_note(s, cfg, ev, f"**{name}** left as Support Admin.")
        outbox.enqueue(s, "discord.render_event", str(ev.id), {"event_id": ev.id})
    return ev


def _md_plain(text: str) -> str:
    return "".join("\\" + c if c in "*_~`|>\\" else c for c in text)[:64].replace("@", "@\u200b")
