"""Members and the character roster."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Character, Member, Signup
from . import audit, catalog
from .actor import Actor
from .errors import Conflict, Invalid, NotFound, PermissionDenied
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
        raise Invalid("Character name must be letters only (2–24), optionally with a second part.")
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
    race, klass = (data.get("race_key") or "").strip().lower(), (data.get("class_key") or "").strip().lower()
    if not race or not klass:
        raise Invalid("Race and class are required.")
    allowed = catalog.race_class_allowed(s, ed.key, faction, race, klass)
    if allowed is False:
        raise Invalid(f"{race.replace('_', ' ').title()} {klass} is not a valid {faction.title()} "
                      f"combination in {ed.name}.")
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
        raise Invalid("Preferred role must be one of the character's eligible roles.")
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


def _own(s: Session, actor: Actor, character_id: int) -> Character:
    ch = s.get(Character, character_id)
    if ch is None or ch.archived_at is not None:
        raise NotFound("Character not found.")
    if ch.member_id != actor.member_id:
        raise PermissionDenied("You can only change your own characters.")
    return ch


def update_character(s: Session, cfg: Config, actor: Actor, character_id: int, data: dict) -> Character:
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
    if timezone:
        from .timeutil import zone
        zone(timezone)
        m.timezone = timezone
    audit.record(s, actor, "member.prefs", "member", m.id)
    return m


def staff_can_view_roster(cfg: Config, actor: Actor) -> bool:
    return has(cfg, actor, "event_moderator") or has(cfg, actor, "admin")
