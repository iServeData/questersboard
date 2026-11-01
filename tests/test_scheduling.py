"""Community-wide capacity, exclusivity and member-level conflicts."""
from __future__ import annotations

import threading

import pytest
from conftest import actor, at, make_cfg, needs_db
from helpers import char, event
from sqlalchemy import text

from wowquester.domain import events, scheduling, signups
from wowquester.domain.errors import Conflict, InvalidTransition, PermissionDenied

pytestmark = needs_db
A = actor(1, "event_creator")
B = actor(2, "event_creator")
C = actor(3, "event_creator", "exclusive_designator")
P = actor(4)


def test_default_limit_one_blocks_overlap(seeded, cfg):
    with seeded.tx() as s:
        event(s, cfg, A, 0, 2)
    with pytest.raises(Conflict) as e, seeded.tx() as s:
        event(s, cfg, B, 1, 2)
    assert e.value.detail["reason"] == "capacity"


def test_back_to_back_allowed_half_open(seeded, cfg):
    with seeded.tx() as s:
        event(s, cfg, A, 0, 2)
        event(s, cfg, B, 2, 2)


def test_drafts_reserve_nothing(seeded, cfg):
    with seeded.tx() as s:
        event(s, cfg, A, 0, 2, publish=False)
        event(s, cfg, B, 0, 2)


def test_limit_two_and_peak_sweep(seeded, cfg):
    cfg2 = make_cfg(scheduling={"max_simultaneous_wow_events": 2})
    with seeded.tx() as s:
        scheduling.sync_policy(s, 2)
        event(s, cfg2, A, 0, 2)          # [0,2)
        event(s, cfg2, B, 3, 2)          # [3,5)
        # [1,4) overlaps both, but never more than 2 at once -> allowed
        event(s, cfg2, actor(5, "event_creator"), 1, 3)
    with pytest.raises(Conflict) as e, seeded.tx() as s:
        event(s, cfg2, actor(6, "event_creator"), 1.5, 0.5)   # 3 at t=1.5
    assert e.value.detail["reason"] == "capacity"


def test_exclusive_blocks_all_and_needs_role(seeded, cfg):
    cfg2 = make_cfg(scheduling={"max_simultaneous_wow_events": 3})
    with pytest.raises(PermissionDenied), seeded.tx() as s:
        event(s, cfg2, A, 0, 2, exclusive=True)
    with seeded.tx() as s:
        scheduling.sync_policy(s, 3)
        event(s, cfg2, C, 0, 2, exclusive=True)
    with pytest.raises(Conflict) as e, seeded.tx() as s:
        event(s, cfg2, B, 1, 2)
    assert e.value.detail["reason"] == "exclusive"


def test_member_conflict_across_characters(seeded, cfg):
    cfg2 = make_cfg(scheduling={"max_simultaneous_wow_events": 2})
    with seeded.tx() as s:
        scheduling.sync_policy(s, 2)
        e1 = event(s, cfg2, A, 0, 2)
        e2 = event(s, cfg2, B, 1, 2)
        c1 = char(s, cfg2, P, "Grommash")
        c2 = char(s, cfg2, P, "Thrallson", race_key="troll", class_key="mage", eligible_roles=["damage"],
                  preferred_role="damage")
        signups.join(s, cfg2, P, e1.id, c1.id)
        ids = (e2.id, c2.id)
    with pytest.raises(Conflict) as e, seeded.tx() as s:
        signups.join(s, cfg2, P, *ids)
    assert e.value.detail["reason"] == "member"


def test_tentative_and_pending_do_not_reserve(seeded, cfg):
    cfg2 = make_cfg(scheduling={"max_simultaneous_wow_events": 2})
    with seeded.tx() as s:
        scheduling.sync_policy(s, 2)
        e1 = event(s, cfg2, A, 0, 2, signup_policy="approval")
        e2 = event(s, cfg2, B, 1, 2)
        e3 = event(s, cfg2, actor(7, "event_creator"), 4, 2, signup_policy="approval")
        c1 = char(s, cfg2, P, "Grommash")
        su = signups.join(s, cfg2, P, e1.id, c1.id)        # pending
        assert su.state == "pending"
        signups.join(s, cfg2, P, e3.id, c1.id, tentative=True)
        su2 = signups.join(s, cfg2, P, e2.id, c1.id)       # confirmed: pending above doesn't block
        assert su2.state == "confirmed"
        pend_id = su.id
    # approving the pending one now must recheck and fail
    with pytest.raises(Conflict), seeded.tx() as s:
        signups.approve(s, cfg2, A, pend_id)


def test_completion_and_cancel_release(seeded, cfg):
    with seeded.tx() as s:
        e1 = event(s, cfg, A, 0, 2)
        events.cancel(s, cfg, A, e1.id, "weather")
        e2 = event(s, cfg, B, 0, 2)
        n = s.execute(text("select count(*) from event_reservations")).scalar()
        assert n == 1 and e2.state == "scheduled"


def test_only_managers_start_end_cancel(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        e1 = event(s, cfg, A, 0, 2)
        eid = e1.id
    for fn in (events.start, events.cancel):
        with pytest.raises(PermissionDenied), seeded.tx() as s:
            fn(s, cfg, P, eid)
    with seeded.tx() as s:
        events.update(s, cfg, A, eid, {"cohosts": [actor(4).discord_id]})
    monkeypatch.setattr(events, "now", lambda: at(0))
    with seeded.tx() as s:
        events.start(s, cfg, actor(4), eid)       # co-host may start
    with pytest.raises(InvalidTransition), seeded.tx() as s:
        events.start(s, cfg, A, eid)


def test_concurrent_publish_only_one_wins(seeded, cfg):
    with seeded.tx() as s:
        drafts = [event(s, cfg, actor(20 + i, "event_creator"), 0, 2, publish=False).id for i in range(6)]
    results = []
    barrier = threading.Barrier(len(drafts))

    def go(i, eid):
        barrier.wait()
        try:
            with seeded.tx() as s:
                events.publish(s, cfg, actor(20 + i, "event_creator"), eid)
            results.append("ok")
        except Conflict:
            results.append("conflict")
    ts = [threading.Thread(target=go, args=(i, d)) for i, d in enumerate(drafts)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 1 and results.count("conflict") == 5


def test_concurrent_join_last_slot(seeded, cfg):
    with seeded.tx() as s:
        e1 = event(s, cfg, A, 0, 2, roles=[{"key": "tank", "quantity": 1}])
        players = []
        for i in range(8):
            a = actor(40 + i)
            players.append((a, char(s, cfg, a, f"Tank{chr(97 + i)}x").id))
        eid = e1.id
    out = []
    barrier = threading.Barrier(len(players))

    def go(a, cid):
        barrier.wait()
        with seeded.tx() as s:
            out.append(signups.join(s, cfg, a, eid, cid, role_key="tank").state)
    ts = [threading.Thread(target=go, args=p) for p in players]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count("confirmed") == 1 and out.count("waitlisted") == 7


def test_reschedule_requires_reconfirm_and_bumps_conflicts(seeded, cfg):
    cfg2 = make_cfg(scheduling={"max_simultaneous_wow_events": 2})
    with seeded.tx() as s:
        scheduling.sync_policy(s, 2)
        e1 = event(s, cfg2, A, 0, 2)
        e2 = event(s, cfg2, B, 5, 2)
        p1, p2 = actor(50), actor(51)
        c1, c2 = char(s, cfg2, p1, "Alpha"), char(s, cfg2, p2, "Bravo")
        signups.join(s, cfg2, p1, e1.id, c1.id)
        signups.join(s, cfg2, p2, e1.id, c2.id)
        signups.join(s, cfg2, p2, e2.id, c2.id)
        r = events.update(s, cfg2, A, e1.id, {"start_at": at(5), "end_at": at(7)})
        assert r["reconfirm"] and r["bumped"]
        states = dict(s.execute(text("select member_id, state from signups where event_id=:e"),
                                {"e": e1.id}).all())
        assert sorted(states.values()) == ["confirmed", "tentative"]
