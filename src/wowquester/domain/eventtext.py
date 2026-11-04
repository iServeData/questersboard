"""Short text about an event shared by the Discord post, the event page and the Chronicle."""

from __future__ import annotations

from ..models import Event


def level_text(ev: Event) -> str:
    if ev.level_min and ev.level_max:
        return f"Level {ev.level_min}" if ev.level_min == ev.level_max else f"Level {ev.level_min}–{ev.level_max}"
    if ev.level_min:
        return f"Level {ev.level_min}+"
    if ev.level_max:
        return f"Up to level {ev.level_max}"
    return ""


def difficulty_line(ev: Event) -> str:
    return " · ".join(x for x in (ev.difficulty, level_text(ev)) if x)
