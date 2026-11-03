"""Event lifecycle: Draft -> Scheduled -> In progress -> Completed, with Cancelled from Scheduled/In progress."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Attendance, Event, EventCohost, EventRole, Member, Signup
from . import audit, outbox, projections, scheduling
from .actor import Actor
from .errors import Invalid, InvalidTransition, NotFound, PermissionDenied
from .permissions import has, require, require_event_manager, require_member
from .roster import ensure_member
from .timeutil import now, zone

ACTIVITY_TYPES = ("raid", "dungeon", "quest", "rp", "arena", "battleground", "world_pvp", "lfg", "social", "custom")
TRANSITIONS = {
    "draft": {"scheduled"},
    "scheduled": {"in_progress", "cancelled"},
    "in_progress": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ACTIVE_SIGNUP = ("pending", "confirmed", "tentative", "waitlisted")


def get(s: Session, event_id: int, lock: bool = False) -> Event:
    stmt = select(Event).where(Event.id == event_id)
    if lock:
        stmt = stmt.with_for_update()
    ev = s.scalar(stmt)
    if ev is None:
        raise NotFound("Event not found.")
    return ev


def _location(d) -> dict:
    if not d:
        return {}
    if isinstance(d, str):
        d = {"label": d}
    out = {"label": str(d.get("label", "")).strip()[:200]}
    for k in ("zone", "coordinates", "ref_key", "source"):
        if d.get(k):
            out[k] = str(d[k])[:120]
    link = str(d.get("link") or "").strip()
    if link:
        if not link.startswith("https://"):
            raise Invalid("Location links must start with https://")
        out["link"] = link[:300]
    return out if out["label"] else {}


def _roles(cfg: Config, roles) -> list[dict]:
    """Creator picks role names and quantities. Presets or custom. Empty list = no composition."""
    out, seen = [], set()
    presets = {p.key: p.label for p in cfg.role_presets}
    for r in roles or []:
        key = str(r.get("key") or "").strip().lower().replace(" ", "_")
        label = str(r.get("label") or presets.get(key) or key.replace("_", " ").title()).strip()[:40]
        try:
            qty = int(r.get("quantity"))
        except (TypeError, ValueError):
            raise Invalid(f"Quantity for {label or key} must be a number") from None
        if not KEY.match(key) or not label:
            raise Invalid("Role names may use letters, numbers and underscores.")
        if not 1 <= qty <= 80:
            raise Invalid(f"Quantity for {label} must be 1–80")
        if key in seen:
            raise Invalid(f"Role {label} is listed twice")
        seen.add(key)
        out.append({"key": key, "label": label, "quantity": qty})
    custom = [r for r in out if r["key"] not in presets]
    if len(custom) > cfg["signups"].get("max_custom_roles", 10):
        raise Invalid("Too many custom roles")
    return out


def _validate_times(cfg: Config, start: datetime, end: datetime, tz: str, allow_past: bool = False) -> None:
    zone(tz)
    if start.tzinfo is None or end.tzinfo is None:
        raise Invalid("Times must be resolved to UTC instants")
    if end <= start:
        raise Invalid("The expected end must be after the start.")
    sch = cfg["scheduling"]
    dur = (end - start).total_seconds() / 60
    if dur < sch.get("min_duration_minutes", 15):
        raise Invalid(f"Events must last at least {sch.get('min_duration_minutes', 15)} minutes.")
    if dur > sch.get("max_duration_hours", 12) * 60:
        raise Invalid(f"Events can last at most {sch.get('max_duration_hours', 12)} hours.")
    if not allow_past and start < now() - timedelta(minutes=5):
        raise Invalid("The start time is in the past.")
    if start > now() + timedelta(days=sch.get("max_days_ahead", 180)):
        raise Invalid("That is too far ahead.")


def _apply_fields(s: Session, cfg: Config, actor: Actor, ev: Event, data: dict, creating: bool) -> None:
    if "edition" in data or creating:
        ed = cfg.edition(data.get("edition") or ev.edition or next(iter(cfg.editions)))
        ev.edition = ed.key
        ev.region = data.get("region") or ev.region or ed.default_region
        if ev.region not in ed.regions:
            raise Invalid(f"{ed.name} supports regions: {', '.join(ed.regions)}")
        if ed.uses_realms:
            ev.ruleset = None
            ev.realm_slug = (data.get("realm_slug") or ev.realm_slug or None)
        else:
            ev.realm_slug = None
            ev.ruleset = data.get("ruleset") or ev.ruleset or ed.default_ruleset
            if ev.ruleset not in ed.rulesets:
                raise Invalid(f"Ruleset must be one of {', '.join(ed.rulesets)}")
        if data.get("faction") or creating:
            f = data.get("faction") or ev.faction or ed.default_faction
            if f and f not in ed.factions + ("any",):
                raise Invalid("Unknown faction")
            ev.faction = None if f == "any" else f
    if "title" in data or creating:
        t = str(data.get("title") or "").strip()
        if not 3 <= len(t) <= 120:
            raise Invalid("Title must be 3–120 characters.")
        ev.title = t
    if "activity_type" in data or creating:
        at = data.get("activity_type") or "lfg"
        if at not in ACTIVITY_TYPES:
            raise Invalid(f"Activity type must be one of {', '.join(ACTIVITY_TYPES)}")
        ev.activity_type = at
    for k, n in (("description", 2000), ("eligibility", 500)):
        if k in data:
            setattr(ev, k, str(data[k] or "")[:n])
    if "activity_location" in data:
        ev.activity_location = _location(data["activity_location"])
    if "muster_location" in data:
        ev.muster_location = _location(data["muster_location"])
    if "signup_policy" in data or creating:
        p = data.get("signup_policy") or cfg["signups"]["default_policy"]
        if p not in ("open", "approval"):
            raise Invalid("Signup policy must be open or approval")
        ev.signup_policy = p
    if "participant_limit" in data:
        v = data["participant_limit"]
        ev.participant_limit = int(v) if v not in (None, "", 0, "0") else None
        if ev.participant_limit is not None and not 1 <= ev.participant_limit <= 80:
            raise Invalid("Participant limit must be 1–80")
    if "signup_deadline" in data:
        ev.signup_deadline = data["signup_deadline"] or None
    for k in ("late_join", "chronicle_enabled", "public_listing"):
        if k in data:
            setattr(ev, k, bool(data[k]))
    if creating and "late_join" not in data:
        ev.late_join = bool(cfg["scheduling"].get("late_join_default", False))
    if "exclusive" in data:
        want = bool(data["exclusive"])
        if want != bool(ev.exclusive):
            if not has(cfg, actor, "exclusive_designator"):
                raise PermissionDenied("Only the exclusive-event role can mark an event exclusive.")
            ev.exclusive = want


def party_max(cfg: Config, activity_type: str | None) -> int:
    return int((cfg["signups"].get("party_size_max") or {}).get(activity_type or "", 80))


def _check_sizes(cfg: Config, ev: Event) -> None:
    """Participant limit <= the activity's group size; role spots add up to no more than the limit."""
    cap = party_max(cfg, ev.activity_type)
    kind = (ev.activity_type or "event").replace("_", " ")
    if ev.participant_limit is not None and ev.participant_limit > cap:
        raise Invalid(f"A {kind} holds at most {cap} players. Lower the participant limit to {cap} or less.")
    auto = ev.participant_limit is None
    if auto and cap < 80:
        ev.participant_limit = cap             # a quest with no limit still holds one group
    limit = ev.participant_limit or cap
    total = sum(r.quantity for r in ev.roles)
    if total > limit:
        what = f"{kind} group size" if auto else "participant limit"
        raise Invalid(f"The roles add up to {total} spots but the {what} is {limit}. "
                      "Lower the How many numbers or raise the limit.")


def _set_roles(s: Session, ev: Event, roles: list[dict]) -> list[int]:
    """Returns the signups that now need to pick a role (their role was added/renamed away)."""
    current = {r.key: r for r in ev.roles}
    wanted = {r["key"]: r for r in roles}
    for key, r in current.items():
        if key not in wanted:
            taken = s.scalar(select(Signup.id).where(Signup.event_id == ev.id, Signup.role_key == key,
                                                     Signup.state.in_(ACTIVE_SIGNUP)).limit(1))
            if taken:
                raise Invalid(f"Move or remove the people signed up as {r.label} before removing that role.")
            ev.roles.remove(r)
    for pos, (key, r) in enumerate(wanted.items()):
        if key in current:
            row = current[key]
            confirmed = s.scalar(select(__import__("sqlalchemy").func.count()).select_from(Signup).where(
                Signup.event_id == ev.id, Signup.role_key == key, Signup.state == "confirmed")) or 0
            if r["quantity"] < confirmed:
                raise Invalid(f"{confirmed} people are confirmed as {row.label}; the quantity can't go below that.")
            row.label, row.quantity, row.position = r["label"], r["quantity"], pos
        else:
            ev.roles.append(EventRole(key=key, label=r["label"], quantity=r["quantity"], position=pos))
    s.flush()
    keys = set(wanted)
    ask = []
    if keys:          # people who joined before roles existed get asked to pick one
        for su in s.scalars(select(Signup).where(Signup.event_id == ev.id, Signup.state.in_(ACTIVE_SIGNUP))):
            if su.role_key not in keys:
                if not su.needs_reconfirm:
                    ask.append(su.id)
                su.needs_reconfirm = True
                su.updated_at = now()
    return ask


def _set_cohosts(s: Session, ev: Event, discord_ids: list[str]) -> set[int]:
    ids = set()
    for did in discord_ids or []:
        did = str(did).strip()
        if not re.match(r"^\d{15,22}$", did):
            raise Invalid("Co-hosts are chosen by Discord account.")
        m = s.scalar(select(Member).where(Member.discord_id == did))
        if m is None:
            m = Member(discord_id=did)
            s.add(m)
            s.flush()
        if m.id != ev.creator_id:
            ids.add(m.id)
    if len(ids) > 5:
        raise Invalid("At most 5 co-hosts")
    s.execute(delete(EventCohost).where(EventCohost.event_id == ev.id))
    for mid in ids:
        s.add(EventCohost(event_id=ev.id, member_id=mid))
    s.flush()
    return ids


def _changed(ev: Event) -> None:
    ev.version += 1
    ev.updated_at = now()


def _notify_post(s: Session, cfg: Config, ev: Event) -> None:
    if ev.state != "draft":
        outbox.enqueue(s, "discord.render_event", str(ev.id), {"event_id": ev.id})
    projections.refresh_event(s, cfg, ev)


# --------------------------------------------------------------------------- create / edit

def create_draft(s: Session, cfg: Config, actor: Actor, data: dict) -> Event:
    """Any member may hold drafts (Copy creates drafts); publishing needs event_creator."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    start, end, tz = data.get("start_at"), data.get("end_at"), data.get("timezone") or cfg["community"]["default_timezone"]
    if start is None or end is None:
        raise Invalid("Start and expected end are required.")
    _validate_times(cfg, start, end, tz)
    ev = Event(creator_id=actor.member_id, start_at=start, end_at=end, timezone=tz, state="draft",
               activity_location={}, muster_location={})
    _apply_fields(s, cfg, actor, ev, data, creating=True)
    s.add(ev)
    s.flush()
    _set_roles(s, ev, _roles(cfg, data.get("roles")))
    _check_sizes(cfg, ev)
    if data.get("cohosts"):
        _set_cohosts(s, ev, data["cohosts"])
    audit.record(s, actor, "event.create_draft", "event", ev.id, copied_from=data.get("copied_from_id"))
    return ev


def update(s: Session, cfg: Config, actor: Actor, event_id: int, data: dict) -> dict:
    """Edit fields. Time changes are a reschedule (checked, and participants asked to reconfirm)."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = get(s, event_id, lock=True)
    cohosts = scheduling.cohost_ids(s, ev.id)
    require_event_manager(cfg, actor, ev.creator_id, cohosts)
    if ev.state in ("completed", "cancelled"):
        raise InvalidTransition("Finished events can't be edited.")
    result = {"reconfirm": [], "bumped": []}
    material = []
    before = _snapshot(s, ev)
    old_loc = (ev.activity_location, ev.muster_location)
    _apply_fields(s, cfg, actor, ev, {k: v for k, v in data.items() if k not in ("start_at", "end_at", "timezone")},
                  creating=False)
    pick_role = []
    if "roles" in data:
        pick_role = _set_roles(s, ev, _roles(cfg, data["roles"]))
    _check_sizes(cfg, ev)
    if "cohosts" in data:
        if not (actor.member_id == ev.creator_id or has(cfg, actor, "event_moderator")):
            raise PermissionDenied("Only the creator or an event moderator can change co-hosts.")
        _set_cohosts(s, ev, data["cohosts"])
    if (ev.activity_location, ev.muster_location) != old_loc:
        material.append("location")
    new_start, new_end = data.get("start_at", ev.start_at), data.get("end_at", ev.end_at)
    new_tz = data.get("timezone", ev.timezone)
    time_changed = (new_start, new_end) != (ev.start_at, ev.end_at)
    if time_changed:
        if ev.state == "in_progress":
            # only extending/shortening the end is allowed while running (overrun handling)
            if new_start != ev.start_at:
                raise InvalidTransition("A running event's start can't move; change the end instead.")
            _validate_times(cfg, ev.start_at, new_end, new_tz, allow_past=True)
        else:
            _validate_times(cfg, new_start, new_end, new_tz)
        ev.start_at, ev.end_at, ev.timezone = new_start, new_end, new_tz
        material.append("time")
    elif new_tz != ev.timezone:
        zone(new_tz)
        ev.timezone = new_tz
    if ev.state in scheduling.HOLDING:
        scheduling.hold(s, ev)        # raises Conflict for capacity/exclusive/organizer conflicts
        if time_changed:
            bumped = scheduling.move_participant_reservations(s, ev)
            for su in s.scalars(select(Signup).where(Signup.event_id == ev.id, Signup.state == "confirmed")):
                if su.member_id in bumped:
                    su.state, su.needs_reconfirm = "tentative", True
                    result["bumped"].append(su.member_id)
                elif cfg["scheduling"].get("reschedule_requires_reconfirm", True):
                    su.needs_reconfirm = True
                    result["reconfirm"].append(su.member_id)
                su.updated_at = now()
            for su in s.scalars(select(Signup).where(Signup.event_id == ev.id,
                                                     Signup.state.in_(("pending", "tentative", "waitlisted")))):
                su.needs_reconfirm = su.needs_reconfirm or su.member_id not in result["bumped"]
    _changed(ev)
    if material and ev.state in scheduling.HOLDING:
        outbox.enqueue(s, "notify.material_change", f"{ev.id}:{ev.version}",
                       {"event_id": ev.id, "changes": material, "bumped": result["bumped"]})
    changed = _diff(before, _snapshot(s, ev))
    if pick_role and ev.state in scheduling.HOLDING:
        outbox.enqueue(s, "notify.pick_role", f"{ev.id}:{ev.version}", {"event_id": ev.id, "signup_ids": pick_role})
    if changed and (actor.member_id != ev.creator_id or cohosts):
        # tell the other hosts what changed (the person who made the edit is skipped)
        outbox.enqueue(s, "notify.event_edited", f"{ev.id}:{ev.version}",
                       {"event_id": ev.id, "actor_id": actor.member_id, "changes": changed})
    audit.record(s, actor, "event.update", "event", ev.id, fields=changed, material=material)
    _notify_post(s, cfg, ev)
    return result


_LABELS = {"title": "title", "description": "description", "edition": "edition", "ruleset": "ruleset",
           "realm_slug": "realm", "region": "region", "activity_type": "type", "activity_location": "where",
           "muster_location": "meeting point", "signup_policy": "signup policy",
           "participant_limit": "participant limit", "signup_deadline": "signup deadline",
           "late_join": "late joining", "chronicle_enabled": "chronicle", "public_listing": "public listing",
           "exclusive": "exclusive", "time": "time", "roles": "roles", "cohosts": "co-hosts",
           "eligibility": "who can join", "cover": "cover image"}


def _snapshot(s: Session, ev: Event) -> dict:
    out = {}
    for col in Event.__table__.columns:
        if col.key in ("id", "version", "updated_at", "created_at", "state", "discord_channel_id",
                       "discord_message_id", "actual_start", "actual_end", "public_id", "creator_id"):
            continue
        out[col.key] = getattr(ev, col.key)
    out["time"] = (ev.start_at, ev.end_at, ev.timezone)
    for k in ("start_at", "end_at", "timezone"):
        out.pop(k, None)
    out["roles"] = [(r.key, r.label, r.quantity) for r in sorted(ev.roles, key=lambda r: r.position)]
    out["cohosts"] = sorted(scheduling.cohost_ids(s, ev.id))
    return out


def _diff(a: dict, b: dict) -> list[str]:
    seen, out = set(), []
    for k in b:
        if a.get(k) != b.get(k):
            lbl = _LABELS.get(k)
            if lbl is None:
                lbl = k.replace("_id", "").replace("_", " ")
                for pre in ("eligibility", "cover"):
                    if k.startswith(pre):
                        lbl = _LABELS[pre]
            if lbl not in seen:
                seen.add(lbl)
                out.append(lbl)
    return out


def publish(s: Session, cfg: Config, actor: Actor, event_id: int) -> Event:
    """Draft -> Scheduled. Needs event_creator; checks capacity, exclusivity and the organizer's own time."""
    require(cfg, actor, "event_creator", "Publishing events needs the event-creator role.")
    actor = ensure_member(s, actor)
    ev = get(s, event_id, lock=True)
    if ev.creator_id != actor.member_id and not has(cfg, actor, "event_moderator"):
        raise PermissionDenied("Only the draft's owner can publish it.")
    _transition(ev, "scheduled")
    _validate_times(cfg, ev.start_at, ev.end_at, ev.timezone)
    if not cfg["discord"].get("event_channel_id") and actor.source != "cli":
        pass  # posting is skipped until a channel is configured; the event still schedules
    ev.discord_channel_id = ev.discord_channel_id or (cfg["discord"].get("event_channel_id") or None)
    scheduling.hold(s, ev)
    _changed(ev)
    audit.record(s, actor, "event.publish", "event", ev.id)
    _notify_post(s, cfg, ev)
    return ev


def discard_draft(s: Session, cfg: Config, actor: Actor, event_id: int) -> None:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = get(s, event_id, lock=True)
    if ev.state != "draft":
        raise InvalidTransition("Only drafts can be discarded. Cancel a scheduled event instead.")
    if ev.creator_id != actor.member_id and not has(cfg, actor, "event_moderator"):
        raise PermissionDenied("Only the draft's owner can discard it.")
    s.delete(ev)
    audit.record(s, actor, "event.discard", "event", event_id)


def _transition(ev: Event, to: str) -> None:
    if to not in TRANSITIONS[ev.state]:
        raise InvalidTransition(f"An event that is {ev.state.replace('_', ' ')} can't become {to.replace('_', ' ')}.")
    ev.state = to


def start(s: Session, cfg: Config, actor: Actor, event_id: int) -> Event:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = get(s, event_id, lock=True)
    require_event_manager(cfg, actor, ev.creator_id, scheduling.cohost_ids(s, ev.id))
    if now() < ev.start_at - timedelta(hours=2):
        raise InvalidTransition("Events can be started up to 2 hours early.")
    _transition(ev, "in_progress")
    ev.actual_start = now()
    _changed(ev)
    audit.record(s, actor, "event.start", "event", ev.id)
    _notify_post(s, cfg, ev)
    return ev


def end(s: Session, cfg: Config, actor: Actor, event_id: int) -> Event:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = get(s, event_id, lock=True)
    require_event_manager(cfg, actor, ev.creator_id, scheduling.cohost_ids(s, ev.id))
    _transition(ev, "completed")
    ev.actual_end = now()
    scheduling.release(s, ev)
    # attendance starts as "unknown" for confirmed people: signing up is not proof of playing
    for su in s.scalars(select(Signup).where(Signup.event_id == ev.id, Signup.state == "confirmed")):
        if s.get(Attendance, (ev.id, su.member_id)) is None:
            s.add(Attendance(event_id=ev.id, member_id=su.member_id, character_id=su.character_id,
                             role_key=su.role_key, status="unknown"))
    _changed(ev)
    if ev.chronicle_enabled:
        from . import chronicle
        chronicle.ensure_draft(s, cfg, ev)
        outbox.enqueue(s, "notify.chronicle_prompt", str(ev.id), {"event_id": ev.id})
    audit.record(s, actor, "event.end", "event", ev.id)
    _notify_post(s, cfg, ev)
    return ev


def cancel(s: Session, cfg: Config, actor: Actor, event_id: int, reason: str = "") -> Event:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = get(s, event_id, lock=True)
    require_event_manager(cfg, actor, ev.creator_id, scheduling.cohost_ids(s, ev.id))
    _transition(ev, "cancelled")
    ev.cancelled_reason = str(reason or "")[:500]
    scheduling.release(s, ev)
    _changed(ev)
    outbox.enqueue(s, "notify.material_change", f"{ev.id}:{ev.version}",
                   {"event_id": ev.id, "changes": ["cancelled"], "bumped": []})
    audit.record(s, actor, "event.cancel", "event", ev.id, reason=ev.cancelled_reason)
    _notify_post(s, cfg, ev)
    return ev


def copy(s: Session, cfg: Config, actor: Actor, event_id: int, start_at: datetime, end_at: datetime,
         tz: str | None = None) -> Event:
    """New draft from an event's template. Never copies people, consent, attendance, outcomes or actual times."""
    require_member(cfg, actor)
    src = get(s, event_id)
    if src.state == "draft" and src.creator_id != ensure_member(s, actor).member_id:
        raise NotFound("Event not found.")
    data = dict(edition=src.edition, region=src.region, ruleset=src.ruleset, realm_slug=src.realm_slug,
                faction=src.faction or "any", title=src.title, activity_type=src.activity_type,
                description=src.description, eligibility=src.eligibility,
                activity_location=src.activity_location, muster_location=src.muster_location,
                signup_policy=src.signup_policy, participant_limit=src.participant_limit,
                late_join=src.late_join, chronicle_enabled=src.chronicle_enabled,
                public_listing=src.public_listing,
                roles=[{"key": r.key, "label": r.label, "quantity": r.quantity} for r in src.roles],
                start_at=start_at, end_at=end_at, timezone=tz or src.timezone, copied_from_id=src.id)
    ev = create_draft(s, cfg, actor, data)
    ev.copied_from_id = src.id
    audit.record(s, actor, "event.copy", "event", ev.id, source_event=src.id)
    return ev


def record_attendance(s: Session, cfg: Config, actor: Actor, event_id: int, statuses: dict[int, str]) -> None:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = get(s, event_id)
    require_event_manager(cfg, actor, ev.creator_id, scheduling.cohost_ids(s, ev.id))
    if ev.state not in ("in_progress", "completed"):
        raise InvalidTransition("Attendance is recorded once the event has started.")
    for member_id, st in statuses.items():
        if st not in ("attended", "absent", "unknown"):
            raise Invalid("Attendance must be attended, absent or unknown")
        row = s.get(Attendance, (ev.id, int(member_id)))
        if row is None:
            su = s.scalar(select(Signup).where(Signup.event_id == ev.id, Signup.member_id == int(member_id))
                          .order_by(Signup.id.desc()))
            if su is None:
                raise Invalid("Only people who signed up can be marked.")
            row = Attendance(event_id=ev.id, member_id=su.member_id, character_id=su.character_id,
                             role_key=su.role_key)
            s.add(row)
        if row.status != st or row.recorded_by is None:     # unchanged rows keep who/when recorded them
            row.status, row.recorded_by, row.recorded_at = st, actor.member_id, now()
    audit.record(s, actor, "event.attendance", "event", ev.id, count=len(statuses))
    from . import chronicle
    chronicle.refresh_if_published(s, cfg, ev.id)
