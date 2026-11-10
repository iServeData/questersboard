"""Members and the character roster."""

from __future__ import annotations

import re
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Character, Member, Signup
from . import audit, catalog
from .actor import Actor
from .errors import Conflict, EditionLocked, Invalid, NeedsLeave, NotFound, PermissionDenied
from .permissions import has, require_member
from .timeutil import now

NAME_RE = re.compile(r"^[^\W\d_][^\W\d_' -]{1,23}(?: [^\W\d_][^\W\d_'-]{1,23})?$", re.UNICODE)
ROLE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
COMBAT_ROLES = {"tank", "healer", "damage"}


def ensure_member(s: Session, actor: Actor) -> Actor:
    """Find or create the member row for a Discord identity; returns the actor with member_id set."""
    if actor.source == "worker":
        return actor
    m = s.scalar(select(Member).where(Member.discord_id == actor.discord_id))
    if m is None:
        m = Member(discord_id=actor.discord_id, display_name=actor.display_name[:100])
        s.add(m)
        s.flush()
    elif actor.display_name and m.display_name != actor.display_name[:100]:
        m.display_name = actor.display_name[:100]
    if actor.username and m.username != actor.username[:40]:
        m.username = actor.username[:40]
    return actor.with_member(m.id)


def _clean_tags(tags, limit=12) -> list[str]:
    out = []
    for t in tags or []:
        t = str(t).strip().lower().replace(" ", "_")[:32]
        if t and ROLE_KEY.match(t) and t not in out:
            out.append(t)
    return out[:limit]


def _validate(s: Session, cfg: Config, data: dict) -> dict:
    ed = cfg.edition(data.get("edition") or next(iter(cfg.editions)))
    region = data.get("region") or ed.default_region
    if region not in ed.regions:
        raise Invalid(f"{ed.name} supports regions: {', '.join(ed.regions)}")
    name = (data.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise Invalid("WoW character names are letters only: Blizzard doesn't allow numbers, symbols or spaces "
                      "(accents like ñ or ö are fine). Enter the name exactly as it is in game. WoW: Forever names "
                      "have two parts, so type both with a space between, e.g. \"Zanra Stormhide\".")
    out = dict(edition=ed.key, region=region, name=name)
    if ed.uses_realms:
        realm = (data.get("realm_slug") or "").strip().lower()
        if not re.match(r"^[a-z0-9-]{2,64}$", realm):
            raise Invalid(f"{ed.name} characters need a realm.")
        out["realm_slug"], out["realm_name"], out["ruleset"] = realm, (data.get("realm_name") or realm)[:64], None
    else:
        # Realmless edition (WoW: Forever): no realm at all, ruleset instead.
        if data.get("realm_slug"):
            raise Invalid(f"{ed.name} has no realms; choose a ruleset instead.")
        rs = data.get("ruleset") or ed.default_ruleset
        if rs not in ed.rulesets:
            raise Invalid(f"Ruleset must be one of: {', '.join(ed.rulesets)}")
        out["ruleset"], out["realm_slug"], out["realm_name"] = rs, None, None
    faction = data.get("faction") or ed.default_faction
    if faction not in ed.factions:
        raise Invalid("Unknown faction")
    race_text, class_text = (data.get("race_key") or "").strip(), (data.get("class_key") or "").strip()
    if not race_text or not class_text:
        raise Invalid("Race and class are required.")
    if len(race_text) > 48 or len(class_text) > 48:
        raise Invalid("Race and class must be 48 characters or fewer.")
    # Catalog entries are suggestions: anything typed is accepted, except a pairing the
    # catalog positively rules out (a known race on the wrong faction, or a known race
    # with a known class it cannot be).
    race_o = catalog.resolve(s, ed.key, "race", race_text, faction)
    class_o = catalog.resolve(s, ed.key, "class", class_text)
    if race_o is None:
        other = catalog.resolve(s, ed.key, "race", race_text)
        if other is not None and other.parent_key != faction:
            art = "an" if other.parent_key[:1] in "aeiou" else "a"
            raise Invalid(f"{other.label} is {art} {other.parent_key.title()} race in {ed.name}. "
                          f"Switch the faction to {other.parent_key.title()}, or pick a {faction.title()} race.")
    race = race_o.key if race_o else catalog._norm(race_text)
    klass = class_o.key if class_o else catalog._norm(class_text)
    if not race or not klass:
        raise Invalid("Race and class need at least one letter or number.")
    if race_o is not None and class_o is not None:
        allowed = catalog.race_class_allowed(s, ed.key, faction, race, klass)
        if allowed is False:
            m = catalog.race_class_map(s, ed.key)
            keys = next((r["classes"] for r in m["factions"].get(faction, []) if r["key"] == race), [])
            ok = [m["classes"].get(k, k.title()) for k in keys]
            raise Invalid(f"{race_o.label} can't be a {class_o.label} in {ed.name}. "
                          f"{race_o.label} classes: {', '.join(ok) or 'none listed'}.")
    level = data.get("level")
    if level not in (None, ""):
        try:
            level = int(level)
        except (TypeError, ValueError):
            raise Invalid("Level must be a number") from None
        if not 1 <= level <= ed.max_level:
            raise Invalid(f"Level must be between 1 and {ed.max_level}")
    else:
        level = None
    roles = _clean_tags(data.get("eligible_roles"), 8)
    pref = data.get("preferred_role") or None
    if pref and pref not in roles:
        # Preferring a role means the character can fill it; don't make the player tick it twice.
        roles = _clean_tags(roles + [pref], 8)
    out.update(faction=faction, race_key=race, class_key=klass, level=level,
               spec=(data.get("spec") or None) and str(data["spec"])[:48],
               eligible_roles=roles, preferred_role=pref, focus_tags=_clean_tags(data.get("focus_tags")),
               notes=str(data.get("notes") or "")[:500],
               recognition_default=data.get("recognition_default"))
    return out


def add_character(s: Session, cfg: Config, actor: Actor, data: dict) -> Character:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    vals = _validate(s, cfg, data)
    ch = Character(member_id=actor.member_id, verification="self_reported", **vals)
    try:
        with s.begin_nested():
            s.add(ch)
            s.flush()
    except IntegrityError:
        raise Conflict("You already have a character with that name here.") from None
    audit.record(s, actor, "character.add", "character", ch.id, edition=ch.edition)
    return ch


def import_verified(s: Session, cfg: Config, actor: Actor, edition: str, region: str,
                    picks: list[tuple[dict, str | None]]) -> tuple[list[str], list[tuple[str, str]]]:
    """Add characters read from the member's own Battle.net account (see Blizzard.link_account).

    Each pick is (snapshot entry, ruleset chosen on the review page). They're added as ownership verified;
    roles, spec and interests start empty for the member to fill in. Returns (added names,
    [(name, why it was skipped)])."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ed = cfg.edition(edition)
    added, skipped = [], []
    for c, ruleset in picks:
        name = str(c.get("name") or "")
        data = {"edition": ed.key, "region": region, "name": name, "faction": c.get("faction") or None,
                "race_key": c.get("race") or "", "class_key": c.get("class") or "",
                "level": c.get("level") if c.get("level") and 1 <= c["level"] <= ed.max_level else None}
        if ed.uses_realms:
            data["realm_slug"] = c.get("realm") or ""
        else:
            data["ruleset"] = ruleset or c.get("ruleset") or ""
        if not data["faction"] or data["faction"] not in ed.factions:
            skipped.append((name, "Blizzard didn't say which faction it is; add it with the form instead"))
            continue
        try:
            vals = _validate(s, cfg, data)
        except Invalid as exc:
            skipped.append((name, exc.message))
            continue
        ch = Character(member_id=actor.member_id, verification="ownership_verified",
                       api_external_id=str(c.get("id") or "") or None, api_refreshed_at=now(), **vals)
        try:
            with s.begin_nested():
                s.add(ch)
                s.flush()
        except IntegrityError:
            skipped.append((name, "already on WoWQuester (yours or verified by someone else)"))
            continue
        audit.record(s, actor, "character.import", "character", ch.id, edition=ch.edition)
        added.append(ch.name)
    return added, skipped


def _own(s: Session, actor: Actor, character_id: int) -> Character:
    ch = s.get(Character, character_id)
    if ch is None or ch.archived_at is not None:
        raise NotFound("Character not found.")
    if ch.member_id != actor.member_id:
        raise PermissionDenied("You can only change your own characters.")
    return ch


def _check_events_still_fit(s: Session, cfg: Config, actor: Actor, ch: Character, vals: dict,
                            leave_events: bool = False) -> None:
    """A character edit must not leave it in events it can't be in.
    - Edition: a Forever character and a Retail character are different characters in game, so the edition
      can only change if every event it was ever in (any signup) is in the new edition. A character that has
      never joined anything can switch freely, which is how a wrong pick gets fixed.
    - Ruleset, realm, region, faction: in-game transfers and faction changes are real, so these may change,
      but not while it's signed up to an upcoming event the new values don't fit. Leave those first, or
      pass leave_events=True (the "Leave these events and save" button) to leave them all here."""
    from ..models import Event, Signup
    from .signups import ACTIVE, fit_problems
    rows = s.execute(select(Signup, Event).join(Event, Event.id == Signup.event_id)
                     .where(Signup.character_id == ch.id)).all()
    if not rows:
        return
    probe = SimpleNamespace(**{k: vals.get(k, getattr(ch, k)) for k in
                               ("edition", "region", "ruleset", "realm_slug", "faction")})
    if vals["edition"] != ch.edition:
        other = sorted({ev.title for _, ev in rows if ev.edition != vals["edition"]})
        if other:
            raise EditionLocked(f"{ch.name} has been in {cfg.edition(ch.edition).name} events ({', '.join(other[:5])}), "
                          f"so it can't become a {cfg.edition(vals['edition']).name} character. A character in "
                          f"another edition is a different character in game: add it as a new one instead.")
    blocked = sorted(((ev, su) for su, ev in rows if su.state in ACTIVE
                      and ev.state in ("draft", "scheduled", "in_progress") and fit_problems(cfg, ev, probe)),
                     key=lambda p: p[0].start_at)
    if not blocked:
        return
    if leave_events:
        from .signups import leave
        for ev, _ in blocked:
            leave(s, cfg, actor, ev.id)
        return
    names = [ev.title for ev, _ in blocked]
    one = len(names) == 1
    raise NeedsLeave(f"{ch.name} is signed up for {len(names)} event{'' if one else 's'} the changed character "
                     f"wouldn't fit. Leave {'it' if one else 'them'} and save, or keep the character as it is.",
                     [{"title": ev.title, "public_id": str(ev.public_id), "start_at": ev.start_at,
                       "timezone": ev.timezone, "state": su.state, "why": fit_problems(cfg, ev, probe)}
                      for ev, su in blocked])


def update_character(s: Session, cfg: Config, actor: Actor, character_id: int, data: dict,
                     leave_events: bool = False) -> Character:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ch = _own(s, actor, character_id)
    merged = {k: getattr(ch, k) for k in ("edition", "region", "ruleset", "realm_slug", "realm_name", "name",
                                          "faction", "race_key", "class_key", "level", "spec", "eligible_roles",
                                          "preferred_role", "focus_tags", "notes", "recognition_default")}
    merged.update({k: v for k, v in data.items() if k in merged})
    if "member_id" in data and data["member_id"] != ch.member_id:
        raise PermissionDenied("Character ownership cannot be changed.")
    vals = _validate(s, cfg, merged)
    _check_events_still_fit(s, cfg, actor, ch, vals, leave_events)
    identity_changed = any(vals[k] != getattr(ch, k) for k in ("name", "edition", "region", "realm_slug", "ruleset",
                                                               "race_key", "class_key", "faction"))
    for k, v in vals.items():
        setattr(ch, k, v)
    if identity_changed and ch.verification != "self_reported":
        ch.verification, ch.api_external_id, ch.api_refreshed_at = "self_reported", None, None
    ch.updated_at = now()
    try:
        with s.begin_nested():
            s.flush()
    except IntegrityError:
        raise Conflict("You already have a character with that name here.") from None
    audit.record(s, actor, "character.update", "character", ch.id, identity_changed=identity_changed)
    return ch


def archive_character(s: Session, cfg: Config, actor: Actor, character_id: int) -> None:
    """Archive keeps history (past events still show who attended). Active signups are withdrawn."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ch = _own(s, actor, character_id)
    from . import signups  # local import to avoid a cycle
    for su in s.scalars(select(Signup).where(Signup.character_id == ch.id,
                                             Signup.state.in_(("pending", "confirmed", "tentative", "waitlisted")))):
        signups.withdraw(s, cfg, actor, su.id, reason="character archived")
    ch.archived_at = now()
    audit.record(s, actor, "character.archive", "character", ch.id)


def remove_character(s: Session, cfg: Config, actor: Actor, character_id: int) -> str:
    """Delete when never used; otherwise archive (history keeps its references)."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ch = _own(s, actor, character_id)
    used = s.scalar(select(Signup.id).where(Signup.character_id == ch.id).limit(1))
    if used:
        archive_character(s, cfg, actor, character_id)
        return "archived"
    s.delete(ch)
    audit.record(s, actor, "character.delete", "character", character_id)
    return "deleted"


def list_characters(s: Session, member_id: int, include_archived: bool = False) -> list[Character]:
    stmt = select(Character).where(Character.member_id == member_id)
    if not include_archived:
        stmt = stmt.where(Character.archived_at.is_(None))
    return list(s.scalars(stmt.order_by(Character.name)))


def set_member_prefs(s: Session, cfg: Config, actor: Actor, recognition_default: bool | None = None,
                     timezone: str | None = None) -> Member:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    m = s.get(Member, actor.member_id)
    if recognition_default is not None:
        m.recognition_default = bool(recognition_default)
    if timezone:                       # None = leave as is, "" = back to the community default
        from .timeutil import zone
        zone(timezone)
        m.timezone = timezone
    elif timezone == "":
        m.timezone = None
    audit.record(s, actor, "member.prefs", "member", m.id)
    return m


def staff_can_view_roster(cfg: Config, actor: Actor) -> bool:
    return has(cfg, actor, "event_moderator") or has(cfg, actor, "admin")
