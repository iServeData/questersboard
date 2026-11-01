from __future__ import annotations

import io

import pytest
from conftest import actor, at, needs_db
from helpers import char, event
from PIL import Image
from sqlalchemy import text

from wowquester.domain import chronicle, events, signups
from wowquester.domain.errors import Invalid, PermissionDenied
from wowquester.models import ChronicleEntry

pytestmark = needs_db
ORG = actor(1, "event_creator")
REV = actor(2, "chronicle_reviewer")
TRUSTED = actor(3, "event_creator", "trusted_author")
YES = actor(10, name="YesDiscord")
NO = actor(11, name="NoDiscord")


def _finished(s, cfg, monkeypatch, org=ORG):
    ev = event(s, cfg, org, 0, 2, roles=[{"key": "healer", "label": "Healer", "quantity": 2}])
    cy = char(s, cfg, YES, "Brightmoon", race_key="tauren", class_key="druid", eligible_roles=["healer"],
              preferred_role="healer")
    cn = char(s, cfg, NO, "Shadowhide", race_key="tauren", class_key="druid", eligible_roles=["healer"],
              preferred_role="healer")
    signups.join(s, cfg, YES, ev.id, cy.id, role_key="healer", recognition=True)
    signups.join(s, cfg, NO, ev.id, cn.id, role_key="healer", recognition=False)
    monkeypatch.setattr(events, "now", lambda: at(1))
    events.start(s, cfg, org, ev.id)
    events.end(s, cfg, org, ev.id)
    ids = dict(s.execute(text("select discord_id, id from members")).all())
    events.record_attendance(s, cfg, org, ev.id, {ids[YES.discord_id]: "attended", ids[NO.discord_id]: "attended"})
    return ev, s.query(ChronicleEntry).filter_by(event_id=ev.id).one()


GOOD = {"attempted": "Clear Wailing Caverns.", "happened": "Brightmoon kept us alive; our other healer "
        "saved the pull.", "outcome": "completed", "moments": "Mutanus fell on the first try."}


def test_end_creates_draft_and_attendance_starts_unknown(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2)
        a = actor(10)
        signups.join(s, cfg, a, ev.id, char(s, cfg, a, "Aaa").id)
        monkeypatch.setattr(events, "now", lambda: at(1))
        events.start(s, cfg, ORG, ev.id)
        events.end(s, cfg, ORG, ev.id)
        assert s.execute(text("select status from attendance")).scalar() == "unknown"
        e = s.query(ChronicleEntry).one()
        assert e.state == "draft" and chronicle.working(s, e).outcome == "unknown"
        assert s.execute(text("select count(*) from event_reservations")).scalar() == 0


def test_outcome_never_inferred_and_privacy_scan(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev, e = _finished(s, cfg, monkeypatch)
        chronicle.save(s, cfg, ORG, e.id, {"attempted": "x", "happened": "y"})
        with pytest.raises(Invalid, match="outcome"):
            chronicle.submit(s, cfg, ORG, e.id)
        for bad in ("Shadowhide tanked", "thanks NoDiscord", "ping <@123>", "id 100000000000000011"):  # sanitize: fixture
            chronicle.save(s, cfg, ORG, e.id, {**GOOD, "happened": bad})
            with pytest.raises(Invalid):
                chronicle.submit(s, cfg, ORG, e.id)


def test_review_publish_projection_is_privacy_safe(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev, e = _finished(s, cfg, monkeypatch)
        chronicle.save(s, cfg, ORG, e.id, GOOD)
        chronicle.submit(s, cfg, ORG, e.id)
        assert e.state == "pending_review"
        with pytest.raises(PermissionDenied):
            chronicle.review(s, cfg, ORG, e.id, "approve")
        chronicle.review(s, cfg, REV, e.id, "approve")
        row = s.execute(text("select row_to_json(p)::text from public_chronicle p")).scalar()
        assert "Brightmoon" in row and "a healer" in row
        for leak in ("Shadowhide", "NoDiscord", "YesDiscord", NO.discord_id, YES.discord_id):
            assert leak not in row


def test_material_edit_keeps_old_version_until_approved(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev, e = _finished(s, cfg, monkeypatch)
        chronicle.save(s, cfg, ORG, e.id, GOOD)
        chronicle.submit(s, cfg, ORG, e.id)
        chronicle.review(s, cfg, REV, e.id, "approve")
        chronicle.save(s, cfg, ORG, e.id, {"moments": "NEW MOMENT"})
        chronicle.submit(s, cfg, ORG, e.id)
        body = s.execute(text("select body_html from public_chronicle")).scalar()
        assert "Mutanus" in body and "NEW MOMENT" not in body
        chronicle.review(s, cfg, REV, e.id, "changes_requested", "tone it down")
        assert e.state == "changes_requested"
        chronicle.save(s, cfg, ORG, e.id, {"moments": "Newer moment"})
        assert e.state == "edited"
        chronicle.submit(s, cfg, ORG, e.id)
        chronicle.review(s, cfg, REV, e.id, "approve")
        body = s.execute(text("select body_html from public_chronicle")).scalar()
        assert "Newer moment" in body
        assert len(chronicle.history(s, e.id)) == 3


def test_trusted_author_self_publishes_but_is_still_checked(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev, e = _finished(s, cfg, monkeypatch, org=TRUSTED)
        chronicle.save(s, cfg, TRUSTED, e.id, {**GOOD, "happened": "Shadowhide was great"})
        with pytest.raises(Invalid):
            chronicle.submit(s, cfg, TRUSTED, e.id)
        chronicle.save(s, cfg, TRUSTED, e.id, GOOD)
        chronicle.submit(s, cfg, TRUSTED, e.id)
        assert e.state == "published"
        assert s.execute(text("select count(*) from audit_log where action='chronicle.self_publish'")).scalar() == 1


def test_recognition_removal_applies_immediately(seeded, cfg, monkeypatch, tmp_path):
    with seeded.tx() as s:
        ev, e = _finished(s, cfg, monkeypatch)
        chronicle.save(s, cfg, ORG, e.id, GOOD)
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), "red").save(buf, "JPEG", exif=b"Exif\x00\x00GPSDATA")
        u = chronicle.add_upload(s, cfg, ORG, e.id, buf.getvalue(), "the pull")
        chronicle.clear_upload(s, cfg, REV, u.id)
        chronicle.submit(s, cfg, ORG, e.id)
        chronicle.review(s, cfg, REV, e.id, "approve")
        assert "media" in s.execute(text("select images::text from public_chronicle")).scalar()
        chronicle.remove_recognition(s, cfg, YES, ev.id)
        row = s.execute(text("select row_to_json(p)::text from public_chronicle p")).scalar()
        assert "Brightmoon" not in row and '"images": []' in row.replace('"images":[]', '"images": []')
        stored = (tmp_path / u.filename).read_bytes()
        assert b"GPSDATA" not in stored and Image.open(io.BytesIO(stored)).format == "WEBP"


def test_bad_upload_rejected(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev, e = _finished(s, cfg, monkeypatch)
        with pytest.raises(Invalid):
            chronicle.add_upload(s, cfg, ORG, e.id, b"<svg onload=alert(1)>", "")
        with pytest.raises(Invalid):
            chronicle.add_upload(s, cfg, ORG, e.id, b"GIF89a" + b"0" * 100, "")


def test_rendering_is_safe(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev, e = _finished(s, cfg, monkeypatch)
        chronicle.save(s, cfg, ORG, e.id, {**GOOD, "body": "<script>alert(1)</script> [x](javascript:alert(1)) "
                                           "<img src=x onerror=alert(1)> **ok**"})
        chronicle.submit(s, cfg, ORG, e.id)
        chronicle.review(s, cfg, REV, e.id, "approve")
        body = s.execute(text("select body_html from public_chronicle")).scalar()
        assert "<script" not in body and 'href="javascript' not in body and "<img" not in body
        assert "&lt;script&gt;" in body and "<strong>ok</strong>" in body



def test_cohost_is_not_coauthor_until_assigned(seeded, cfg, monkeypatch):
    CO = actor(4, "event_creator")
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2)
        events.update(s, cfg, ORG, ev.id, {"cohosts": [CO.discord_id]})
        monkeypatch.setattr(events, "now", lambda: at(1))
        events.start(s, cfg, CO, ev.id)                                # co-host may run the event...
        events.end(s, cfg, CO, ev.id)
        e = s.query(ChronicleEntry).filter_by(event_id=ev.id).one()
        with pytest.raises(PermissionDenied):
            chronicle.save(s, cfg, CO, e.id, GOOD)                     # ...but isn't an author by default
        with pytest.raises(PermissionDenied):
            chronicle.set_coauthors(s, cfg, CO, e.id, [CO.discord_id])  # only the author/editor assigns
        chronicle.set_coauthors(s, cfg, ORG, e.id, [CO.discord_id])
        chronicle.save(s, cfg, CO, e.id, GOOD)                         # now a co-author may edit
