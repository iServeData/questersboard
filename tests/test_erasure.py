"""Remove me: one irreversible step that withdraws, cancels, anonymises and unlinks."""
from __future__ import annotations

import pytest
from conftest import actor, at, needs_db
from helpers import char, event
from sqlalchemy import text

from questersboard.domain import chronicle, erasure, events, signups
from questersboard.domain.errors import NotFound
from questersboard.models import ChronicleEntry, Member

pytestmark = needs_db
ORG = actor(1, "event_creator")
REV = actor(2, "chronicle_reviewer")
ME = actor(10, "event_creator", name="LeavingDiscord")
OTHER = actor(11)
WAIT = actor(12)


def _mid(s, a):
    return s.execute(text("select id from members where discord_id=:d"), {"d": a.discord_id}).scalar()


def test_phrase():
    assert erasure.phrase_ok("remove me") and erasure.phrase_ok('  "Remove   ME" ')
    assert not erasure.phrase_ok("") and not erasure.phrase_ok("remove") and not erasure.phrase_ok("delete me")


def test_erase_withdraws_cancels_anonymises_and_unlinks(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        # a finished event with a published story that names ME's character
        past = event(s, cfg, ORG, 0, 2, roles=[{"key": "healer", "label": "Healer", "quantity": 2}])
        healer = dict(race_key="tauren", class_key="druid", eligible_roles=["healer"], preferred_role="healer")
        mine = char(s, cfg, ME, "Brightmoon", **healer)
        signups.join(s, cfg, ME, past.id, mine.id, role_key="healer", recognition=True, note="bring snacks")
        monkeypatch.setattr(events, "now", lambda: at(1))
        events.start(s, cfg, ORG, past.id)
        events.end(s, cfg, ORG, past.id)
        events.record_attendance(s, cfg, ORG, past.id, {_mid(s, ME): "attended"})
        e = s.query(ChronicleEntry).filter_by(event_id=past.id).one()
        chronicle.save(s, cfg, ORG, e.id, {"attempted": "Clear Wailing Caverns.", "outcome": "completed",
                                           "happened": "Brightmoon kept us alive.", "moments": "Brightmoon!"})
        chronicle.submit(s, cfg, ORG, e.id)
        chronicle.review(s, cfg, REV, e.id, "approve")
        assert "Brightmoon" in s.execute(text("select body_html from public_chronicle")).scalar()
        monkeypatch.undo()

        # an upcoming event ME joined (full, with someone waiting), one ME hosts, a draft, a spare character
        up = event(s, cfg, ORG, 48, 2, roles=[{"key": "tank", "quantity": 1}])
        signups.join(s, cfg, ME, up.id, char(s, cfg, ME, "Grimtusk").id, role_key="tank")
        waiting = signups.join(s, cfg, WAIT, up.id, char(s, cfg, WAIT, "Waiter").id, role_key="tank")
        assert waiting.state == "waitlisted"
        hosted = event(s, cfg, ME, 72, 2, title="My raid")
        signups.join(s, cfg, OTHER, hosted.id, char(s, cfg, OTHER, "Other").id)
        event(s, cfg, ME, 96, 2, publish=False, title="My draft")
        char(s, cfg, ME, "Unused")
        mid = _mid(s, ME)

        p = erasure.plan(s, mid)
        assert [x.title for x in p.leave] == ["Deadmines run"] and [x.title for x in p.cancel] == ["My raid"]
        assert p.drafts == 1 and p.characters == 3 and p.stories == 1

        r = erasure.erase(s, cfg, mid)
        assert r["left"] == ["Deadmines run"] and r["cancelled"] == ["My raid"] and r["drafts"] == 1
        assert r["characters_deleted"] == 1 and r["characters_blanked"] == 2 and r["stories"] == 1

        # the waitlisted player moved up, the hosted event is cancelled, the draft is gone
        assert s.execute(text("select state from signups where id=:i"), {"i": waiting.id}).scalar() == "confirmed"
        assert s.execute(text("select state from events where id=:i"), {"i": hosted.id}).scalar() == "cancelled"
        assert s.execute(text("select count(*) from events where title='My draft'")).scalar() == 0
        # names gone everywhere: public story, every saved revision, characters, member row, notes
        assert "Brightmoon" not in s.execute(text("select row_to_json(p)::text from public_chronicle p")).scalar()
        revs = s.execute(text("select string_agg(concat(title,attempted,happened,moments,body), ' ') "
                              "from chronicle_revisions")).scalar()
        assert "Brightmoon" not in revs and "a healer" in revs
        names = set(s.execute(text("select name from characters where member_id=:m"), {"m": mid}).scalars())
        assert names == {"(removed)"}
        m = s.get(Member, mid)
        assert m.discord_id == f"erased:{mid}" and m.display_name == "" and m.erased_at is not None
        assert s.execute(text("select count(*) from signups where member_id=:m and (note<>'' or recognition)"),
                         {"m": mid}).scalar() == 0
        assert s.execute(text("select count(*) from audit_log where action='member.erase'")).scalar() == 1
        assert s.execute(text("select count(*) from audit_log where action='member.erase' and "
                              "actor_discord_id=:d"), {"d": ME.discord_id}).scalar() == 0
        # the past event still adds up: one attendee
        assert s.execute(text("select count(*) from attendance where event_id=:e"), {"e": past.id}).scalar() == 1
        with pytest.raises(NotFound):
            erasure.erase(s, cfg, mid)
        with pytest.raises(NotFound):
            erasure.find_member_id(s, ME.discord_id)
