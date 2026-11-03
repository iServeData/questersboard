"""Turn what a host types (Discord usernames, display names or numeric IDs) into Discord IDs."""
from __future__ import annotations

import re
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Member
from .errors import Invalid

_ID = re.compile(r"^\d{15,22}$")
_MENTION = re.compile(r"^<@!?(\d{15,22})>$")

# search(query) -> [{"id": "...", "username": "...", "global_name": "..."}]; set by the web app when the
# bot token is available (Discord guild member search). Without it, only people who used WoWQuester match.
Search = Callable[[str], list[dict]]


def split(raw: str) -> list[str]:
    """Commas (or new lines) separate people. A run of numeric IDs separated by spaces also works."""
    out: list[str] = []
    for part in re.split(r"[,\n;]+", raw or ""):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        if len(bits) > 1 and all(_ID.match(b) or _MENTION.match(b) for b in bits):
            out.extend(bits)
        else:
            out.append(part)
    return out


def label(m: Member) -> str:
    """How a member is shown in name fields: @username when known, else display name, else the ID."""
    if m.username:
        return "@" + m.username
    return m.display_name or m.discord_id


def resolve(s: Session, tokens: list[str], search: Search | None = None) -> list[str]:
    ids, missing, unclear = [], [], []
    for raw in tokens:
        t = raw.strip()
        m = _MENTION.match(t)
        if m:
            t = m.group(1)
        if _ID.match(t):
            ids.append(t)
            continue
        name = t.lstrip("@").strip()
        if not name:
            continue
        hit = s.scalar(select(Member).where(func.lower(Member.username) == name.lower(),
                                            Member.erased_at.is_(None)))
        if hit is None:
            same = list(s.scalars(select(Member).where(func.lower(Member.display_name) == name.lower(),
                                                       Member.erased_at.is_(None)).limit(2)))
            if len(same) == 1:
                hit = same[0]
            elif len(same) > 1:
                unclear.append(raw)
                continue
        if hit is None and search is not None:
            found = [u for u in (search(name) or []) if (u.get("username") or "").lower() == name.lower()]
            if len(found) == 1:
                u = found[0]
                hit = s.scalar(select(Member).where(Member.discord_id == str(u["id"])))
                if hit is None:
                    hit = Member(discord_id=str(u["id"]))
                    s.add(hit)
                hit.username = (u.get("username") or "")[:40]
                hit.display_name = hit.display_name or (u.get("global_name") or u.get("username") or "")[:100]
                s.flush()
        if hit is None:
            missing.append(raw)
        else:
            ids.append(hit.discord_id)
    if unclear:
        raise Invalid("More than one person uses the name " + ", ".join(unclear)
                      + ". Use their Discord username (the unique one, shown under their name in Discord).")
    if missing:
        raise Invalid("Couldn't find " + ", ".join(missing) + ". Use their Discord username; if it still "
                      "isn't found, ask them to sign in to WoWQuester once, or paste their Discord ID.")
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out
