"""Public projections: the only data public routes, feeds and the readonly role can see.

Refreshed inside the same transaction as the change that affects them, so a story or
an event is public exactly when its source row says so. No member IDs, Discord IDs,
display names or unconsented character names are ever written here.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import Config
from ..models import (ChronicleEntry, ChronicleRevision, ChronicleUpload, Event, PublicChronicle, PublicEvent,
                      Signup)
from . import privacy, render
from .timeutil import now


def refresh_event(s: Session, cfg: Config, ev: Event) -> None:
    if ev.state not in ("scheduled", "in_progress") or not ev.public_listing:
        s.execute(delete(PublicEvent).where(PublicEvent.public_id == ev.public_id))
        return
    counts = dict(s.execute(select(Signup.role_key, func.count()).where(
        Signup.event_id == ev.id, Signup.state == "confirmed").group_by(Signup.role_key)).all())
    roles = [{"label": r.label, "filled": int(counts.get(r.key, 0)), "quantity": r.quantity} for r in ev.roles]
    if not roles:
        total = sum(int(v) for v in counts.values())
        roles = [{"label": "Players", "filled": total, "quantity": ev.participant_limit}]
    ed = cfg.edition(ev.edition)
    vals = dict(public_id=ev.public_id, title=ev.title, activity_type=ev.activity_type, edition_name=ed.name,
                ruleset=ev.ruleset, start_at=ev.start_at, end_at=ev.end_at, timezone=ev.timezone, state=ev.state,
                location_label=(ev.activity_location or {}).get("label", ""),
                muster_label=(ev.muster_location or {}).get("label", ""), roles=roles,
                signup_policy=ev.signup_policy, exclusive=ev.exclusive, updated_at=now())
    s.execute(insert(PublicEvent).values(**vals).on_conflict_do_update(
        index_elements=[PublicEvent.public_id], set_={k: v for k, v in vals.items() if k != "public_id"}))


def refresh_chronicle(s: Session, cfg: Config, entry: ChronicleEntry) -> None:
    if entry.state == "unpublished" or entry.published_revision_id is None:
        s.execute(delete(PublicChronicle).where(PublicChronicle.public_id == entry.public_id))
        return
    rev = s.get(ChronicleRevision, entry.published_revision_id)
    ev = s.get(Event, entry.event_id)
    roles = {r.key: r.label for r in ev.roles}
    people = privacy.participants(s, cfg, ev.id, roles)        # attended: the public "who was there" list
    everyone = privacy.involved(s, cfg, ev.id, roles)          # anyone connected: redaction safety net
    body_html, summary = render.story_html(cfg, rev, everyone)
    imgs = [{"url": f"{cfg['chronicle']['public_path'].rstrip('/')}/media/{u.public_id}.{_ext(u.mime)}",
             "caption": privacy.redact(u.caption, everyone, cfg), "width": u.width, "height": u.height}
            for u in s.scalars(select(ChronicleUpload).where(ChronicleUpload.entry_id == entry.id,
                                                             ChronicleUpload.cleared_at.is_not(None),
                                                             ChronicleUpload.removed_at.is_(None))
                               .order_by(ChronicleUpload.id))]
    plist = [{"label": p.public_label(cfg), "named": p.consent,
              "role": p.role_label or "", "class": p.class_key if p.consent else ""} for p in people]
    vals = dict(public_id=entry.public_id, slug=entry.slug, title=privacy.redact(rev.title, everyone, cfg),
                summary=summary, body_html=body_html, activity_type=ev.activity_type,
                edition_name=cfg.edition(ev.edition).name, event_date=ev.actual_start or ev.start_at,
                outcome=rev.outcome, participants=plist, images=imgs, ai_assisted=rev.ai_assisted,
                published_at=entry.first_published_at or now(), updated_at=now())
    s.execute(insert(PublicChronicle).values(**vals).on_conflict_do_update(
        index_elements=[PublicChronicle.public_id], set_={k: v for k, v in vals.items() if k != "public_id"}))


def _ext(mime: str) -> str:
    return {"image/webp": "webp", "image/png": "png", "image/jpeg": "jpg"}.get(mime, "bin")
