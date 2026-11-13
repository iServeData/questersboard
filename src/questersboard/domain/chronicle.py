"""Chronicle entries and the review gate.

States: draft -> pending_review -> published
        pending_review -> changes_requested | rejected -> edited -> pending_review
        published -> edited (new revision) -> pending_review -> published (replaces old)
        published -> unpublished (reviewer, or privacy)

The public version is always `published_revision_id`; a newer working revision under
review never replaces it until approved. Outcomes are whatever the author selects;
nothing is inferred from event metadata (outcome starts as "unknown" and must be chosen).
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import unicodedata
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import (ChronicleCoauthor, ChronicleEntry, ChronicleRevision, ChronicleUpload, Event, Member,
                      RecognitionRemoval, Signup)
from . import audit, outbox, privacy, projections, render
from .actor import Actor
from .errors import Invalid, InvalidTransition, NotFound, PermissionDenied
from .permissions import has, require_member
from .roster import ensure_member
from .timeutil import now

OUTCOMES = ("completed", "partial", "not_completed", "social", "unknown")
EDITABLE_FROM = {"draft": "draft", "edited": "edited", "changes_requested": "edited", "rejected": "edited",
                 "published": "edited", "unpublished": "edited", "pending_review": "edited"}
MAX_PIXELS = 40_000_000


def _slugify(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:80] or "adventure"


def ensure_draft(s: Session, cfg: Config, ev: Event, author_id: int | None = None) -> ChronicleEntry:
    """One story per author per event. The host's story is created when the event ends."""
    author_id = author_id or ev.creator_id
    e = s.scalar(select(ChronicleEntry).where(ChronicleEntry.event_id == ev.id, ChronicleEntry.author_id == author_id))
    if e:
        return e
    base = f"{(ev.actual_start or ev.start_at):%Y-%m-%d}-{_slugify(ev.title)}"
    slug, n = base, 2
    while s.scalar(select(ChronicleEntry.id).where(ChronicleEntry.slug == slug)):
        slug, n = f"{base}-{n}", n + 1
    e = ChronicleEntry(event_id=ev.id, slug=slug, author_id=author_id, state="draft")
    s.add(e)
    s.flush()
    # The event's objective becomes "The plan"; the author edits it like any other answer.
    rev = ChronicleRevision(entry_id=e.id, number=1, title=ev.title, attempted=(ev.objective or "")[:2000],
                            outcome="unknown", created_by=author_id)
    s.add(rev)
    s.flush()
    e.working_revision_id = rev.id
    return e


def entries(s: Session, event_id: int) -> list[ChronicleEntry]:
    """All stories for an event, oldest first. The first one (the host's) anchors the public page."""
    return list(s.scalars(select(ChronicleEntry).where(ChronicleEntry.event_id == event_id)
                          .order_by(ChronicleEntry.id)))


def lead(s: Session, event_id: int) -> ChronicleEntry | None:
    return s.scalar(select(ChronicleEntry).where(ChronicleEntry.event_id == event_id)
                    .order_by(ChronicleEntry.id).limit(1))


def story_eligible(s: Session, cfg: Config, actor: Actor, ev: Event) -> bool:
    """Hosts, co-hosts and anyone who took part (confirmed/tentative and not marked absent)."""
    if actor is None or actor.member_id is None or not ev.chronicle_enabled or ev.state != "completed":
        return False
    if ev.kind == "solo":                              # a solo story has exactly one author
        return actor.member_id == ev.creator_id
    from . import scheduling
    from .permissions import is_event_manager
    if is_event_manager(cfg, actor, ev.creator_id, scheduling.cohost_ids(s, ev.id)):
        return True
    from ..models import Attendance
    att = s.get(Attendance, (ev.id, actor.member_id))
    if att is not None:
        return att.status != "absent"
    return bool(s.scalar(select(Signup.id).where(Signup.event_id == ev.id, Signup.member_id == actor.member_id,
                                                 Signup.state.in_(("confirmed", "tentative"))).limit(1)))


def start_story(s: Session, cfg: Config, actor: Actor, event_id: int) -> ChronicleEntry:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    ev = s.get(Event, event_id)
    if ev is None:
        raise NotFound("Event not found.")
    if not story_eligible(s, cfg, actor, ev):
        raise PermissionDenied("Stories can be written by the hosts and by people who took part, once the event ends.")
    if lead(s, ev.id) is None:
        ensure_draft(s, cfg, ev)                       # the host's story always anchors the page
    e = ensure_draft(s, cfg, ev, actor.member_id)
    audit.record(s, actor, "chronicle.start", "chronicle", e.id)
    return e


def get(s: Session, entry_id: int, lock: bool = False) -> ChronicleEntry:
    stmt = select(ChronicleEntry).where(ChronicleEntry.id == entry_id)
    if lock:
        stmt = stmt.with_for_update()
    e = s.scalar(stmt)
    if e is None:
        raise NotFound("Chronicle entry not found.")
    return e


def coauthor_ids(s: Session, entry_id: int) -> set[int]:
    return set(s.scalars(select(ChronicleCoauthor.member_id).where(ChronicleCoauthor.entry_id == entry_id)))


def is_writer(s: Session, actor: Actor, e: ChronicleEntry) -> bool:
    return actor.member_id == e.author_id or actor.member_id in coauthor_ids(s, e.id)


def can_edit(s: Session, cfg: Config, actor: Actor, e: ChronicleEntry) -> bool:
    return is_writer(s, actor, e) or has(cfg, actor, "chronicle_editor")


def _require_edit(s, cfg, actor, e):
    if not can_edit(s, cfg, actor, e):
        raise PermissionDenied("Only the entry's author, a co-author or a chronicle editor can change it.")


def working(s: Session, e: ChronicleEntry) -> ChronicleRevision:
    return s.get(ChronicleRevision, e.working_revision_id)


def _people(s: Session, cfg: Config, e: ChronicleEntry) -> list[privacy.Participant]:
    ev = s.get(Event, e.event_id)
    return privacy.involved(s, cfg, ev.id, {r.key: r.label for r in ev.roles})


def save(s: Session, cfg: Config, actor: Actor, entry_id: int, fields: dict) -> ChronicleRevision:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    e = get(s, entry_id, lock=True)
    _require_edit(s, cfg, actor, e)
    rev = working(s, e)
    changed = _changes(rev, fields)
    if not changed:
        return rev                                     # nothing new: history doesn't grow
    ev = s.get(Event, e.event_id)
    blank = (not any((rev.attempted, rev.happened, rev.moments, rev.body)) and rev.outcome == "unknown"
             and rev.title == ev.title and rev.submitted_at is None)
    if not blank:                                      # every real save is a new version (v2, v3, ...)
        n = (s.scalar(select(func.max(ChronicleRevision.number)).where(ChronicleRevision.entry_id == e.id)) or 0) + 1
        rev = ChronicleRevision(entry_id=e.id, number=n, title=rev.title, attempted=rev.attempted,
                                happened=rev.happened, outcome=rev.outcome, moments=rev.moments, body=rev.body,
                                ai_assisted=rev.ai_assisted, created_by=actor.member_id)
        s.add(rev)
        s.flush()
        e.working_revision_id = rev.id
    limit = cfg["chronicle"].get("max_body_chars", 8000)
    if "title" in fields:
        t = str(fields["title"] or "").strip()
        if not 3 <= len(t) <= 140:
            raise Invalid("Title must be 3–140 characters.")
        rev.title = t
    for k in ("attempted", "happened", "moments", "body"):
        if k in fields:
            v = str(fields[k] or "")
            if len(v) > limit:
                raise Invalid(f"{k.title()} is too long (max {limit} characters).")
            setattr(rev, k, v)
    if "outcome" in fields:
        if fields["outcome"] not in OUTCOMES:
            raise Invalid("Pick an outcome.")
        rev.outcome = fields["outcome"]
    if fields.get("ai_assisted") is not None:
        rev.ai_assisted = bool(fields["ai_assisted"])
    rev.updated_at = now()
    e.state = EDITABLE_FROM[e.state]
    e.updated_at = now()
    rev.created_by = rev.created_by if blank else actor.member_id
    audit.record(s, actor, "chronicle.save", "chronicle", e.id, revision=rev.number, fields=changed)
    return rev


def _changes(rev: ChronicleRevision, fields: dict) -> list[str]:
    out = []
    for k in ("title", "attempted", "happened", "moments", "body", "outcome"):
        if k in fields and fields[k] is not None:
            new = str(fields[k]).strip() if k == "title" else str(fields[k])
            if new != (getattr(rev, k) or ""):
                out.append(k)
    if fields.get("ai_assisted") is not None and bool(fields["ai_assisted"]) != bool(rev.ai_assisted):
        out.append("ai_assisted")
    return out


def coauthor_choices(s: Session, cfg: Config, e: ChronicleEntry) -> list[tuple[int, str]]:
    """People who can be added: anyone connected to the event except the author. (member_id, character name)"""
    out: dict[int, str] = {}
    ev = s.get(Event, e.event_id)
    if ev.kind == "solo":
        return []
    for p in _people(s, cfg, e):
        if p.member_id != e.author_id and p.member_id not in out:
            out[p.member_id] = p.discord_name or p.character_name
    from . import scheduling
    for mid in scheduling.cohost_ids(s, ev.id) | {ev.creator_id}:
        if mid != e.author_id and mid not in out:
            m = s.get(Member, mid)
            if m is not None and not m.erased_at:
                out[mid] = m.display_name or (("@" + m.username) if m.username else "a host")
    return sorted(out.items(), key=lambda x: x[1].lower())


def set_coauthors(s: Session, cfg: Config, actor: Actor, entry_id: int, member_ids: list[int]) -> set[int]:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    e = get(s, entry_id, lock=True)
    if not (actor.member_id == e.author_id or has(cfg, actor, "chronicle_editor")):
        raise PermissionDenied("Only the entry's author or a chronicle editor can choose co-authors.")
    allowed = {mid for mid, _ in coauthor_choices(s, cfg, e)}
    ids = set()
    for mid in member_ids:
        mid = int(mid)
        if mid not in allowed:
            raise Invalid("Co-authors are picked from the people who were part of this event.")
        ids.add(mid)
    before = coauthor_ids(s, e.id)
    s.query(ChronicleCoauthor).filter(ChronicleCoauthor.entry_id == e.id).delete()
    for mid in ids:
        s.add(ChronicleCoauthor(entry_id=e.id, member_id=mid))
    added = sorted(ids - before)
    if added:
        outbox.enqueue(s, "notify.chronicle_coauthor", f"{e.id}:{now().timestamp()}",
                       {"entry_id": e.id, "member_ids": added})
    audit.record(s, actor, "chronicle.coauthors", "chronicle", e.id, count=len(ids),
                 added=len(added), removed=len(before - ids))
    return ids


def check(s: Session, cfg: Config, e: ChronicleEntry, rev: ChronicleRevision | None = None) -> list[str]:
    """Everything that blocks submission or publication, in plain language."""
    rev = rev or working(s, e)
    probs = []
    ev = s.get(Event, e.event_id)
    if e.author_id == ev.creator_id:
        if not rev.attempted.strip():
            probs.append("Say what you set out to do." if ev.kind == "solo" else "Say what the group set out to do.")
        if not rev.happened.strip():
            probs.append("Say what happened.")
        if rev.outcome == "unknown":
            probs.append("Choose the outcome. QuestersBoard never guesses it.")
    elif not (rev.happened.strip() or rev.body.strip()):
        probs.append("Say what happened from your side (or write your story).")
    people = _people(s, cfg, e)
    text = "\n".join([rev.title, rev.attempted, rev.happened, rev.moments, rev.body] +
                     [u.caption for u in s.scalars(select(ChronicleUpload).where(
                         ChronicleUpload.entry_id == e.id, ChronicleUpload.removed_at.is_(None)))])
    probs += privacy.scan(text, people)
    return probs


def submit(s: Session, cfg: Config, actor: Actor, entry_id: int) -> ChronicleEntry:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    e = get(s, entry_id, lock=True)
    _require_edit(s, cfg, actor, e)
    if e.state not in ("draft", "edited"):
        raise InvalidTransition("Only drafts and edited entries can be submitted.")
    rev = working(s, e)
    probs = check(s, cfg, e, rev)
    if probs:
        raise Invalid("Not ready yet: " + " ".join(probs), problems=probs)
    rev.submitted_at, rev.submitted_by = now(), actor.member_id
    if has(cfg, actor, "trusted_author") and is_writer(s, actor, e):
        rev.decision, rev.decided_by, rev.decided_at = "self_published", actor.member_id, now()
        _publish(s, cfg, actor, e, rev, self_published=True)
    else:
        e.state = "pending_review"
        outbox.enqueue(s, "notify.chronicle_review", str(e.id), {"entry_id": e.id})
        audit.record(s, actor, "chronicle.submit", "chronicle", e.id, revision=rev.number)
    e.updated_at = now()
    return e


def review(s: Session, cfg: Config, actor: Actor, entry_id: int, decision: str, note: str = "") -> ChronicleEntry:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    if not has(cfg, actor, "chronicle_reviewer"):
        raise PermissionDenied("Reviewing needs the chronicle reviewer role.")
    e = get(s, entry_id, lock=True)
    if is_writer(s, actor, e) and not has(cfg, actor, "trusted_author"):
        raise PermissionDenied("You can't review an entry you wrote. Another reviewer needs to do it.")
    if e.state != "pending_review":
        raise InvalidTransition("This entry isn't waiting for review.")
    if decision not in ("approve", "changes_requested", "rejected"):
        raise Invalid("Decision must be approve, changes_requested or rejected.")
    if decision != "approve" and not note.strip():
        raise Invalid("Tell the author what to change.")
    rev = working(s, e)
    rev.decided_by, rev.decided_at, rev.decision_note = actor.member_id, now(), note[:1000]
    if decision == "approve":
        probs = check(s, cfg, e, rev)   # consent can change while an entry waits
        if probs:
            raise Invalid("Can't publish yet: " + " ".join(probs), problems=probs)
        rev.decision = "approved"
        _publish(s, cfg, actor, e, rev, self_published=False)
    else:
        rev.decision = decision
        e.state = decision
        outbox.enqueue(s, "notify.chronicle_decision", f"{e.id}:{rev.number}",
                       {"entry_id": e.id, "decision": decision, "note": note[:500]})
        audit.record(s, actor, f"chronicle.{decision}", "chronicle", e.id, revision=rev.number)
    e.updated_at = now()
    return e


def _publish(s, cfg, actor, e, rev, self_published: bool):
    replaced = e.published_revision_id
    e.published_revision_id = rev.id
    e.state = "published"
    e.unpublished_reason = ""
    e.first_published_at = e.first_published_at or now()
    projections.refresh_chronicle(s, cfg, e)
    outbox.enqueue(s, "notify.chronicle_decision", f"{e.id}:{rev.number}", {"entry_id": e.id, "decision": "published"})
    audit.record(s, actor, "chronicle.self_publish" if self_published else "chronicle.publish", "chronicle", e.id,
                 revision=rev.number, replaced_revision=replaced)


def unpublish(s: Session, cfg: Config, actor: Actor, entry_id: int, reason: str) -> ChronicleEntry:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    if not has(cfg, actor, "chronicle_reviewer"):
        raise PermissionDenied("Unpublishing needs the chronicle reviewer role.")
    e = get(s, entry_id, lock=True)
    if e.published_revision_id is None:
        raise InvalidTransition("This entry isn't published.")
    if not reason.strip():
        raise Invalid("Give a reason; it's kept in the audit log.")
    e.state, e.unpublished_reason, e.updated_at = "unpublished", reason[:500], now()
    projections.refresh_chronicle(s, cfg, e)
    outbox.enqueue(s, "notify.chronicle_decision", f"{e.id}:unpublished:{now().timestamp()}",
                   {"entry_id": e.id, "decision": "unpublished", "note": reason[:500]})
    audit.record(s, actor, "chronicle.unpublish", "chronicle", e.id, reason=reason[:500])
    return e


def remove_recognition(s: Session, cfg: Config, actor: Actor, event_id: int) -> None:
    """A participant withdraws consent after the fact. Applied immediately, no review needed."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    took_part = s.scalar(select(Signup.id).where(Signup.event_id == event_id, Signup.member_id == actor.member_id)
                         .limit(1))
    if not took_part:
        raise NotFound("You weren't part of that event.")
    if not s.scalar(select(RecognitionRemoval.id).where(RecognitionRemoval.event_id == event_id,
                                                        RecognitionRemoval.member_id == actor.member_id)):
        s.add(RecognitionRemoval(member_id=actor.member_id, event_id=event_id, applied_at=now()))
    for su in s.scalars(select(Signup).where(Signup.event_id == event_id, Signup.member_id == actor.member_id)):
        su.recognition = False
    s.flush()
    for e in entries(s, event_id):
        # screenshots might show the name plate; hide them all until a reviewer re-clears them
        for u in s.scalars(select(ChronicleUpload).where(ChronicleUpload.entry_id == e.id,
                                                         ChronicleUpload.cleared_at.is_not(None))):
            u.cleared_at, u.cleared_by = None, None
        outbox.enqueue(s, "notify.chronicle_review", f"{e.id}:recognition", {"entry_id": e.id, "privacy": True})
    s.flush()
    e = lead(s, event_id)
    if e:
        projections.refresh_chronicle(s, cfg, e)
    audit.record(s, actor, "chronicle.recognition_removed", "event", event_id)


def refresh_if_published(s: Session, cfg: Config, event_id: int) -> None:
    e = lead(s, event_id)
    if e:
        projections.refresh_chronicle(s, cfg, e)


# --------------------------------------------------------------------------- screenshots

def add_upload(s: Session, cfg: Config, actor: Actor, entry_id: int, data: bytes, caption: str = "") -> ChronicleUpload:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    e = get(s, entry_id, lock=True)
    _require_edit(s, cfg, actor, e)
    up = cfg["uploads"]
    if len(data) > up.get("max_bytes", 5_000_000):
        raise Invalid("That image is too large.")
    count = s.scalar(select(func.count()).select_from(ChronicleUpload).where(
        ChronicleUpload.entry_id == e.id, ChronicleUpload.removed_at.is_(None))) or 0
    if count >= up.get("max_per_entry", 6):
        raise Invalid("This entry already has the maximum number of screenshots.")
    allowed = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
    allowed = {v for k, v in allowed.items() if k in up.get("allowed_types", list(allowed))}
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            if fmt not in allowed:
                raise Invalid("Screenshots must be PNG, JPEG or WebP.")
            if im.width * im.height > MAX_PIXELS:
                raise Invalid("That image has too many pixels.")
            im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
            im.thumbnail((2560, 2560))
            out = io.BytesIO()
            im.save(out, "WEBP", quality=85, method=4)   # re-encode: strips EXIF/GPS and any trailing payload
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError):
        raise Invalid("That file isn't a readable image.") from None
    blob = out.getvalue()
    u = ChronicleUpload(entry_id=e.id, filename="", mime="image/webp", width=im.width, height=im.height,
                        sha256=hashlib.sha256(blob).hexdigest(), caption=str(caption)[:200],
                        uploaded_by=actor.member_id)
    s.add(u)
    s.flush()
    u.filename = f"{u.public_id}.webp"
    d = Path(up["storage_dir"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{u.filename}.tmp"
    tmp.write_bytes(blob)
    os.chmod(tmp, 0o640)
    tmp.replace(d / u.filename)
    audit.record(s, actor, "chronicle.upload", "chronicle", e.id, upload=str(u.public_id))
    return u


def clear_upload(s: Session, cfg: Config, actor: Actor, upload_id: int, cleared: bool = True) -> ChronicleUpload:
    """Confirms the screenshot shows nobody who hasn't consented (name plates, chat, Discord overlays)."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    u = s.get(ChronicleUpload, upload_id)
    if u is None or u.removed_at:
        raise NotFound("Screenshot not found.")
    e = get(s, u.entry_id, lock=True)
    trusted_own = has(cfg, actor, "trusted_author") and is_writer(s, actor, e)
    if not (has(cfg, actor, "chronicle_reviewer") or trusted_own):
        raise PermissionDenied("Clearing screenshots needs the chronicle reviewer role.")
    u.cleared_at, u.cleared_by = (now(), actor.member_id) if cleared else (None, None)
    if e.published_revision_id:
        projections.refresh_chronicle(s, cfg, e)
    audit.record(s, actor, "chronicle.clear_upload" if cleared else "chronicle.unclear_upload", "chronicle", e.id,
                 upload=str(u.public_id))
    return u


def remove_upload(s: Session, cfg: Config, actor: Actor, upload_id: int) -> None:
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    u = s.get(ChronicleUpload, upload_id)
    if u is None or u.removed_at:
        raise NotFound("Screenshot not found.")
    e = get(s, u.entry_id, lock=True)
    if not (can_edit(s, cfg, actor, e) or has(cfg, actor, "chronicle_reviewer")):
        raise PermissionDenied("You can't remove that screenshot.")
    u.removed_at = now()
    try:
        (Path(cfg["uploads"]["storage_dir"]) / u.filename).unlink(missing_ok=True)
    except OSError:
        pass
    if e.published_revision_id:
        projections.refresh_chronicle(s, cfg, e)
    audit.record(s, actor, "chronicle.remove_upload", "chronicle", e.id, upload=str(u.public_id))


def set_banner(s: Session, cfg: Config, actor: Actor, entry_id: int, upload_id: int | None) -> None:
    """Use one of this story's screenshots as the page banner (None = back to the event's cover art).

    Only cleared screenshots appear publicly, so an uncleared banner shows the event art until it's cleared.
    """
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    e = get(s, entry_id, lock=True)
    _require_edit(s, cfg, actor, e)
    if upload_id is not None:
        u = s.get(ChronicleUpload, upload_id)
        if u is None or u.removed_at or u.entry_id != e.id:
            raise NotFound("Screenshot not found.")
    e.banner_upload_id = upload_id
    if e.published_revision_id:
        projections.refresh_chronicle(s, cfg, e)
    audit.record(s, actor, "chronicle.banner", "chronicle", e.id, upload=upload_id)


# --------------------------------------------------------------------------- AI wording

def ai_suggest(s: Session, cfg: Config, actor: Actor, entry_id: int, client=None) -> tuple[ChronicleRevision, str]:
    """Ask the configured AI provider to polish the author's own notes. Draft only; review still applies.

    Returns (revision, source) where source is "ai" or "template" (fallback).
    """
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    e = get(s, entry_id, lock=True)
    _require_edit(s, cfg, actor, e)
    rev = working(s, e)
    people = _people(s, cfg, e)
    from ..integrations import ai
    text, source = ai.suggest_story(cfg, rev, people, client=client)
    rev = save(s, cfg, actor, entry_id, {"body": text, "ai_assisted": source == "ai"})
    audit.record(s, actor, "chronicle.ai_suggest", "chronicle", e.id, source=source)
    return rev, source


def history(s: Session, entry_id: int) -> list[ChronicleRevision]:
    return list(s.scalars(select(ChronicleRevision).where(ChronicleRevision.entry_id == entry_id)
                          .order_by(ChronicleRevision.number)))


__all__ = ["ensure_draft", "entries", "lead", "start_story", "story_eligible", "coauthor_choices", "save", "submit", "review", "unpublish", "remove_recognition", "add_upload",
           "clear_upload", "remove_upload", "ai_suggest", "history", "check", "render"]
