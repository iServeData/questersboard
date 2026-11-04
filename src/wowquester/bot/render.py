"""The canonical Discord post for an event: one message, edited in place on every change.

Shared by the worker (which posts/edits through REST) and the bot (ephemeral previews).
Buttons carry stable custom_ids "wq:ev:<action>:<event_id>" handled by persistent
DynamicItems, so they keep working after restarts and never embed user data.
Public posts show WoW character names only for people who consented for this event;
others are counted ("+2 others"). Discord IDs and display names are never rendered.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..domain import covers
from ..domain.eventtext import difficulty_line, level_text  # noqa: F401  (re-exported)
from ..domain.timeutil import discord_ts, zone
from ..models import Character, Event, Series, Signup

STATE_LABEL = {"scheduled": "Scheduled", "in_progress": "In progress", "completed": "Completed",
               "cancelled": "Cancelled", "draft": "Draft"}
COLOR = {"scheduled": 0xC9A227, "in_progress": 0x2E8B57, "completed": 0x6B7280, "cancelled": 0x8B1E1E,
         "draft": 0x444444}
ACTIVITY = {"raid": "Raid", "dungeon": "Dungeon", "quest": "Questing", "rp": "Roleplay", "arena": "Arena",
            "battleground": "Battleground", "world_pvp": "World PvP", "lfg": "Looking for group",
            "social": "Social", "custom": "Activity"}


def cid(action: str, event_id: int) -> str:
    return f"wq:ev:{action}:{event_id}"


def _who(s: Session, ev: Event, state: str, role_key: str | None) -> tuple[list[str], int]:
    rows = s.execute(select(Signup, Character).join(Character, Character.id == Signup.character_id).where(
        Signup.event_id == ev.id, Signup.state == state,
        Signup.role_key.is_(None) if role_key is None else Signup.role_key == role_key)
        .order_by(Signup.created_at)).all()
    named = [c.name for su, c in rows if su.recognition]
    return named, len(rows) - len(named)


def _all(s: Session, ev: Event, state: str) -> tuple[list[str], int]:
    rows = s.execute(select(Signup, Character).join(Character, Character.id == Signup.character_id).where(
        Signup.event_id == ev.id, Signup.state == state).order_by(Signup.created_at)).all()
    named = [c.name for su, c in rows if su.recognition]
    return named, len(rows) - len(named)


def _line(names: list[str], others: int) -> str:
    parts = names[:12]
    extra = others + max(0, len(names) - 12)
    if extra:
        parts.append(f"+{extra} other{'s' if extra != 1 else ''}")
    return ", ".join(parts) or "—"


def duration_text(ev: Event) -> str:
    mins = int((ev.end_at - ev.start_at).total_seconds() // 60)
    h, m = divmod(mins, 60)
    parts = ([f"{h} hour{'s' if h != 1 else ''}"] if h else []) + ([f"{m} minutes"] if m else [])
    return " ".join(parts) or "0 minutes"


def repeat_text(s: Session, ev: Event) -> str:
    if not ev.series_id or ev.series_detached:
        return ""
    ser = s.get(Series, ev.series_id)
    if ser is None:
        return ""
    rule = dict(p.split("=", 1) for p in ser.rrule.upper().split(";") if "=" in p)
    n = int(rule.get("INTERVAL", "1") or 1)
    day = ev.start_at.astimezone(zone(ev.timezone)).strftime("%A")
    freq = rule.get("FREQ")
    if freq == "WEEKLY":
        text = f"Every week on {day}" if n == 1 else f"Every {n} weeks on {day}"
    elif freq == "DAILY":
        text = "Every day" if n == 1 else f"Every {n} days"
    elif freq == "MONTHLY":
        text = "Every month" if n == 1 else f"Every {n} months"
    else:
        return ""
    if rule.get("COUNT") and ev.series_index is not None:
        text += f" · {ev.series_index + 1} of {rule['COUNT']}"
    return text


def color(cfg: Config, ev: Event) -> int:
    if ev.state in ("scheduled", "in_progress"):
        c = cfg["event_posts"]["faction_colors"].get(ev.faction or "any")
        return int(c.lstrip("#"), 16)
    return COLOR.get(ev.state, 0)


def event_message(s: Session, cfg: Config, ev: Event) -> dict:
    ed = cfg.edition(ev.edition)
    where = ed.name + (f" · {ed.ruleset_label(ev.ruleset)}" if ev.ruleset else "") + \
        (f" · {ev.realm_slug}" if ev.realm_slug else "") + (f" · {ev.faction.title()}" if ev.faction else "")
    desc = []
    if ev.state == "cancelled":
        desc.append("~~This event was cancelled.~~" + (f" {ev.cancelled_reason}" if ev.cancelled_reason else ""))
    if difficulty_line(ev):
        desc.append(f"**{difficulty_line(ev)}**")
    if ev.exclusive:
        desc.append("Exclusive: no other WoW events run at the same time.")
    if ev.description:
        desc.append(ev.description[:1500])
    if ev.objective:
        desc.append(f"**Objective**\n{ev.objective[:500]}")
    fields = [{"name": "Time", "value": f"{discord_ts(ev.start_at, 'F')} ({discord_ts(ev.start_at, 'R')})",
               "inline": False},
              {"name": "Duration", "value": duration_text(ev), "inline": False}]
    rep = repeat_text(s, ev)
    if rep:
        fields.append({"name": "Repeat", "value": rep, "inline": False})
    loc = (ev.activity_location or {}).get("label")
    mus = (ev.muster_location or {}).get("label")
    if loc:
        fields.append({"name": "Where", "value": loc[:1024], "inline": True})
    if mus:
        fields.append({"name": "Meet at", "value": mus[:1024], "inline": True})
    if ev.eligibility:
        fields.append({"name": "Who can join", "value": ev.eligibility[:1024], "inline": False})
    if ev.roles:
        for r in ev.roles:
            names, others = _who(s, ev, "confirmed", r.key)
            fields.append({"name": f"{r.label} ({len(names) + others}/{r.quantity})",
                           "value": _line(names, others)[:1024], "inline": True})
    else:
        names, others = _who(s, ev, "confirmed", None)
        cap = f"/{ev.participant_limit}" if ev.participant_limit else ""
        fields.append({"name": f"Going ({len(names) + others}{cap})", "value": _line(names, others)[:1024],
                       "inline": False})
    tent = _all(s, ev, "tentative")
    if tent[0] or tent[1]:
        fields.append({"name": f"Tentative ({len(tent[0]) + tent[1]})", "value": _line(*tent)[:1024],
                       "inline": False})
    extra = []
    for st, label in (("waitlisted", "Waitlist"), ("pending", "Awaiting approval")):
        n = s.scalar(select(func.count()).select_from(Signup).where(Signup.event_id == ev.id, Signup.state == st))
        if n:
            extra.append(f"{label}: {n}")
    if extra:
        fields.append({"name": "Also", "value": " · ".join(extra), "inline": False})
    policy = "Open signup" if ev.signup_policy == "open" else "Organizer approves signups"
    host = s.get(Character, ev.host_character_id) if ev.host_character_id else None
    foot = ([f"Hosted by {host.name}"] if host else []) + [STATE_LABEL.get(ev.state, ev.state), policy,
                                                           f"WoWQuester #{ev.id}"]
    embed = {"author": {"name": f"{ACTIVITY.get(ev.activity_type, 'Activity')} · {where}"[:256]},
             "title": ev.title[:256], "description": "\n\n".join(desc)[:4000], "color": color(cfg, ev),
             "fields": fields[:25], "footer": {"text": " · ".join(foot)[:2048]}}
    img = covers.url(cfg, ev, absolute=True)
    if img:
        embed["image"] = {"url": img}
    base = cfg["community"]["public_base_url"].rstrip("/") + cfg["web"]["member_path"].rstrip("/")
    link = {"type": 2, "style": 5, "label": "Details", "url": f"{base}/events/{ev.public_id}"}
    if ev.state in ("scheduled", "in_progress"):
        open_now = ev.state == "scheduled" or ev.late_join
        row = [{"type": 2, "style": 3, "label": "Join", "custom_id": cid("join", ev.id), "disabled": not open_now},
               {"type": 2, "style": 2, "label": "Tentative", "custom_id": cid("tentative", ev.id),
                "disabled": not open_now},
               {"type": 2, "style": 4, "label": "Leave", "custom_id": cid("leave", ev.id)},
               {"type": 2, "style": 1, "label": "Organizer", "custom_id": cid("manage", ev.id)},
               link]
        components = [{"type": 1, "components": row}]
    else:
        components = [{"type": 1, "components": [link]}]
    return {"content": "", "embeds": [embed], "components": components}
