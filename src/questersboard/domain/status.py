"""Public status for the community site's WoW cards (status.json).

Two inputs only, both safe to publish:
* service_heartbeats: the bot and worker check in every [status] heartbeat_seconds.
* public_events (+ public_event_voice): the same projection upcoming.json reads.

"Live" means an organiser launched the event (state in_progress). The clock alone never
makes an event live; once the start time passes without a launch the phase is "waiting".
Nothing here ends an event: past its planned end a launched event is "running_over".
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import Config
from ..models import PublicEvent, PublicEventVoice, ServiceHeartbeat
from .timeutil import now

SCHEMA_VERSION = 1


def beat(s: Session, name: str, state: str, started_at: datetime) -> None:
    """Record a check-in. started_at is the process start, so a restart is visible."""
    t = now()
    s.execute(insert(ServiceHeartbeat).values(name=name, state=state, beat_at=t, started_at=started_at)
              .on_conflict_do_update(index_elements=[ServiceHeartbeat.name],
                                     set_={"state": state, "beat_at": t, "started_at": started_at}))


def phase(pe: PublicEvent, at: datetime, soon_minutes: int) -> str:
    if pe.state == "in_progress":
        return "running_over" if at > pe.end_at else "live"
    if at >= pe.start_at:
        return "waiting"                        # start time passed, organiser hasn't launched yet
    if at >= pe.start_at - timedelta(minutes=soon_minutes):
        return "starting_soon"
    return "scheduled"


def _needs(roles: list) -> list[dict]:
    out = []
    for r in roles or []:
        q, f = r.get("quantity"), int(r.get("filled") or 0)
        if isinstance(q, int) and q > f:
            out.append({"label": r.get("label", ""), "missing": q - f})
    return out


def _service(hb: ServiceHeartbeat | None, at: datetime, stale: int) -> tuple[str, bool]:
    if hb is None:
        return "unknown", False
    fresh = timedelta(seconds=-60) <= at - hb.beat_at <= timedelta(seconds=stale)
    return ("running" if fresh else "stale"), fresh


def _cal_links(cfg, pe) -> dict:
    from .calendar import public_links
    return public_links(cfg, pe)


def snapshot(s: Session, cfg: Config) -> dict:
    st = cfg["status"]
    at = now()
    soon, stale, vstale = st["starting_soon_minutes"], st["stale_after_seconds"], st["voice_stale_after_seconds"]
    hbs = {h.name: h for h in s.scalars(select(ServiceHeartbeat))}

    def iso(d):
        return d.isoformat() if d else None

    bot_state, bot_fresh = _service(hbs.get("bot"), at, stale)
    worker_state, _ = _service(hbs.get("worker"), at, stale)
    bot = hbs.get("bot")
    if bot is None or not bot_fresh:
        discord_state = "unknown"
    else:
        discord_state = "connected" if bot.state == "connected" else "disconnected"
    services = {
        "event_board": {"state": "responding", "observed_at": iso(at)},
        "bot": {"state": bot_state, "observed_at": iso(bot.beat_at) if bot else None},
        "discord": {"state": discord_state, "observed_at": iso(bot.beat_at) if bot else None},
        "worker": {"state": worker_state,
                   "observed_at": iso(hbs["worker"].beat_at) if "worker" in hbs else None},
    }

    # The focus event: a launched one first (even past its planned end), else the next scheduled one.
    live = list(s.scalars(select(PublicEvent).where(PublicEvent.state == "in_progress")
                          .order_by(PublicEvent.start_at)))
    pe = live[0] if live else s.scalars(
        select(PublicEvent).where(PublicEvent.state == "scheduled", PublicEvent.end_at > at)
        .order_by(PublicEvent.start_at).limit(1)).first()
    event = None
    if pe is not None:
        ph = phase(pe, at, soon)
        voice = None
        if ph in ("live", "running_over"):
            v = s.get(PublicEventVoice, pe.public_id)
            if v is not None and at - v.observed_at <= timedelta(seconds=vstale):
                voice = {"in_voice": v.in_voice, "observed_at": iso(v.observed_at)}
        roles = pe.roles or []
        caps = [r.get("quantity") for r in roles]
        event = dict(
            id=str(pe.public_id), title=pe.title, activity_type=pe.activity_type, edition=pe.edition_name,
            ruleset=(pe.ruleset or "").upper() or None, start=iso(pe.start_at), end=iso(pe.end_at),
            state=pe.state, phase=ph, roles=roles, needs=_needs(roles),
            signed_up=sum(int(r.get("filled") or 0) for r in roles),
            capacity=sum(caps) if caps and all(isinstance(c, int) for c in caps) else None,
            voice=voice,
            url=f"{cfg['community']['public_base_url'].rstrip('/')}"
                f"{cfg['web']['member_path'].rstrip('/')}/events/{pe.public_id}",
            **_cal_links(cfg, pe))
    return {"schema_version": SCHEMA_VERSION, "generated_at": iso(at),
            "starting_soon_minutes": soon,
            "expiry": {"public_json_seconds": max(180, 3 * st["cache_seconds"]), "heartbeat_seconds": stale,
                       "voice_seconds": vstale},
            "services": services, "live_count": len(live), "event": event}


def upcoming_filter(at: datetime):
    """upcoming.json: events not yet over, plus launched ones running past their planned end."""
    return or_(PublicEvent.end_at > at, PublicEvent.state == "in_progress")
