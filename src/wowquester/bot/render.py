"""The canonical Discord post for an event: one message, edited in place on every change.

Shared by the worker (which posts/edits through REST) and the bot (ephemeral previews).
Buttons carry stable custom_ids "wq:ev:<action>:<event_id>" handled by persistent
DynamicItems, so they keep working after restarts and never embed user data.
Public posts show WoW character names only for people who consented for this event;
others are counted ("+2 others"). Discord IDs and display names are never rendered.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import Character, Event, Signup
from ..domain.timeutil import discord_ts

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


def _line(names: list[str], others: int) -> str:
    parts = names[:12]
    extra = others + max(0, len(names) - 12)
    if extra:
        parts.append(f"+{extra} other{'s' if extra != 1 else ''}")
    return ", ".join(parts) or "—"


def event_message(s: Session, cfg: Config, ev: Event) -> dict:
    ed = cfg.edition(ev.edition)
    where = ed.name + (f" · {ev.ruleset.upper()}" if ev.ruleset else "") + \
        (f" · {ev.realm_slug}" if ev.realm_slug else "") + (f" · {ev.faction.title()}" if ev.faction else "")
    desc = [f"**{ACTIVITY.get(ev.activity_type, 'Activity')}** · {where}",
            f"{discord_ts(ev.start_at, 'F')} → {discord_ts(ev.end_at, 't')} ({discord_ts(ev.start_at, 'R')})"]
    if ev.state == "cancelled":
        desc.insert(0, "~~This event was cancelled.~~" + (f" {ev.cancelled_reason}" if ev.cancelled_reason else ""))
    if ev.exclusive:
        desc.append("Exclusive: no other WoW events run at the same time.")
    if ev.description:
        desc.append("")
        desc.append(ev.description[:1500])
    fields = []
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
    counts = {st: s.scalar(select(Signup.id).where(Signup.event_id == ev.id, Signup.state == st).limit(1))
              for st in ("tentative", "waitlisted", "pending")}
    extra = []
    for st, label in (("tentative", "Tentative"), ("waitlisted", "Waitlist"), ("pending", "Awaiting approval")):
        if counts[st]:
            n = len(list(s.scalars(select(Signup.id).where(Signup.event_id == ev.id, Signup.state == st))))
            extra.append(f"{label}: {n}")
    if extra:
        fields.append({"name": "Also", "value": " · ".join(extra), "inline": False})
    policy = "Open signup" if ev.signup_policy == "open" else "Organizer approves signups"
    embed = {"title": ev.title[:256], "description": "\n".join(desc)[:4000], "color": COLOR.get(ev.state, 0),
             "fields": fields[:25],
             "footer": {"text": f"{STATE_LABEL.get(ev.state, ev.state)} · {policy} · WoWQuester #{ev.id}"}}
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
