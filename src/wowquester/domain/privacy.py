"""Who may be named in public, and how everyone else is described.

Rules (applied to the participant list, the prose and the screenshots):
* A participant is named only with their WoW character name, and only when they
  consented for this event (signup.recognition) and have not since asked for removal.
* Everyone else gets a neutral description ("a healer"), never an invented WoW name.
* Discord display names, usernames, mentions and IDs never appear publicly.
* Screenshots publish only after a reviewer (or a trusted author on their own entry)
  clears them; a recognition removal un-clears every screenshot of that event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Attendance, Character, Member, RecognitionRemoval, Signup

SNOWFLAKE = re.compile(r"(?<!\d)\d{17,20}(?!\d)")
MENTION = re.compile(r"<[@#][!&]?\d+>|@everyone|@here")


@dataclass
class Participant:
    member_id: int
    character_name: str
    race_key: str
    class_key: str
    role_key: str | None
    role_label: str | None
    consent: bool
    discord_name: str
    attended: bool = True

    def public_label(self, cfg: Config) -> str:
        if self.consent:
            return self.character_name
        return neutral(cfg, self.role_label, self.class_key)


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def neutral(cfg: Config, role_label: str | None, class_key: str | None) -> str:
    style = cfg["chronicle"].get("neutral_description_style", "role")
    role = (role_label or "").strip().lower()
    if style == "role_class" and class_key:
        noun = f"{class_key.replace('_', ' ')} {role}".strip() if role else class_key.replace("_", " ")
    else:
        noun = role or "adventurer"
    return f"{_article(noun)} {noun}"


def participants(s: Session, cfg: Config, event_id: int, roles: dict[str, str]) -> list[Participant]:
    """People recorded as attended. Signing up alone is never treated as attendance."""
    removed = set(s.scalars(select(RecognitionRemoval.member_id).where(RecognitionRemoval.event_id == event_id)))
    out = []
    rows = s.execute(select(Attendance, Character, Member).join(Character, Character.id == Attendance.character_id)
                     .join(Member, Member.id == Attendance.member_id)
                     .where(Attendance.event_id == event_id, Attendance.status == "attended")
                     .order_by(Character.name)).all()
    for att, ch, m in rows:
        su = s.scalar(select(Signup).where(Signup.event_id == event_id, Signup.member_id == m.id)
                      .order_by(Signup.id.desc()).limit(1))
        consent = bool(su and su.recognition) and m.id not in removed and m.erased_at is None
        out.append(Participant(m.id, ch.name, ch.race_key, ch.class_key, att.role_key,
                               roles.get(att.role_key or "", None), consent, m.display_name or ""))
    return out


def involved(s: Session, cfg: Config, event_id: int, roles: dict[str, str]) -> list[Participant]:
    """Everyone connected to the event (any signup state, or any attendance row).

    Used for privacy scanning and redaction: a player who signed up but whose attendance
    is unknown, absent, withdrawn or rejected must still never be named without consent.
    """
    removed = set(s.scalars(select(RecognitionRemoval.member_id).where(RecognitionRemoval.event_id == event_id)))
    seen: dict[tuple[int, int], Participant] = {}
    for su, ch, m in s.execute(select(Signup, Character, Member).join(Character, Character.id == Signup.character_id)
                               .join(Member, Member.id == Signup.member_id)
                               .where(Signup.event_id == event_id).order_by(Signup.id)).all():
        latest = s.scalar(select(Signup.recognition).where(Signup.event_id == event_id, Signup.member_id == m.id)
                          .order_by(Signup.id.desc()).limit(1))
        consent = bool(latest) and m.id not in removed and m.erased_at is None
        seen[(m.id, ch.id)] = Participant(m.id, ch.name, ch.race_key, ch.class_key, su.role_key,
                                          roles.get(su.role_key or "", None), consent, m.display_name or "")
    for att, ch, m in s.execute(select(Attendance, Character, Member).join(Character, Character.id == Attendance.character_id)
                                .join(Member, Member.id == Attendance.member_id)
                                .where(Attendance.event_id == event_id)).all():
        if (m.id, ch.id) not in seen:
            seen[(m.id, ch.id)] = Participant(m.id, ch.name, ch.race_key, ch.class_key, att.role_key,
                                              roles.get(att.role_key or "", None), False, m.display_name or "")
    attended = set(s.scalars(select(Attendance.member_id).where(Attendance.event_id == event_id,
                                                               Attendance.status == "attended")))
    for p in seen.values():
        p.attended = p.member_id in attended
    # one person may have used several characters: consent is per person for this event
    by_member: dict[int, bool] = {}
    for p in seen.values():
        by_member[p.member_id] = by_member.get(p.member_id, True) and p.consent
    for p in seen.values():
        p.consent = by_member[p.member_id]
    return list(seen.values())


def blocked_terms(people: list[Participant]) -> list[str]:
    """Words that must not appear in public text for this event."""
    terms = set()
    for p in people:
        if not p.consent:
            terms.add(p.character_name)
        if p.discord_name and len(p.discord_name) >= 3 and p.discord_name.lower() != p.character_name.lower():
            terms.add(p.discord_name)
    return sorted(terms, key=len, reverse=True)


def scan(text: str, people: list[Participant]) -> list[str]:
    """Return readable problems; empty list = publication-safe."""
    problems = []
    if SNOWFLAKE.search(text):
        problems.append("The text contains what looks like a Discord ID. Remove it.")
    if MENTION.search(text):
        problems.append("The text contains a Discord mention. Use WoW character names (with consent) instead.")
    for term in blocked_terms(people):
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE):
            who = next((p for p in people if term in (p.character_name, p.discord_name)), None)
            if who and term == who.discord_name and who.consent:
                problems.append("The text uses someone's Discord name. Use their WoW character name instead.")
            else:
                problems.append("The text names a participant who hasn't agreed to be named. "
                                "Describe them neutrally (for example “our healer”).")
    return sorted(set(problems))


def redact(text: str, people: list[Participant], cfg: Config) -> str:
    """Last line of defense for already-published text after a recognition removal."""
    for p in people:
        repl = p.public_label(cfg)
        for term in {t for t in (p.discord_name, None if p.consent else p.character_name) if t and len(t) >= 2}:
            text = re.sub(rf"(?<!\w){re.escape(term)}(?!\w)", repl, text, flags=re.IGNORECASE)
    text = MENTION.sub("someone", text)
    return SNOWFLAKE.sub("[removed]", text)
