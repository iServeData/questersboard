"""Signups: pending, confirmed, tentative, waitlisted, withdrawn, rejected.

Only `confirmed` holds a member reservation. Every move into `confirmed` (join on an
open event, approval, waitlist promotion, reconfirmation) runs in one savepoint that
(1) sets the state, which fires the DB capacity trigger, and (2) inserts the member
reservation, which hits the DB exclusion constraint. Either failure rolls both back.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Character, Event, Member, Signup
from . import audit, outbox, projections, scheduling
from .actor import Actor
from .errors import Conflict, Invalid, InvalidTransition, NotFound, PermissionDenied
from .events import get as get_event
from .permissions import require_event_manager, require_member
from .roster import ensure_member
from .timeutil import now

ACTIVE = ("pending", "confirmed", "tentative", "waitlisted")


def _open_for_signup(ev: Event) -> None:
    if ev.state == "scheduled":
        if ev.signup_deadline and now() > ev.signup_deadline:
            raise InvalidTransition("Signups for this event have closed.")
        return
    if ev.state == "in_progress" and ev.late_join:
        return
    if ev.state == "in_progress":
        raise InvalidTransition("This event has started and doesn't take late joiners.")
    raise InvalidTransition("This event isn't taking signups.")


def _check_character(cfg: Config, ev: Event, ch: Character, member_id: int) -> None:
    if ch.member_id != member_id or ch.archived_at is not None:
        raise PermissionDenied("You can only sign up with one of your own active characters.")
    ed = cfg.edition(ev.edition)
    if ch.edition != ev.edition or ch.region != ev.region:
        raise Invalid(f"This event is for {ed.name} ({ev.region.upper()}); pick a character from there.")
    if ed.uses_realms:
        if ev.realm_slug and ch.realm_slug != ev.realm_slug:
            raise Invalid("This event is on a different realm.")
    elif ev.ruleset and ch.ruleset != ev.ruleset:
        raise Invalid(f"This event is on the {ev.ruleset.upper()} ruleset; pick a character from that ruleset.")
    if ev.faction and ch.faction != ev.faction:
        raise Invalid(f"This event is {ev.faction.title()} only.")


def event_requirement(cfg: Config, ev: Event) -> str:
    """Plain description of which characters can join, e.g. "Horde WoW: Forever characters on the RP ruleset"."""
    ed = cfg.edition(ev.edition)
    out = f"{ev.faction.title() + ' ' if ev.faction else ''}{ed.name} characters ({ev.region.upper()})"
    if ed.uses_realms and ev.realm_slug:
        out += f" on {ev.realm_slug}"
    elif not ed.uses_realms and ev.ruleset:
        out += f" on the {ev.ruleset.upper()} ruleset"
    return out


def fit_problems(cfg: Config, ev: Event, ch: Character) -> list[str]:
    """Why a character can't join this event, in words a player understands. Empty means it fits.
    Same rules as _check_character."""
    ed = cfg.edition(ev.edition)
    if ch.edition != ev.edition:
        other = cfg.editions.get(ch.edition)
        return [f"{other.name if other else ch.edition}, not {ed.name}"]
    out = []
    if ch.region != ev.region:
        out.append(f"{ch.region.upper()} region, not {ev.region.upper()}")
    if ed.uses_realms:
        if ev.realm_slug and ch.realm_slug != ev.realm_slug:
            out.append(f"realm {ch.realm_slug or 'not set'}, not {ev.realm_slug}")
    elif ev.ruleset and ch.ruleset != ev.ruleset:
        out.append(f"{(ch.ruleset or 'no').upper()} ruleset, not {ev.ruleset.upper()}")
    if ev.faction and ch.faction != ev.faction:
        out.append(f"{ch.faction.title()}, not {ev.faction.title()}")
    return out


def _check_role(ev: Event, role_key: str | None) -> str | None:
    if not ev.roles:
        return None
    keys = {r.key for r in ev.roles}
    if not role_key:
        raise Invalid("Pick a role: " + ", ".join(r.label for r in ev.roles))
    if role_key not in keys:
        raise Invalid("That role isn't part of this event.")
    return role_key


def _active(s: Session, event_id: int, member_id: int) -> Signup | None:
    return s.scalar(select(Signup).where(Signup.event_id == event_id, Signup.member_id == member_id,
                                         Signup.state.in_(ACTIVE)).with_for_update())


def _try_confirm(s: Session, ev: Event, su: Signup) -> None:
    """Atomic confirm: capacity trigger + member exclusion constraint, all or nothing."""
    def _do():
        su.state = "confirmed"
        su.needs_reconfirm = False
        su.updated_at = now()
        s.flush()
        scheduling.reserve_participant(s, ev, su.member_id)
    scheduling.guarded(s, _do)


def _after(s: Session, cfg: Config, ev: Event) -> None:
    outbox.enqueue(s, "discord.render_event", str(ev.id), {"event_id": ev.id})
    projections.refresh_event(s, cfg, ev)


def join(s: Session, cfg: Config, actor: Actor, event_id: int, character_id: int, role_key: str | None = None,
         tentative: bool = False, recognition: bool | None = None, note: str = "") -> Signup:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = get_event(s, event_id, lock=True)
    _open_for_signup(ev)
    ch = s.get(Character, character_id)
    if ch is None:
        raise NotFound("Character not found.")
    _check_character(cfg, ev, ch, actor.member_id)
    role_key = _check_role(ev, role_key)
    if _active(s, ev.id, actor.member_id):
        raise Conflict("You're already signed up for this event. Use Change or Leave instead.", reason="duplicate")
    if tentative and not cfg["signups"].get("allow_tentative", True):
        raise Invalid("Tentative signups are turned off.")
    if recognition is None:
        m = s.get(Member, actor.member_id)
        recognition = ch.recognition_default if ch.recognition_default is not None else m.recognition_default
    su = Signup(event_id=ev.id, member_id=actor.member_id, character_id=ch.id, role_key=role_key,
                state="tentative" if tentative else "pending", recognition=bool(recognition), note=str(note)[:300])
    try:
        with s.begin_nested():
            s.add(su)
            s.flush()
    except Exception as exc:
        c = scheduling.translate(exc, s)
        if c is None and "ux_signup_active_member" in str(exc):
            raise Conflict("You're already signed up for this event.", reason="duplicate") from None
        raise c or exc
    outcome = su.state
    if not tentative and ev.signup_policy == "open":
        try:
            _try_confirm(s, ev, su)
            outcome = "confirmed"
        except Conflict as c:
            if c.detail.get("reason") in ("role_full", "event_full"):
                su.state = outcome = "waitlisted"
            else:
                # personal time conflict: keep nothing, tell them why
                s.delete(su)
                s.flush()
                raise
    elif not tentative:
        outbox.enqueue(s, "notify.signup_pending", f"{ev.id}:{su.id}", {"event_id": ev.id, "signup_id": su.id})
    audit.record(s, actor, "signup.join", "signup", su.id, event_id=ev.id, state=outcome, role=role_key,
                 recognition=su.recognition)
    _after(s, cfg, ev)
    return su


def _own_active(s: Session, actor: Actor, event_id: int) -> Signup:
    su = _active(s, event_id, actor.member_id)
    if su is None:
        raise NotFound("You're not signed up for this event.")
    return su


def withdraw(s: Session, cfg: Config, actor: Actor, signup_id: int, reason: str = "") -> Signup:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    su = s.get(Signup, signup_id, with_for_update=True)
    if su is None or su.state not in ACTIVE:
        raise NotFound("Signup not found.")
    if su.member_id != actor.member_id:
        raise PermissionDenied("You can only withdraw your own signup.")
    ev = get_event(s, su.event_id, lock=True)
    if ev.state in ("completed", "cancelled"):
        raise InvalidTransition("This event is over.")
    was_confirmed = su.state == "confirmed"
    su.state, su.updated_at = "withdrawn", now()
    s.flush()
    if was_confirmed:
        scheduling.unreserve_participant(s, ev, su.member_id)
        promote_waitlist(s, cfg, ev, su.role_key)
    audit.record(s, actor, "signup.withdraw", "signup", su.id, event_id=ev.id, reason=reason[:200])
    _after(s, cfg, ev)
    return su


def leave(s: Session, cfg: Config, actor: Actor, event_id: int) -> Signup:
    actor = ensure_member(s, actor)
    return withdraw(s, cfg, actor, _own_active(s, actor, event_id).id)


def set_tentative(s: Session, cfg: Config, actor: Actor, event_id: int) -> Signup:
    """Member marks themself tentative. Releases their reservation (tentative never reserves)."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = get_event(s, event_id, lock=True)
    su = _own_active(s, actor, event_id)
    was_confirmed = su.state == "confirmed"
    su.state, su.updated_at = "tentative", now()
    s.flush()
    if was_confirmed:
        scheduling.unreserve_participant(s, ev, su.member_id)
        promote_waitlist(s, cfg, ev, su.role_key)
    audit.record(s, actor, "signup.tentative", "signup", su.id, event_id=ev.id)
    _after(s, cfg, ev)
    return su


def change(s: Session, cfg: Config, actor: Actor, event_id: int, character_id: int | None = None,
           role_key: str | None = None, recognition: bool | None = None) -> Signup:
    """Swap character/role or consent on an existing signup."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = get_event(s, event_id, lock=True)
    su = _own_active(s, actor, event_id)
    if character_id and character_id != su.character_id:
        _open_for_signup(ev)
        ch = s.get(Character, character_id)
        if ch is None:
            raise NotFound("Character not found.")
        _check_character(cfg, ev, ch, actor.member_id)
        su.character_id = ch.id
    if role_key is not None and role_key != su.role_key:
        _open_for_signup(ev)
        role_key = _check_role(ev, role_key)
        old = su.role_key
        scheduling.guarded(s, lambda: setattr(su, "role_key", role_key))  # capacity trigger on role change
        if su.state == "confirmed":
            promote_waitlist(s, cfg, ev, old)
    if recognition is not None:
        su.recognition = bool(recognition)
    su.updated_at = now()
    audit.record(s, actor, "signup.change", "signup", su.id, event_id=ev.id, role=su.role_key,
                 recognition=su.recognition)
    _after(s, cfg, ev)
    if recognition is False and ev.state == "completed":
        from . import chronicle
        chronicle.remove_recognition(s, cfg, actor, ev.id)
    return su


def reconfirm(s: Session, cfg: Config, actor: Actor, event_id: int) -> Signup:
    """After a reschedule: confirmed people clear the flag; bumped/tentative people try to confirm again."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = get_event(s, event_id, lock=True)
    _open_for_signup(ev)
    su = _own_active(s, actor, event_id)
    if su.state == "confirmed":
        su.needs_reconfirm = False
    elif ev.signup_policy == "open" or su.state == "tentative" and su.needs_reconfirm:
        try:
            _try_confirm(s, ev, su)
        except Conflict as c:
            if c.detail.get("reason") in ("role_full", "event_full"):
                su.state = "waitlisted"
            else:
                raise
    else:
        su.state = "pending"
        outbox.enqueue(s, "notify.signup_pending", f"{ev.id}:{su.id}", {"event_id": ev.id, "signup_id": su.id})
    su.updated_at = now()
    audit.record(s, actor, "signup.reconfirm", "signup", su.id, event_id=ev.id, state=su.state)
    _after(s, cfg, ev)
    return su


# --------------------------------------------------------------------------- organizer actions

def _managed(s: Session, cfg: Config, actor: Actor, signup_id: int) -> tuple[Signup, Event, Actor]:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    su = s.get(Signup, signup_id, with_for_update=True)
    if su is None:
        raise NotFound("Signup not found.")
    ev = get_event(s, su.event_id, lock=True)
    require_event_manager(cfg, actor, ev.creator_id, scheduling.cohost_ids(s, ev.id))
    return su, ev, actor


def approve(s: Session, cfg: Config, actor: Actor, signup_id: int) -> Signup:
    su, ev, actor = _managed(s, cfg, actor, signup_id)
    if su.state not in ("pending", "waitlisted", "tentative"):
        raise InvalidTransition(f"A {su.state} signup can't be approved.")
    if ev.state not in ("scheduled", "in_progress"):
        raise InvalidTransition("This event isn't taking signups.")
    _try_confirm(s, ev, su)          # rechecks role/event capacity and the member's own time atomically
    su.decided_by = actor.member_id
    outbox.enqueue(s, "notify.signup_decision", f"{su.id}", {"signup_id": su.id, "decision": "confirmed"})
    audit.record(s, actor, "signup.approve", "signup", su.id, event_id=ev.id)
    _after(s, cfg, ev)
    return su


def reject(s: Session, cfg: Config, actor: Actor, signup_id: int, reason: str = "") -> Signup:
    su, ev, actor = _managed(s, cfg, actor, signup_id)
    if su.state not in ACTIVE:
        raise InvalidTransition("That signup is no longer active.")
    was_confirmed = su.state == "confirmed"
    su.state, su.decided_by, su.updated_at = "rejected", actor.member_id, now()
    s.flush()
    if was_confirmed:
        scheduling.unreserve_participant(s, ev, su.member_id)
        promote_waitlist(s, cfg, ev, su.role_key)
    outbox.enqueue(s, "notify.signup_decision", f"{su.id}", {"signup_id": su.id, "decision": "rejected"})
    audit.record(s, actor, "signup.reject", "signup", su.id, event_id=ev.id, reason=reason[:200])
    _after(s, cfg, ev)
    return su


def waitlist(s: Session, cfg: Config, actor: Actor, signup_id: int) -> Signup:
    su, ev, actor = _managed(s, cfg, actor, signup_id)
    if su.state not in ("pending", "confirmed", "tentative"):
        raise InvalidTransition("That signup can't be waitlisted.")
    was_confirmed = su.state == "confirmed"
    su.state, su.decided_by, su.updated_at = "waitlisted", actor.member_id, now()
    s.flush()
    if was_confirmed:
        scheduling.unreserve_participant(s, ev, su.member_id)
    audit.record(s, actor, "signup.waitlist", "signup", su.id, event_id=ev.id)
    _after(s, cfg, ev)
    return su


def move_role(s: Session, cfg: Config, actor: Actor, signup_id: int, role_key: str) -> Signup:
    su, ev, actor = _managed(s, cfg, actor, signup_id)
    if su.state not in ACTIVE:
        raise InvalidTransition("That signup is no longer active.")
    role_key = _check_role(ev, role_key)
    old = su.role_key
    scheduling.guarded(s, lambda: setattr(su, "role_key", role_key))
    if su.state == "confirmed":
        promote_waitlist(s, cfg, ev, old)
    audit.record(s, actor, "signup.move", "signup", su.id, event_id=ev.id, from_role=old, to_role=role_key)
    _after(s, cfg, ev)
    return su


def promote_waitlist(s: Session, cfg: Config, ev: Event, role_key: str | None) -> Signup | None:
    """Open events: promote the earliest waitlisted signup that now fits (role first, then any).

    Approval events: notify organizers instead; nobody is confirmed without a decision.
    Each attempt is its own savepoint, so a candidate with a personal conflict is skipped.
    """
    q = select(Signup).where(Signup.event_id == ev.id, Signup.state == "waitlisted").order_by(Signup.created_at)
    candidates = list(s.scalars(q))
    if not candidates:
        return None
    if ev.signup_policy == "approval":
        outbox.enqueue(s, "notify.slot_open", str(ev.id), {"event_id": ev.id, "role": role_key})
        return None
    candidates.sort(key=lambda c: (c.role_key != role_key, c.created_at))
    for c in candidates:
        try:
            _try_confirm(s, ev, c)
        except Conflict:
            continue
        outbox.enqueue(s, "notify.signup_decision", f"{c.id}", {"signup_id": c.id, "decision": "promoted"})
        audit.record(s, Actor("0", "worker"), "signup.promote", "signup", c.id, event_id=ev.id)
        return c
    return None


def roster(s: Session, event_id: int) -> list[Signup]:
    return list(s.scalars(select(Signup).where(Signup.event_id == event_id, Signup.state.in_(ACTIVE))
                          .order_by(Signup.created_at)))
