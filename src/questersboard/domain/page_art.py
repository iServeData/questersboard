"""[page_art]: the banner at the top of every page and the picture above the footer, one pair a day.

The banner list moves forward one step a day and the footer list moves back one step a day. When the
two list lengths share no factor (3 and 4, 4 and 5, ...) that alone shows every pairing once before any
repeats. When they do share one (3 and 3), the footer skips one extra step each time the next pairing
would be one already shown, so every pairing still comes up once per cycle. Pairings listed in
[page_art] skip_pairs are left out. The day changes at midnight in [page_art] timezone.

Everything comes from configuration: adding, removing or reordering pictures needs no code change.
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from .timeutil import now


@lru_cache(maxsize=32)
def sequence(n_banners: int, n_footers: int, skip: frozenset = frozenset()) -> tuple[tuple[int, int], ...]:
    """One full cycle as (banner index, footer index) per day, skipped pairings removed."""
    if n_banners < 1 or n_footers < 1:
        return ()
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int]] = []
    b = f = 0
    for step in range(n_banners * n_footers):
        if step:
            b = (b + 1) % n_banners
            f = (f - 1) % n_footers
            for _ in range(n_footers):          # already shown with this banner: one more step back
                if (b, f) not in seen:
                    break
                f = (f - 1) % n_footers
        seen.add((b, f))
        out.append((b, f))
    return tuple(p for p in out if p not in skip)


def repeats(seq) -> list[int]:
    """Days (0-based, the cycle wraps) whose banner or footer is the same as the day before."""
    n = len(seq)
    if n < 2:
        return []
    return [i for i in range(n) if seq[i][0] == seq[i - 1][0] or seq[i][1] == seq[i - 1][1]]


def _skip_set(pa: dict) -> frozenset:
    bk = [x["key"] for x in pa["banners"]]
    fk = [x["key"] for x in pa["footers"]]
    return frozenset((bk.index(b), fk.index(f)) for b, f in pa["skip_pairs"])


def cycle(pa: dict) -> tuple[tuple[int, int], ...]:
    return sequence(len(pa["banners"]), len(pa["footers"]), _skip_set(pa))


def day_number(pa: dict, today: date) -> int:
    return (today - date.fromisoformat(pa["start_date"])).days


def pick(cfg, at: datetime | None = None) -> dict | None:
    """Today's banner and footer (the validated config entries), or None when [page_art] is off."""
    pa = cfg["page_art"]
    if not pa["enabled"]:
        return None
    seq = cycle(pa)
    if not seq:
        return None
    tz = ZoneInfo(pa["timezone"] or cfg["community"]["default_timezone"])
    today = (at or now()).astimezone(tz).date()
    bi, fi = seq[day_number(pa, today) % len(seq)]
    return {"banner": pa["banners"][bi], "footer": pa["footers"][fi], "fx_script": pa["fx_script"]}


SECTIONS = {"quests": "Quests", "characters": "Characters", "chronicle": "Chronicle", "help": "Help"}


def section(path: str, mp: str, cp: str) -> str:
    """The banner's big word: the same sections as the submenu (_subnav.html)."""
    if path.startswith(mp + "/characters") or path.startswith(mp + "/players") or path.startswith(mp + "/guilds/"):
        return SECTIONS["characters"]
    if path.startswith(cp) or path.startswith(mp + "/chronicle/") or path.startswith(mp + "/stories/") \
            or path == mp + "/review":
        return SECTIONS["chronicle"]
    if path in (mp + "/help", mp + "/privacy", mp + "/terms", mp + "/changelog"):
        return SECTIONS["help"]
    if path == mp + "/" or path.startswith(mp + "/events"):
        return SECTIONS["quests"]
    return ""
