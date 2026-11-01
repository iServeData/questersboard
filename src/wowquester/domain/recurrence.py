"""Recurring event templates.

A series stores an RFC 5545 RRULE, the IANA zone and the LOCAL wall-clock start, so a
weekly 19:00 Pacific event stays at 19:00 across DST changes. Occurrences are created
as drafts (they reserve nothing) and are then published individually or together.

DST: an occurrence whose local time doesn't exist (spring-forward gap) is skipped and
reported; an ambiguous one (fall-back) uses the first occurrence (fold=0) and is noted.

Edits: "this occurrence" detaches it from the series; "this and future" updates the
series template and every not-yet-started, non-detached occurrence from that one on.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from dateutil.rrule import rrulestr
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Event, Series
from . import audit, events
from .actor import Actor
from .errors import Invalid
from .permissions import require
from .roster import ensure_member
from .timeutil import UTC, now, resolve_local, zone

ALLOWED_FREQ = ("FREQ=WEEKLY", "FREQ=DAILY", "FREQ=MONTHLY")


def occurrences(rrule: str, local_start: datetime, tz: str, limit: int) -> tuple[list[datetime], list[str]]:
    if sum(f in rrule.upper() for f in ALLOWED_FREQ) != 1 or "FREQ=" not in rrule.upper():
        raise Invalid("Repeat must be daily, weekly or monthly.")
    if "COUNT=" not in rrule.upper() and "UNTIL=" not in rrule.upper():
        rrule = f"{rrule};COUNT={limit}"
    try:
        rule = rrulestr(rrule, dtstart=local_start)
    except (ValueError, TypeError) as exc:
        raise Invalid(f"Repeat rule not understood: {exc}") from None
    out, notes = [], []
    for i, local in enumerate(rule):
        if i >= limit:
            break
        try:
            out.append(resolve_local(local, tz))
        except Invalid as exc:
            if exc.detail.get("reason") == "nonexistent":
                notes.append(f"Skipped {local:%a %b %-d %H:%M}: that time doesn't exist in {tz} (DST).")
                continue
            out.append(resolve_local(local, tz, fold=0))
            notes.append(f"{local:%a %b %-d %H:%M} happens twice in {tz}; using the first one.")
    return out, notes


def create_series(s: Session, cfg: Config, actor: Actor, template: dict, rrule: str, local_start: datetime,
                  tz: str, duration_minutes: int) -> tuple[Series, list[Event], list[str]]:
    require(cfg, actor, "event_creator", "Recurring events need the event-creator role.")
    actor = ensure_member(s, actor)
    zone(tz)
    limit = cfg["scheduling"].get("max_recurrence_occurrences", 26)
    starts, notes = occurrences(rrule, local_start, tz, limit)
    if not starts:
        raise Invalid("That repeat rule produces no dates.")
    ser = Series(creator_id=actor.member_id, rrule=rrule, timezone=tz, local_start=local_start.isoformat(),
                 duration_minutes=duration_minutes, template=_jsonable(template))
    s.add(ser)
    s.flush()
    evs = []
    for i, st in enumerate(starts):
        if st < now():
            continue
        data = dict(template, start_at=st, end_at=st + timedelta(minutes=duration_minutes), timezone=tz)
        ev = events.create_draft(s, cfg, actor, data)
        ev.series_id, ev.series_index = ser.id, i
        evs.append(ev)
    audit.record(s, actor, "series.create", "series", ser.id, occurrences=len(evs), notes=notes)
    return ser, evs, notes


def _jsonable(d: dict) -> dict:
    return {k: v for k, v in d.items() if not isinstance(v, datetime)}


def edit_occurrence(s: Session, cfg: Config, actor: Actor, event_id: int, data: dict) -> dict:
    """Change one occurrence only: it leaves the series' future edits."""
    ev = events.get(s, event_id, lock=True)
    ev.series_detached = True
    return events.update(s, cfg, actor, event_id, data)


def edit_future(s: Session, cfg: Config, actor: Actor, event_id: int, data: dict,
                shift_local_time: str | None = None) -> list[tuple[int, str]]:
    """Apply a change to this and all future non-detached, not-started occurrences.

    shift_local_time "HH:MM" moves the local start time of each occurrence (DST-safe).
    Each occurrence is checked independently; failures are reported, not hidden.
    """
    actor = ensure_member(s, actor)
    ev = events.get(s, event_id, lock=True)
    if ev.series_id is None:
        raise Invalid("This event isn't part of a series.")
    ser = s.get(Series, ev.series_id, with_for_update=True)
    ser.template = dict(ser.template, **_jsonable({k: v for k, v in data.items()
                                                    if k not in ("start_at", "end_at", "timezone")}))
    results = []
    targets = list(s.scalars(select(Event).where(Event.series_id == ser.id, Event.series_index >= ev.series_index,
                                                 Event.series_detached.is_(False),
                                                 Event.state.in_(("draft", "scheduled")))
                             .order_by(Event.series_index)))
    for t in targets:
        d = dict(data)
        if shift_local_time:
            hh, mm = (int(x) for x in shift_local_time.split(":"))
            local = t.start_at.astimezone(zone(t.timezone)).replace(hour=hh, minute=mm, tzinfo=None)
            try:
                st = resolve_local(local, t.timezone, fold=0)
            except Invalid as exc:
                results.append((t.id, f"skipped: {exc.message}"))
                continue
            d["start_at"], d["end_at"] = st, st + (t.end_at - t.start_at)
        try:
            with s.begin_nested():
                events.update(s, cfg, actor, t.id, d)
            results.append((t.id, "updated"))
        except Exception as exc:  # report per occurrence
            results.append((t.id, f"not changed: {getattr(exc, 'message', str(exc))}"))
    audit.record(s, actor, "series.edit_future", "series", ser.id, from_index=ev.series_index,
                 results=[r[1] for r in results])
    return results


def publish_series(s: Session, cfg: Config, actor: Actor, series_id: int) -> list[tuple[int, str]]:
    res = []
    for ev in s.scalars(select(Event).where(Event.series_id == series_id, Event.state == "draft")
                        .order_by(Event.series_index)):
        try:
            with s.begin_nested():
                events.publish(s, cfg, actor, ev.id)
            res.append((ev.id, "scheduled"))
        except Exception as exc:
            res.append((ev.id, f"not scheduled: {getattr(exc, 'message', str(exc))}"))
    return res


__all__ = ["occurrences", "create_series", "edit_occurrence", "edit_future", "publish_series", "UTC"]
