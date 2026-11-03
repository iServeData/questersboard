"""Public projections: the only data public routes, feeds and the readonly role can see.

Refreshed inside the same transaction as the change that affects them, so a story or
an event is public exactly when its source row says so. No member IDs, Discord IDs,
display names or unconsented character names are ever written here.
"""

from __future__ import annotations

from html import escape

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import Config
from ..models import (ChronicleEntry, ChronicleRevision, ChronicleUpload, Event, EventCohost, PublicChronicle, PublicEvent,
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
    """One public page per event. Every published story is on it: the host's first, then co-hosts,
    then everyone else in the order they were published. The page uses the first (host's) entry's id/slug."""
    stories = list(s.scalars(select(ChronicleEntry).where(ChronicleEntry.event_id == entry.event_id)
                             .order_by(ChronicleEntry.id)))
    if not stories:
        return
    anchor = stories[0]
    live = [e for e in stories if e.state != "unpublished" and e.published_revision_id is not None]
    stale = [e.public_id for e in stories if e.public_id != anchor.public_id]
    if stale:                                     # older single-story rows keyed on another entry
        s.execute(delete(PublicChronicle).where(PublicChronicle.public_id.in_(stale)))
    if not live:
        s.execute(delete(PublicChronicle).where(PublicChronicle.public_id == anchor.public_id))
        return
    ev = s.get(Event, entry.event_id)
    cohosts = list(s.scalars(select(EventCohost.member_id).where(EventCohost.event_id == ev.id)
                             .order_by(EventCohost.member_id)))
    def rank(e):
        if e.author_id == ev.creator_id:
            return (0, 0, 0)
        if e.author_id in cohosts:
            return (1, cohosts.index(e.author_id), 0)
        return (2, 0, (e.first_published_at or now()).timestamp())
    live.sort(key=rank)
    roles = {r.key: r.label for r in ev.roles}
    people = privacy.participants(s, cfg, ev.id, roles)        # attended: the public "who was there" list
    everyone = privacy.involved(s, cfg, ev.id, roles)          # anyone connected: redaction safety net
    revs = [(e, s.get(ChronicleRevision, e.published_revision_id)) for e in live]
    outcome = next((r.outcome for _, r in revs if r.outcome != "unknown"), "unknown")
    parts = []
    for e, rev in revs:
        html_, _ = render.story_html(cfg, rev, everyone, with_outcome=rev.outcome != outcome)
        tag = "host" if e.author_id == ev.creator_id else ("co-host" if e.author_id in cohosts else "")
        who, named = _author_label(cfg, e.author_id, people, everyone, tag)
        parts.append(f'<section class="wq-tale"><h2 class="wq-story-by">{escape(who)}'
                     + (f' <span class="note">· {tag}</span>' if tag and named else "")
                     + f"</h2>{html_}</section>")
    lead_e, lead_rev = revs[0]
    _, summary = render.story_html(cfg, lead_rev, everyone)
    title_rev = s.get(ChronicleRevision, anchor.published_revision_id) if anchor in live else lead_rev
    imgs = [{"url": f"{cfg['chronicle']['public_path'].rstrip('/')}/media/{u.public_id}.{_ext(u.mime)}",
             "caption": privacy.redact(u.caption, everyone, cfg), "width": u.width, "height": u.height}
            for u in s.scalars(select(ChronicleUpload).where(ChronicleUpload.entry_id.in_([e.id for e in live]),
                                                             ChronicleUpload.cleared_at.is_not(None),
                                                             ChronicleUpload.removed_at.is_(None))
                               .order_by(ChronicleUpload.id))]
    plist = [{"label": p.public_label(cfg), "named": p.consent,
              "role": p.role_label or "", "class": p.class_key if p.consent else ""} for p in people]
    vals = dict(public_id=anchor.public_id, slug=anchor.slug, title=privacy.redact(title_rev.title, everyone, cfg),
                summary=summary, body_html="".join(parts), activity_type=ev.activity_type,
                edition_name=cfg.edition(ev.edition).name, event_date=ev.actual_start or ev.start_at,
                outcome=outcome, participants=plist, images=imgs,
                ai_assisted=any(r.ai_assisted for _, r in revs),
                published_at=min((e.first_published_at or now()) for e in live), updated_at=now())
    s.execute(insert(PublicChronicle).values(**vals).on_conflict_do_update(
        index_elements=[PublicChronicle.public_id], set_={k: v for k, v in vals.items() if k != "public_id"}))


def _author_label(cfg: Config, member_id: int, people, everyone, tag: str) -> tuple[str, bool]:
    """Privacy-aware byline: the author's character name if they agreed to be named, else neutral."""
    mine = [p for p in people if p.member_id == member_id] or [p for p in everyone if p.member_id == member_id]
    if mine:
        return "Told by " + mine[0].public_label(cfg), True
    return "Told by the " + (tag or "storyteller"), False


def _ext(mime: str) -> str:
    return {"image/webp": "webp", "image/png": "png", "image/jpeg": "jpg"}.get(mime, "bin")
