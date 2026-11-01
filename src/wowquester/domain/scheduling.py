"""Reservations: the two independent safeguards.

1. Community-wide WoW capacity (event_reservations): at most
   [scheduling] max_simultaneous_wow_events overlapping WoWQuester events, and nothing
   may overlap an exclusive event. Enforced by a PostgreSQL trigger under a global
   advisory lock.
2. Member-level conflicts (member_reservations): one person cannot organize, co-host
   or be confirmed in two overlapping WoW events, whatever character they bring.
   Enforced by a PostgreSQL exclusion constraint.

Only WoWQuester events are ever written here. Other Discord scheduled events are never
read, so they cannot block or be blocked.

Which states hold reservations:
  draft                 -> none
  scheduled/in_progress -> planned [start_at, end_at)
  completed/cancelled   -> none (released)
Pending, tentative and waitlisted signups never hold a member reservation.
"""

from __future__ import annotations

import re

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from ..db import schedule_lock
from ..models import Event, EventCohost, EventReservation, Member, MemberReservation
from .errors import Conflict
from .timeutil import fmt_local, rng

HOLDING = ("scheduled", "in_progress")


def _member_label(s: Session, member_id: int) -> str:
    m = s.get(Member, member_id)
    return m.display_name if m and m.display_name else "a member"


def translate(exc: Exception, s: Session | None = None) -> Conflict | None:
    msg = str(getattr(exc, "orig", exc))
    m = re.search(r"wq_exclusive_conflict:(\d+)", msg)
    if m:
        other = s.get(Event, int(m.group(1))) if s else None
        when = f" ({other.title}, {fmt_local(other.start_at, other.timezone)})" if other else ""
        return Conflict("That time overlaps an exclusive WoW event" + when + ".", reason="exclusive",
                        event_id=int(m.group(1)))
    m = re.search(r"wq_capacity_conflict:(\d+):(\d+)", msg)
    if m:
        lim = int(m.group(2))
        return Conflict(f"Another WoW event is already scheduled then. The community allows {lim} "
                        f"simultaneous WoW event{'s' if lim != 1 else ''}.", reason="capacity", limit=lim)
    if "ex_member_overlap" in msg:
        mm = re.search(r"\(member_id, during\)=\((\d+),", msg)
        who = _member_label(s, int(mm.group(1))) if (mm and s) else "A member"
        return Conflict(f"{who} is already committed to another WoW event at that time.", reason="member",
                        member_id=int(mm.group(1)) if mm else None)
    m = re.search(r"wq_role_full:(\w+)", msg)
    if m:
        return Conflict(f"The {m.group(1)} slots are full.", reason="role_full", role=m.group(1))
    if "wq_event_full" in msg:
        return Conflict("This event is full.", reason="event_full")
    return None


def guarded(s: Session, fn):
    """Run fn in a savepoint, turning database rule violations into readable Conflicts."""
    try:
        with s.begin_nested():
            out = fn()
            s.flush()
            return out
    except (IntegrityError, DBAPIError) as exc:
        c = translate(exc, s)
        if c:
            raise c from None
        raise


def sync_policy(s: Session, max_simultaneous: int) -> None:
    s.execute(text("UPDATE schedule_policy SET max_simultaneous = :n, updated_at = now() WHERE id = 1 "
                   "AND max_simultaneous <> :n"), {"n": max_simultaneous})


def cohost_ids(s: Session, event_id: int) -> set[int]:
    return set(s.scalars(select(EventCohost.member_id).where(EventCohost.event_id == event_id)))


def preview_conflicts(s: Session, start, end, exclusive: bool, ignore_event_id: int | None,
                      member_ids: set[int], limit: int) -> list[str]:
    """Friendly pre-check for forms. The database still makes the final decision."""
    r = rng(start, end)
    probs: list[str] = []
    q = select(EventReservation, Event).join(Event, Event.id == EventReservation.event_id).where(
        EventReservation.during.op("&&")(r))
    if ignore_event_id:
        q = q.where(EventReservation.event_id != ignore_event_id)
    rows = s.execute(q).all()
    for res, ev in rows:
        if res.exclusive or exclusive:
            probs.append(f"Overlaps exclusive event “{ev.title}” ({fmt_local(ev.start_at, ev.timezone)}).")
    if len(rows) + 1 > limit and not probs:
        probs.append(f"{len(rows)} other WoW event(s) overlap; the limit is {limit} at a time.")
    if member_ids:
        mq = select(MemberReservation).where(MemberReservation.member_id.in_(member_ids),
                                             MemberReservation.during.op("&&")(r))
        if ignore_event_id:
            mq = mq.where(MemberReservation.event_id != ignore_event_id)
        for mr in s.scalars(mq):
            probs.append(f"{_member_label(s, mr.member_id)} is already committed to another event then.")
    return probs


def hold(s: Session, ev: Event) -> None:
    """Create/refresh the event's reservation and the organizer/co-host reservations."""
    schedule_lock(s)
    r = rng(ev.start_at, ev.end_at)

    def _do():
        existing = s.get(EventReservation, ev.id)
        if existing is None:
            s.add(EventReservation(event_id=ev.id, during=r, exclusive=ev.exclusive))
        else:
            existing.during, existing.exclusive = r, ev.exclusive
        s.flush()
        # organizer + co-hosts; confirmed participants keep their rows (updated in place)
        staff = {ev.creator_id: "organizer"}
        for cid in cohost_ids(s, ev.id):
            staff.setdefault(cid, "cohost")
        for mid, kind in staff.items():
            mr = s.scalar(select(MemberReservation).where(MemberReservation.member_id == mid,
                                                          MemberReservation.event_id == ev.id))
            if mr is None:
                s.add(MemberReservation(member_id=mid, event_id=ev.id, during=r, kind=kind))
            else:
                mr.during = r
                if mr.kind == "participant" or (mr.kind == "cohost" and kind == "organizer"):
                    mr.kind = kind
            s.flush()
        # staff who are no longer staff: drop unless they are confirmed participants
        s.execute(delete(MemberReservation).where(
            MemberReservation.event_id == ev.id, MemberReservation.kind.in_(("organizer", "cohost")),
            MemberReservation.member_id.not_in(list(staff))))
    guarded(s, _do)


def release(s: Session, ev: Event) -> None:
    schedule_lock(s)
    s.execute(delete(EventReservation).where(EventReservation.event_id == ev.id))
    s.execute(delete(MemberReservation).where(MemberReservation.event_id == ev.id))


def reserve_participant(s: Session, ev: Event, member_id: int) -> None:
    """Called inside the signup-confirm savepoint. Organizer/co-host rows already cover them."""
    exists = s.scalar(select(MemberReservation.id).where(MemberReservation.member_id == member_id,
                                                         MemberReservation.event_id == ev.id))
    if exists:
        return
    s.add(MemberReservation(member_id=member_id, event_id=ev.id, during=rng(ev.start_at, ev.end_at),
                            kind="participant"))
    s.flush()


def unreserve_participant(s: Session, ev: Event, member_id: int) -> None:
    s.execute(delete(MemberReservation).where(MemberReservation.member_id == member_id,
                                              MemberReservation.event_id == ev.id,
                                              MemberReservation.kind == "participant"))


def overlapping_count(s: Session, start, end, ignore_event_id=None) -> int:
    q = select(func.count()).select_from(EventReservation).where(EventReservation.during.op("&&")(rng(start, end)))
    if ignore_event_id:
        q = q.where(EventReservation.event_id != ignore_event_id)
    return s.scalar(q) or 0


def move_participant_reservations(s: Session, ev: Event) -> list[int]:
    """After a reschedule, move each confirmed participant's reservation; return members who conflict.

    Each move runs in its own savepoint so one conflict does not undo the others.
    """
    conflicted = []
    r = rng(ev.start_at, ev.end_at)
    for mr in list(s.scalars(select(MemberReservation).where(MemberReservation.event_id == ev.id,
                                                             MemberReservation.kind == "participant"))):
        try:
            with s.begin_nested():
                s.execute(update(MemberReservation).where(MemberReservation.id == mr.id).values(during=r))
        except (IntegrityError, DBAPIError) as exc:
            if translate(exc, s) is None:
                raise
            conflicted.append(mr.member_id)
    if conflicted:
        s.execute(delete(MemberReservation).where(MemberReservation.event_id == ev.id,
                                                  MemberReservation.member_id.in_(conflicted),
                                                  MemberReservation.kind == "participant"))
    return conflicted
