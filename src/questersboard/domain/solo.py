"""Solo adventure stories ([solo_stories]).

A member writes up something they did on their own, about one of their characters. Behind each story is a
private event record (events.kind = "solo"): already completed, never listed, posted to Discord, put in
calendars or offered for signups. The author is recorded as its one participant, with their own choice of
being named, so the story goes through exactly the same review, screenshot clearing, privacy checks,
"Stop naming me" and Remove me as an event story.

The social side: readers can press "Join me next time" (chronicle_interest). The author sees who, and can
plan a group event from the story in one step; when it's published, everyone who asked is told.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import (Attendance, Character, ChronicleEntry, ChronicleInterest, ChronicleRevision, ChronicleUpload,
                      Event, Member, Signup)
from . import audit, outbox, projections
from .actor import Actor
from .errors import Invalid, InvalidTransition, NotFound, PermissionDenied
from .permissions import require_member
from .roster import ensure_member
from .timeutil import now, resolve_local, zone

ACTIVITY_TYPES = ("quest", "dungeon", "raid", "world_pvp", "battleground", "arena", "rp", "custom")
ACTIVITY_LABELS = {"quest": "Questing or exploring", "dungeon": "Dungeon", "raid": "Raid", "world_pvp": "World PvP",
                   "battleground": "Battleground", "arena": "Arena", "rp": "Roleplay", "custom": "Something else"}
MAX_DAYS_BACK = 366


def is_solo(ev: Event | None) -> bool:
    return ev is not None and ev.kind == "solo"


def allowed(cfg: Config, actor: Actor | None) -> bool:
    return actor is not None and cfg.solo_allowed(actor.role_ids)


def _require_allowed(cfg: Config, actor: Actor) -> None:
    require_member(cfg, actor)
    if not cfg["solo_stories"]["enabled"]:
        raise PermissionDenied("Solo adventure stories are turned off.")
    if not allowed(cfg, actor):
        raise PermissionDenied("Solo adventure stories are open to some roles only, and yours isn't one of them.")


def started_recently(s: Session, member_id: int) -> int:
    """Solo stories this member started in the last 7 days (the [solo_stories] per_week limit)."""
    return s.scalar(select(func.count()).select_from(Event).where(
        Event.kind == "solo", Event.creator_id == member_id, Event.created_at > now() - timedelta(days=7))) or 0


def left_this_week(s: Session, cfg: Config, member_id: int | None) -> int | None:
    """How many more the member can start now; None = no limit."""
    cap = cfg["solo_stories"]["per_week"]
    if not cap:
        return None
    return max(0, cap - (started_recently(s, member_id) if member_id else 0))


def start(s: Session, cfg: Config, actor: Actor, character_id: int, title: str, day: date,
          activity_type: str = "quest", named: bool = False, tz: str | None = None) -> ChronicleEntry:
    """Create the private record and the story draft. Returns the entry (open it to write)."""
    _require_allowed(cfg, actor)
    actor = ensure_member(s, actor)
    s.execute(select(Member.id).where(Member.id == actor.member_id).with_for_update())   # one at a time per member
    left = left_this_week(s, cfg, actor.member_id)
    if left == 0:
        cap = cfg["solo_stories"]["per_week"]
        raise Invalid(f"You can start {cap} solo stor{'y' if cap == 1 else 'ies'} in 7 days. "
                      "Finish one you've started, or try again in a few days.")
    ch = s.get(Character, int(character_id or 0))
    if ch is None or ch.member_id != actor.member_id or ch.archived_at is not None:
        raise Invalid("Pick one of your characters.")
    if ch.edition not in cfg.editions:
        raise Invalid("That character's game version isn't offered here.")
    t = str(title or "").strip()
    if not 3 <= len(t) <= 120:
        raise Invalid("Title must be 3–120 characters.")
    if activity_type not in ACTIVITY_TYPES:
        raise Invalid("Pick what kind of adventure it was.")
    m = s.get(Member, actor.member_id)
    tz = tz or (m.timezone if m and m.timezone else None) or cfg["community"]["default_timezone"]
    zone(tz)
    today = now().astimezone(zone(tz)).date()
    if not isinstance(day, date):
        raise Invalid("Pick the day it happened.")
    if day > today:
        raise Invalid("Pick a day that has already happened.")
    if day < today - timedelta(days=MAX_DAYS_BACK):
        raise Invalid("Solo stories can be about the last year or so.")
    start_at = resolve_local(datetime.combine(day, time(12, 0)), tz)
    ev = Event(kind="solo", edition=ch.edition, region=ch.region, ruleset=ch.ruleset, realm_slug=ch.realm_slug,
               faction=ch.faction, title=t, activity_type=activity_type, host_character_id=ch.id,
               creator_id=actor.member_id, start_at=start_at, end_at=start_at + timedelta(hours=1), timezone=tz,
               actual_start=start_at, actual_end=start_at + timedelta(hours=1), state="completed",
               participant_limit=1, chronicle_enabled=True, public_listing=False)
    s.add(ev)
    s.flush()
    s.add(Signup(event_id=ev.id, member_id=actor.member_id, character_id=ch.id, state="confirmed",
                 recognition=bool(named)))
    s.add(Attendance(event_id=ev.id, member_id=actor.member_id, character_id=ch.id, status="attended",
                     recorded_by=actor.member_id))
    s.flush()
    from . import chronicle
    e = chronicle.ensure_draft(s, cfg, ev, actor.member_id)
    audit.record(s, actor, "chronicle.solo_start", "chronicle", e.id, activity=activity_type)
    return e


def entry_event(s: Session, e: ChronicleEntry) -> Event:
    return s.get(Event, e.event_id)


def set_named(s: Session, cfg: Config, actor: Actor, entry_id: int, named: bool) -> None:
    """The author changes whether their character is named on their own solo story."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    from . import chronicle
    e = chronicle.get(s, entry_id, lock=True)
    ev = entry_event(s, e)
    if not is_solo(ev) or e.author_id != actor.member_id:
        raise PermissionDenied("Only the author can change this.")
    su = s.scalar(select(Signup).where(Signup.event_id == ev.id, Signup.member_id == actor.member_id)
                  .order_by(Signup.id.desc()).limit(1))
    if su is None:
        raise NotFound("Story not found.")
    if bool(named) and not su.recognition:
        from ..models import RecognitionRemoval
        s.execute(delete(RecognitionRemoval).where(RecognitionRemoval.event_id == ev.id,
                                                   RecognitionRemoval.member_id == actor.member_id))
    su.recognition, su.updated_at = bool(named), now()
    s.flush()
    if e.published_revision_id:
        projections.refresh_chronicle(s, cfg, e)
    audit.record(s, actor, "chronicle.solo_named", "chronicle", e.id, named=bool(named))


def discard(s: Session, cfg: Config, actor: Actor, entry_id: int) -> None:
    """Delete a solo story that was never published (the author changed their mind)."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    from . import chronicle
    e = chronicle.get(s, entry_id, lock=True)
    ev = entry_event(s, e)
    if not is_solo(ev) or e.author_id != actor.member_id:
        raise PermissionDenied("Only the author can delete this story.")
    if e.published_revision_id is not None or e.first_published_at is not None:
        raise InvalidTransition("This story has been published. A reviewer can take it down instead.")
    from pathlib import Path
    for u in s.scalars(select(ChronicleUpload).where(ChronicleUpload.entry_id == e.id)):
        try:
            (Path(cfg["uploads"]["storage_dir"]) / u.filename).unlink(missing_ok=True)
        except OSError:
            pass
    e.banner_upload_id = None
    s.flush()
    s.execute(delete(ChronicleUpload).where(ChronicleUpload.entry_id == e.id))
    s.execute(delete(ChronicleRevision).where(ChronicleRevision.entry_id == e.id))
    s.delete(e)
    s.flush()
    s.execute(delete(Attendance).where(Attendance.event_id == ev.id))
    s.execute(delete(Signup).where(Signup.event_id == ev.id))
    from ..models import RecognitionRemoval
    s.execute(delete(RecognitionRemoval).where(RecognitionRemoval.event_id == ev.id))
    from sqlalchemy import update
    s.execute(update(Event).where(Event.copied_from_id == ev.id).values(copied_from_id=None))   # planned events stay
    s.delete(ev)
    audit.record(s, actor, "chronicle.solo_discard", "chronicle", entry_id)


def mine(s: Session, member_id: int) -> list[tuple[ChronicleEntry, Event, ChronicleRevision, int]]:
    """The member's solo stories, newest first: (entry, record, working revision, interested count)."""
    rows = s.execute(select(ChronicleEntry, Event).join(Event, Event.id == ChronicleEntry.event_id)
                     .where(Event.kind == "solo", ChronicleEntry.author_id == member_id)
                     .order_by(Event.start_at.desc(), ChronicleEntry.id.desc())).all()
    out = []
    for e, ev in rows:
        n = s.scalar(select(func.count()).select_from(ChronicleInterest)
                     .where(ChronicleInterest.entry_id == e.id)) or 0
        out.append((e, ev, s.get(ChronicleRevision, e.working_revision_id), n))
    return out


# --------------------------------------------------------------------------- Join me next time

def published_solo(s: Session, public_id: str) -> tuple[ChronicleEntry, Event]:
    import uuid
    try:
        pid = uuid.UUID(str(public_id))
    except ValueError:
        raise NotFound("Story not found.") from None
    e = s.scalar(select(ChronicleEntry).where(ChronicleEntry.public_id == pid))
    ev = s.get(Event, e.event_id) if e else None
    if e is None or not is_solo(ev) or e.state == "unpublished" or e.published_revision_id is None:
        raise NotFound("That story isn't published (or was removed).")
    return e, ev


def interested(s: Session, entry_id: int) -> list[tuple[int, str]]:
    """(member_id, name) of everyone who pressed Join me next time, oldest first. For the author only."""
    rows = s.execute(select(Member).join(ChronicleInterest, ChronicleInterest.member_id == Member.id)
                     .where(ChronicleInterest.entry_id == entry_id, Member.erased_at.is_(None))
                     .order_by(ChronicleInterest.created_at)).scalars()
    return [(m.id, m.display_name or (("@" + m.username) if m.username else "a member")) for m in rows]


def is_interested(s: Session, entry_id: int, member_id: int | None) -> bool:
    return bool(member_id) and s.get(ChronicleInterest, (entry_id, member_id)) is not None


def join_me(s: Session, cfg: Config, actor: Actor, public_id: str, want: bool = True) -> ChronicleEntry:
    """A reader says they'd like to come along next time (want=False takes it back)."""
    require_member(cfg, actor)
    if not cfg["solo_stories"]["join_me"]:
        raise PermissionDenied("Join me next time is turned off.")
    actor = ensure_member(s, actor)
    e, ev = published_solo(s, public_id)
    if e.author_id == actor.member_id:
        raise Invalid("That's your own story.")
    row = s.get(ChronicleInterest, (e.id, actor.member_id))
    if want and row is None:
        s.add(ChronicleInterest(entry_id=e.id, member_id=actor.member_id))
        s.flush()
        outbox.enqueue(s, "notify.join_me", f"{e.id}:{actor.member_id}",
                       {"entry_id": e.id, "member_id": actor.member_id})
        audit.record(s, actor, "chronicle.join_me", "chronicle", e.id)
    elif not want and row is not None:
        s.delete(row)
        audit.record(s, actor, "chronicle.join_me_withdraw", "chronicle", e.id)
    return e


def plan_event(s: Session, cfg: Config, actor: Actor, entry_id: int, start_at: datetime, end_at: datetime,
               tz: str) -> Event:
    """The author turns their solo story into a group event draft. Everyone who pressed Join me next time
    is told once it's published (see events.publish)."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    from . import chronicle, events
    e = chronicle.get(s, entry_id)
    src = entry_event(s, e)
    if not is_solo(src) or e.author_id != actor.member_id:
        raise PermissionDenied("Only the story's author can plan an event from it.")
    rev = s.get(ChronicleRevision, e.published_revision_id or e.working_revision_id)
    title = (rev.title if rev else src.title)[:120]
    data = dict(edition=src.edition, region=src.region, ruleset=src.ruleset, realm_slug=src.realm_slug,
                faction=src.faction or "any", title=title, activity_type=src.activity_type,
                description=f"Following on from the solo adventure “{title}”."[:2000],
                start_at=start_at, end_at=end_at, timezone=tz, chronicle_enabled=True, public_listing=True)
    ev = events.create_draft(s, cfg, actor, data)
    ev.copied_from_id = src.id
    if src.host_character_id:
        ch = s.get(Character, src.host_character_id)
        if ch is not None and ch.archived_at is None and ch.member_id == actor.member_id:
            ev.host_character_id = ch.id
    audit.record(s, actor, "chronicle.solo_plan", "event", ev.id, entry=e.id)
    return ev


def solo_source(s: Session, ev: Event) -> tuple[ChronicleEntry, Event] | None:
    """The solo story an event was planned from, if any."""
    if not ev.copied_from_id:
        return None
    src = s.get(Event, ev.copied_from_id)
    if not is_solo(src):
        return None
    e = s.scalar(select(ChronicleEntry).where(ChronicleEntry.event_id == src.id).limit(1))
    return (e, src) if e else None


__all__ = ["ACTIVITY_TYPES", "allowed", "start", "set_named", "discard", "mine", "join_me", "interested",
           "is_interested", "plan_event", "solo_source", "is_solo", "left_this_week", "published_solo"]
