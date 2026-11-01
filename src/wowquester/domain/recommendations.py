"""Explainable, rule-based suggestions. No machine learning, no hidden scoring.

Every suggestion lists the exact reasons it was made, so members can see (and
organizers can argue with) the logic. Inputs: the member's own characters (roles,
focus tags, level), upcoming public events, open role slots, and the configured
activity catalog. Nothing here is stored or shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Character, Event, Signup
from .timeutil import fmt_local, now


@dataclass
class Suggestion:
    kind: str                 # "event" | "activity"
    title: str
    score: int
    reasons: list[str] = field(default_factory=list)
    event_public_id: str | None = None
    character_id: int | None = None


def for_member(s: Session, cfg: Config, member_id: int, limit: int = 6) -> list[Suggestion]:
    chars = list(s.scalars(select(Character).where(Character.member_id == member_id,
                                                   Character.archived_at.is_(None))))
    if not chars:
        return [Suggestion("activity", "Add a character to get suggestions", 0,
                           ["Suggestions are based on your characters' roles and interests."])]
    out: list[Suggestion] = []
    mine = set(s.scalars(select(Signup.event_id).where(Signup.member_id == member_id,
                                                       Signup.state.in_(("pending", "confirmed", "tentative",
                                                                         "waitlisted")))))
    upcoming = list(s.scalars(select(Event).where(Event.state == "scheduled", Event.start_at > now())
                              .order_by(Event.start_at).limit(40)))
    for ev in upcoming:
        if ev.id in mine or ev.creator_id == member_id:
            continue
        filled = dict(s.execute(select(Signup.role_key, func.count()).where(
            Signup.event_id == ev.id, Signup.state == "confirmed").group_by(Signup.role_key)).all())
        for ch in chars:
            if ch.edition != ev.edition or ch.region != ev.region:
                continue
            if ev.ruleset and ch.ruleset != ev.ruleset:
                continue
            if ev.realm_slug and ch.realm_slug != ev.realm_slug:
                continue
            if ev.faction and ch.faction != ev.faction:
                continue
            score, reasons = 10, [f"{ch.name} fits this event ({cfg.edition(ev.edition).name}"
                                  f"{', ' + ev.ruleset.upper() if ev.ruleset else ''}"
                                  f"{', ' + ev.faction.title() if ev.faction else ''})."]
            open_roles = [r for r in ev.roles if filled.get(r.key, 0) < r.quantity]
            match = [r for r in open_roles if r.key in (ch.eligible_roles or [])]
            if ev.roles and not open_roles:
                continue
            if match:
                pref = next((r for r in match if r.key == ch.preferred_role), match[0])
                score += 30 if pref.key == ch.preferred_role else 20
                left = pref.quantity - filled.get(pref.key, 0)
                reasons.append(f"Needs {left} more {pref.label.lower()}{'s' if left != 1 else ''}, "
                               f"a role {ch.name} can play.")
            elif ev.roles:
                score -= 5
                reasons.append("Open slots don't match this character's listed roles.")
            tags = set(ch.focus_tags or [])
            act_tags = next((set(a.tags) for a in cfg.activities if a.type == ev.activity_type), set())
            common = tags & (act_tags | {ev.activity_type})
            if common:
                score += 10 * len(common)
                reasons.append("Matches your interests: " + ", ".join(sorted(common)) + ".")
            hours = (ev.start_at - now()).total_seconds() / 3600
            if hours < 48:
                score += 5
                reasons.append(f"Starts soon: {fmt_local(ev.start_at, ev.timezone)}.")
            out.append(Suggestion("event", ev.title, score, reasons, str(ev.public_id), ch.id))
    # activity ideas for when nothing is scheduled that fits
    if len(out) < limit:
        for a in cfg.activities:
            for ch in chars:
                tags = set(ch.focus_tags or [])
                common = tags & set(a.tags)
                if not common:
                    continue
                reasons = [f"{ch.name} lists {', '.join(sorted(common))}.",
                           f"Typical size {a.min_size}–{a.max_size}, about {a.min_minutes}+ minutes "
                           "(organizer guidance from the community's settings)."]
                if a.roles:
                    need = ", ".join(f"{n} {k}" for k, n in a.roles.items())
                    reasons.append(f"Usual composition: {need}.")
                out.append(Suggestion("activity", f"Organize: {a.label}", 5 * len(common), reasons,
                                      character_id=ch.id))
                break
    best: dict[tuple, Suggestion] = {}
    for sug in out:
        k = (sug.kind, sug.event_public_id or sug.title)
        if k not in best or sug.score > best[k].score:
            best[k] = sug
    return sorted(best.values(), key=lambda x: -x.score)[:limit]
