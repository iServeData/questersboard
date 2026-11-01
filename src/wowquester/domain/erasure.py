"""Remove me: a member erases their own WoWQuester data. Irreversible.

What happens, in one transaction:
* every active signup is withdrawn (waitlists move up, hosts and Discord cards update);
* events they host that haven't finished are cancelled (participants are told), their drafts
  are discarded and any repeating series they own stops;
* in Chronicle stories about events they were part of, their character names and Discord
  name are replaced, in every saved revision and caption, with the neutral description
  ("a healer"); screenshots of those events go back to review because a name plate might show;
* characters never used in an event are deleted; used ones are blanked to "(removed)" and
  archived so other people's event history still adds up;
* calendar links, the Battle.net link, reminders and every login session are deleted;
* the member row is unlinked from Discord (the Discord ID and display name are wiped), so the
  same Discord account logging in later starts a brand-new, empty profile.

Not touched: the append-only security log (pruned after [retention] audit_days) and encrypted
backups (they age out after [legal] backup_keep_days). The receipt says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import (Attendance, BattlenetLink, CalendarToken, Character, ChronicleCoauthor, ChronicleEntry,
                      ChronicleRevision, ChronicleUpload, Event, EventCohost, Member, MemberReservation,
                      RateBucket, RecognitionRemoval, ReminderDelivery, Series, Signup)
from ..models import Session as WebSession
from . import audit, events, outbox, privacy, projections, signups
from .actor import Actor
from .errors import NotFound
from .timeutil import now

CONFIRM_PHRASE = "remove me"
REMOVED_NAME = "(removed)"
REVISION_FIELDS = ("title", "attempted", "happened", "moments", "body")


def phrase_ok(typed: str) -> bool:
    return " ".join(str(typed or "").strip().strip("\"'“”").lower().split()) == CONFIRM_PHRASE


@dataclass
class Plan:
    leave: list[Event] = field(default_factory=list)        # events they are signed up for
    cancel: list[Event] = field(default_factory=list)       # upcoming events they host
    drafts: int = 0
    characters: int = 0
    stories: int = 0                                         # Chronicle entries they're part of
    calendar_links: int = 0
    battlenet: bool = False


def _member(s: Session, member_id: int, lock: bool = False) -> Member:
    m = s.get(Member, member_id, with_for_update=lock)
    if m is None or m.erased_at is not None:
        raise NotFound("That profile has already been removed.")
    return m


def _involved_event_ids(s: Session, member_id: int) -> set[int]:
    ids = set(s.scalars(select(Signup.event_id).where(Signup.member_id == member_id)))
    ids |= set(s.scalars(select(Attendance.event_id).where(Attendance.member_id == member_id)))
    return ids


def plan(s: Session, member_id: int) -> Plan:
    """What "Remove me" would do, for the confirmation page. Changes nothing."""
    _member(s, member_id)
    p = Plan()
    p.leave = list(s.scalars(select(Event).join(Signup, Signup.event_id == Event.id).where(
        Signup.member_id == member_id, Signup.state.in_(signups.ACTIVE),
        Event.state.in_(("scheduled", "in_progress"))).order_by(Event.start_at)))
    p.cancel = list(s.scalars(select(Event).where(Event.creator_id == member_id,
                                                  Event.state.in_(("scheduled", "in_progress")))
                              .order_by(Event.start_at)))
    p.drafts = len(list(s.scalars(select(Event.id).where(Event.creator_id == member_id, Event.state == "draft"))))
    p.characters = len(list(s.scalars(select(Character.id).where(Character.member_id == member_id,
                                                                 Character.archived_at.is_(None)))))
    inv = _involved_event_ids(s, member_id)
    p.stories = len(list(s.scalars(select(ChronicleEntry.id).where(ChronicleEntry.event_id.in_(inv))))) if inv else 0
    p.calendar_links = len(list(s.scalars(select(CalendarToken.id).where(CalendarToken.member_id == member_id,
                                                                         CalendarToken.revoked_at.is_(None)))))
    p.battlenet = s.scalar(select(BattlenetLink.member_id).where(BattlenetLink.member_id == member_id)
                           .limit(1)) is not None
    return p


def erase(s: Session, cfg: Config, member_id: int, source: str = "web") -> dict:
    """Carry out "Remove me". Returns a receipt of what was done."""
    m = _member(s, member_id, lock=True)
    old_discord_id = m.discord_id
    pseudonym = f"erased:{member_id}"
    # Internal actor: acts as the member for their own rows, never records their Discord ID.
    act = Actor(discord_id=pseudonym, source="worker", member_id=member_id)
    receipt: dict = {"left": [], "cancelled": [], "drafts": 0, "series": 0, "characters_deleted": 0,
                     "characters_blanked": 0, "stories": 0, "screenshots_rechecked": 0, "calendar_links": 0,
                     "battlenet": False, "sessions": 0}

    # 1. withdraw from everything still open
    for su in list(s.scalars(select(Signup).where(Signup.member_id == member_id, Signup.state.in_(signups.ACTIVE))
                             .order_by(Signup.id))):
        ev = s.get(Event, su.event_id)
        if ev.state in ("scheduled", "in_progress"):
            signups.withdraw(s, cfg, act, su.id, reason="profile removed")
            receipt["left"].append(ev.title)
        else:
            su.state, su.updated_at = "withdrawn", now()

    # 2. events they host
    for ev in list(s.scalars(select(Event).where(Event.creator_id == member_id).order_by(Event.start_at))):
        if ev.state in ("scheduled", "in_progress"):
            events.cancel(s, cfg, act, ev.id, reason="The host has left WoWQuester.")
            receipt["cancelled"].append(ev.title)
        elif ev.state == "draft":
            referenced = s.scalar(select(Event.id).where(Event.copied_from_id == ev.id).limit(1))
            if referenced:
                # another event was copied from this draft: keep the row, it's not shown anywhere
                ev.title, ev.description, ev.eligibility = "(removed draft)", "", ""
            else:
                s.delete(ev)
            receipt["drafts"] += 1
    s.flush()
    for sr in s.scalars(select(Series).where(Series.creator_id == member_id, Series.ended_at.is_(None))):
        sr.ended_at = now()
        receipt["series"] += 1
    s.execute(delete(EventCohost).where(EventCohost.member_id == member_id))
    s.execute(delete(MemberReservation).where(MemberReservation.member_id == member_id))

    # 3. Chronicle: rewrite their names out of every revision, before the names are blanked
    m.erased_at = now()             # from here on, privacy.* treats them as not consenting
    s.flush()
    involved = _involved_event_ids(s, member_id)
    for su in s.scalars(select(Signup).where(Signup.member_id == member_id)):
        su.recognition, su.note = False, ""
    for eid in sorted(involved):
        if not s.scalar(select(RecognitionRemoval.id).where(RecognitionRemoval.event_id == eid,
                                                            RecognitionRemoval.member_id == member_id)):
            s.add(RecognitionRemoval(member_id=member_id, event_id=eid, applied_at=now()))
    s.flush()
    for eid in sorted(involved):
        ev = s.get(Event, eid)
        entry = s.scalar(select(ChronicleEntry).where(ChronicleEntry.event_id == eid))
        if entry is not None:
            roles = {r.key: r.label for r in ev.roles}
            mine = [p for p in privacy.involved(s, cfg, eid, roles) if p.member_id == member_id]
            for rev in s.scalars(select(ChronicleRevision).where(ChronicleRevision.entry_id == entry.id)):
                for f in REVISION_FIELDS:
                    val = getattr(rev, f) or ""
                    new = privacy.redact(val, mine, cfg)
                    if new != val:
                        setattr(rev, f, new[:140] if f == "title" else new)
            for u in s.scalars(select(ChronicleUpload).where(ChronicleUpload.entry_id == entry.id)):
                u.caption = privacy.redact(u.caption or "", mine, cfg)[:200]
                if u.cleared_at is not None and u.removed_at is None:
                    u.cleared_at, u.cleared_by = None, None
                    receipt["screenshots_rechecked"] += 1
            projections.refresh_chronicle(s, cfg, entry)
            if entry.published_revision_id:
                outbox.enqueue(s, "notify.chronicle_review", f"{entry.id}:erasure:{member_id}",
                               {"entry_id": entry.id, "privacy": True})
            receipt["stories"] += 1
        if ev.state != "draft":
            outbox.enqueue(s, "discord.render_event", str(ev.id), {"event_id": ev.id})   # card drops the name
    s.execute(delete(ChronicleCoauthor).where(ChronicleCoauthor.member_id == member_id))

    # 4. characters
    for ch in list(s.scalars(select(Character).where(Character.member_id == member_id))):
        used = s.scalar(select(Signup.id).where(Signup.character_id == ch.id).limit(1)) or \
            s.scalar(select(Attendance.event_id).where(Attendance.character_id == ch.id).limit(1))
        if used:
            ch.name, ch.notes, ch.level, ch.spec, ch.focus_tags = REMOVED_NAME, "", None, None, []
            ch.realm_slug = ch.realm_name = ch.api_external_id = ch.api_namespace = ch.api_refreshed_at = None
            ch.recognition_default, ch.verification = None, "self_reported"
            ch.archived_at = ch.archived_at or now()
            ch.updated_at = now()
            receipt["characters_blanked"] += 1
        else:
            s.delete(ch)
            receipt["characters_deleted"] += 1

    # 5. links, reminders, sessions, rate counters
    receipt["calendar_links"] = s.execute(delete(CalendarToken).where(CalendarToken.member_id == member_id)).rowcount
    receipt["battlenet"] = bool(s.execute(delete(BattlenetLink).where(BattlenetLink.member_id == member_id)).rowcount)
    s.execute(delete(ReminderDelivery).where(ReminderDelivery.member_id == member_id))
    receipt["sessions"] = s.execute(delete(WebSession).where(WebSession.member_id == member_id)).rowcount
    s.execute(delete(RateBucket).where(or_(RateBucket.key.like(f"%:{old_discord_id}"),
                                           RateBucket.key.like(f"%:{old_discord_id}:%"))))

    # 6. unlink from Discord
    m.discord_id, m.display_name, m.timezone, m.recognition_default = pseudonym, "", None, False
    s.flush()
    audit.record(s, act, "member.erase", "member", member_id, via=source,
                 **{k: (len(v) if isinstance(v, list) else v) for k, v in receipt.items()})
    return receipt


def find_member_id(s: Session, discord_id: str) -> int:
    mid = s.scalar(select(Member.id).where(Member.discord_id == str(discord_id), Member.erased_at.is_(None)))
    if mid is None:
        raise NotFound("No WoWQuester profile for that Discord ID.")
    return mid

