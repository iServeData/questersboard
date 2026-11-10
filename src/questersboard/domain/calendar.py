"""iCalendar feeds.

* Public feed: scheduled/in-progress events from the public_events projection only.
* Personal feed: events the member organizes, co-hosts or is confirmed/tentative/waitlisted
  for. Addressed by an unguessable token; only its SHA-256 is stored, so a database
  leak doesn't reveal working URLs. Members can rotate (revoke + reissue) at any time.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import CalendarToken, Event, EventCohost, PublicEvent, Signup
from . import audit
from .actor import Actor
from .permissions import require_member
from .roster import ensure_member
from .timeutil import UTC, now


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(s: Session, cfg: Config, actor: Actor) -> str:
    """Revokes any previous token and returns the new plain token (shown once)."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    for t in s.scalars(select(CalendarToken).where(CalendarToken.member_id == actor.member_id,
                                                   CalendarToken.revoked_at.is_(None))):
        t.revoked_at = now()
    tok = secrets.token_urlsafe(32)
    s.add(CalendarToken(member_id=actor.member_id, token_hash=_hash(tok)))
    audit.record(s, actor, "calendar.issue_token", "member", actor.member_id)
    return tok


def revoke_tokens(s: Session, cfg: Config, actor: Actor) -> int:
    actor = ensure_member(s, actor)
    n = 0
    for t in s.scalars(select(CalendarToken).where(CalendarToken.member_id == actor.member_id,
                                                   CalendarToken.revoked_at.is_(None))):
        t.revoked_at, n = now(), n + 1
    audit.record(s, actor, "calendar.revoke", "member", actor.member_id)
    return n


def member_for_token(s: Session, token: str) -> int | None:
    if not token or len(token) > 100:
        return None
    t = s.scalar(select(CalendarToken).where(CalendarToken.token_hash == _hash(token),
                                             CalendarToken.revoked_at.is_(None)))
    return t.member_id if t else None


def _esc(t: str) -> str:
    return (t or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "") \
        .replace("\n", "\\n")


def _fold(line: str) -> str:
    b = line.encode()
    if len(b) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        e = ch.encode()
        if len(cur) + len(e) > (75 if not out else 74):
            out.append(cur.decode())
            cur = b""
        cur += e
    out.append(cur.decode())
    return "\r\n ".join(out)


def _dt(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def ics(cfg: Config, name: str, items: list[dict]) -> str:
    host = cfg["community"]["public_base_url"].split("//", 1)[-1].split("/")[0]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//questersboard//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_esc(name)}", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]
    stamp = _dt(now())
    for it in items:
        lines += ["BEGIN:VEVENT", f"UID:{it['uid']}@{host}", f"DTSTAMP:{stamp}",
                  f"DTSTART:{_dt(it['start'])}", f"DTEND:{_dt(it['end'])}", f"SUMMARY:{_esc(it['title'])}",
                  f"DESCRIPTION:{_esc(it.get('description', ''))}",
                  f"STATUS:{'CANCELLED' if it.get('cancelled') else 'CONFIRMED'}",
                  f"SEQUENCE:{it.get('sequence', 0)}"]
        if it.get("location"):
            lines.append(f"LOCATION:{_esc(it['location'])}")
        if it.get("url"):
            lines.append(f"URL:{it['url']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


def public_items(s: Session, cfg: Config) -> list[dict]:
    horizon = now() + timedelta(days=cfg["calendar"].get("horizon_days", 90))
    out = []
    for pe in s.scalars(select(PublicEvent).where(PublicEvent.end_at > now() - timedelta(days=1),
                                                  PublicEvent.start_at < horizon).order_by(PublicEvent.start_at)):
        out.append(dict(uid=f"wq-{pe.public_id}", start=pe.start_at, end=pe.end_at, title=pe.title,
                        location=pe.location_label, description=f"{pe.edition_name} · {pe.activity_type}"))
    return out


def personal_items(s: Session, cfg: Config, member_id: int) -> list[dict]:
    horizon = now() + timedelta(days=cfg["calendar"].get("horizon_days", 90))
    sub_signup = select(Signup.event_id).where(Signup.member_id == member_id,
                                               Signup.state.in_(("confirmed", "tentative", "waitlisted", "pending")))
    sub_cohost = select(EventCohost.event_id).where(EventCohost.member_id == member_id)
    q = select(Event).where(Event.state.in_(("scheduled", "in_progress", "cancelled")),
                            Event.start_at < horizon, Event.end_at > now() - timedelta(days=7),
                            or_(Event.creator_id == member_id, Event.id.in_(sub_signup), Event.id.in_(sub_cohost)))
    base = cfg["community"]["public_base_url"].rstrip("/") + cfg["web"]["member_path"].rstrip("/")
    out = []
    for ev in s.scalars(q.order_by(Event.start_at)):
        su = s.scalar(select(Signup.state).where(Signup.event_id == ev.id, Signup.member_id == member_id)
                      .order_by(Signup.id.desc()).limit(1))
        status = "organizer" if ev.creator_id == member_id else (su or "co-host")
        out.append(dict(uid=f"wq-{ev.public_id}", start=ev.start_at, end=ev.end_at, title=ev.title,
                        location=(ev.activity_location or {}).get("label", ""),
                        description=f"Your status: {status}", cancelled=ev.state == "cancelled",
                        sequence=ev.version, url=f"{base}/events/{ev.public_id}"))
    return out
