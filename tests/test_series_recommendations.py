"""Recurring templates (edit one vs this-and-future) and explainable recommendations."""
from __future__ import annotations

from datetime import timedelta

from conftest import actor, needs_db
from helpers import char, event

from wowquester.domain import recommendations, recurrence
from wowquester.domain.timeutil import as_local, now
from wowquester.models import Event

pytestmark = needs_db
ORG = actor(1, "event_creator")
TZ = "America/Los_Angeles"


def _series(s, cfg, weeks=4):
    start = as_local(now() + timedelta(days=3), TZ).replace(hour=19, minute=0, second=0, microsecond=0, tzinfo=None)
    tpl = dict(edition="forever", ruleset="pvp", faction="horde", title="Weekly dungeon", activity_type="dungeon")
    return recurrence.create_series(s, cfg, ORG, tpl, f"FREQ=WEEKLY;COUNT={weeks}", start, TZ, 120)


def test_edit_one_detaches_and_edit_future_skips_detached(seeded, cfg):
    with seeded.tx() as s:
        ser, evs, _ = _series(s, cfg)
        assert len(evs) == 4 and all(e.state == "draft" for e in evs)   # drafts: nothing reserved yet
        ids = [e.id for e in evs]
        recurrence.edit_occurrence(s, cfg, ORG, ids[2], {"title": "Special week"})
        res = recurrence.edit_future(s, cfg, ORG, ids[1], {"title": "Weekly dungeon v2"}, shift_local_time="20:30")
        assert [r[0] for r in res] == [ids[1], ids[3]]                   # detached #2 left alone
        got = {e.id: e for e in s.query(Event).filter(Event.id.in_(ids))}
        assert got[ids[0]].title == "Weekly dungeon"                    # earlier occurrence untouched
        assert got[ids[2]].title == "Special week" and got[ids[2]].series_detached
        for i in (1, 3):
            loc = as_local(got[ids[i]].start_at, TZ)
            assert got[ids[i]].title == "Weekly dungeon v2" and (loc.hour, loc.minute) == (20, 30)
            assert got[ids[i]].end_at - got[ids[i]].start_at == timedelta(hours=2)
        out = recurrence.publish_series(s, cfg, ORG, ser.id)
        assert [r[1] for r in out] == ["scheduled"] * 4                 # a week apart: no overlap with limit 1


def test_recommendations_explain_fit_and_skip_mismatches(seeded, cfg):
    me = actor(20)
    with seeded.tx() as s:
        char(s, cfg, me, "Grom")                                          # Forever PvP Horde tank/damage
        fit = event(s, cfg, ORG, 1, 2, title="Needs a tank", roles=[{"key": "tank", "quantity": 1}])
        event(s, cfg, ORG, 10, 2, title="Alliance only", faction="alliance")
        event(s, cfg, ORG, 20, 2, title="RP ruleset", ruleset="rp")
        from wowquester.models import Member
        member_id = s.query(Member.id).filter(Member.discord_id == me.discord_id).scalar()
        sugs = recommendations.for_member(s, cfg, member_id)
    titles = [x.title for x in sugs if x.kind == "event"]
    assert titles == ["Needs a tank"]                                    # faction/ruleset mismatches excluded
    why = " ".join(sugs[0].reasons)
    assert "Grom fits this event" in why and "1 more tank" in why        # every score is explained
