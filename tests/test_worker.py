"""Worker + outbox against a FIXTURE Discord (tests/fake_discord.py). Live Discord is not exercised."""
from __future__ import annotations

from datetime import timedelta

from conftest import actor, at, make_cfg, needs_db
from fake_discord import FakeDiscord
from helpers import char, event
from sqlalchemy import text

from questersboard.config import ConfigHolder
from questersboard.domain import events, signups
from questersboard.worker import Worker

pytestmark = needs_db
ORG = actor(1, "event_creator")


def _worker(worker_db, cfg, fake):
    return Worker(ConfigHolder(cfg), worker_db, fake.client())


def _drain(w, n=10):
    for _ in range(n):
        if not w.tick():
            break


def test_canonical_post_is_edited_and_reposted_when_deleted(seeded, worker_db, cfg):
    cfg = make_cfg(discord={"event_channel_id": str(10 ** 17 + 77)})
    fake = FakeDiscord()
    w = _worker(worker_db, cfg, fake)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2)
        eid = ev.id
    _drain(w)
    assert len(fake.messages) == 1
    first = next(iter(fake.messages))
    with seeded.tx() as s:
        a = actor(10)
        signups.join(s, cfg, a, eid, char(s, cfg, a, "Aaa").id)
    _drain(w)
    assert len(fake.messages) == 1 and ("PATCH", f"/channels/{10 ** 17 + 77}/messages/{first}") in fake.calls
    fake.messages.clear()                          # someone deleted the post
    with seeded.tx() as s:
        events.update(s, cfg, ORG, eid, {"description": "bring water"})
    _drain(w)
    assert len(fake.messages) == 1 and next(iter(fake.messages)) != first


def test_rate_limit_and_forbidden(seeded, worker_db, cfg):
    ch = str(10 ** 17 + 78)
    cfg = make_cfg(discord={"event_channel_id": ch})
    fake = FakeDiscord()
    fake.fail_next = [429, 503]
    w = _worker(worker_db, cfg, fake)
    with seeded.tx() as s:
        event(s, cfg, ORG, 0, 2)
    with seeded.tx() as s:
        s.execute(text("update outbox set run_after = now()"))
    for _ in range(3):
        w.tick()
        with seeded.tx() as s:
            s.execute(text("update outbox set run_after = now() where done_at is null"))
    assert len(fake.messages) == 1
    fake.forbid_channels.add(ch)
    with seeded.tx() as s:
        event(s, cfg, actor(2, "event_creator"), 5, 2)
    _drain(w)
    with seeded.tx() as s:
        dead = s.execute(text("select last_error from outbox where dead_at is not null")).scalar()
    assert "permissions" in dead


def test_reminders_once_and_rescheduled_again(seeded, worker_db, cfg, monkeypatch):
    fake = FakeDiscord()
    w = _worker(worker_db, cfg, fake)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2)
        a = actor(10)
        signups.join(s, cfg, a, ev.id, char(s, cfg, a, "Aaa").id)
        eid = ev.id
    import questersboard.worker as wm
    monkeypatch.setattr(wm, "now", lambda: at(-1))       # 60 minutes before start
    assert w.scan_reminders() == 2                       # participant + organizer
    assert w.scan_reminders() == 0                       # idempotent
    _drain(w)
    assert len(fake.dms) == 2
    monkeypatch.setattr(events, "now", lambda: at(-3))
    with seeded.tx() as s:
        events.update(s, cfg, ORG, eid, {"start_at": at(1), "end_at": at(3)})
    monkeypatch.setattr(wm, "now", lambda: at(0))
    assert w.scan_reminders() == 2                       # new start -> new reminders


def test_overrun_nudged_once_and_not_auto_extended(seeded, worker_db, cfg, monkeypatch):
    fake = FakeDiscord()
    w = _worker(worker_db, cfg, fake)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2)
        monkeypatch.setattr(events, "now", lambda: at(0))
        events.start(s, cfg, ORG, ev.id)
    import questersboard.worker as wm
    monkeypatch.setattr(wm, "now", lambda: at(3))
    assert w.scan_overruns() == 1 and w.scan_overruns() == 0
    _drain(w)
    assert any("past its planned end" in c for _, c in fake.dms)
    with seeded.tx() as s:
        assert s.execute(text("select upper(during) from event_reservations")).scalar() == at(2)


def test_coalescing_keeps_one_pending_job(seeded, cfg):
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2)
        for i in range(5):
            events.update(s, cfg, ORG, ev.id, {"description": f"v{i}"})
        n = s.execute(text("select count(*) from outbox where kind='discord.render_event' and done_at is null"
                           )).scalar()
    assert n == 1



def test_unstarted_event_is_asked_then_cancelled(seeded, worker_db, cfg, monkeypatch):
    """[scheduling] no_start_*: one "Has your event started?" message at +60 min (24-hour times plus
    UTC) from the worker, then the bot's close_unstarted cancels it at +120 min with the configured
    reason, and the host and signups are told."""
    import questersboard.worker as wm
    fake = FakeDiscord()
    w = _worker(worker_db, cfg, fake)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2)
        eid = ev.id
        a = actor(11)
        signups.join(s, cfg, a, eid, char(s, cfg, a, "Fixture Joiner").id)
    _drain(w)
    fake.dms.clear()
    monkeypatch.setattr(wm, "now", lambda: at(0) + timedelta(minutes=59))
    assert w.scan_unstarted() == 0
    monkeypatch.setattr(wm, "now", lambda: at(0) + timedelta(minutes=61))
    assert w.scan_unstarted() == 1 and w.scan_unstarted() == 0
    _drain(w)
    asks = [c for _, c in fake.dms if c.startswith("Has your event started?")]
    assert len(asks) == 1 and "UTC" in asks[0] and "cancelled and closed" in asks[0]
    assert " AM" not in asks[0] and " PM" not in asks[0]
    with seeded.tx() as s:
        assert events.close_unstarted(s, cfg, at(0) + timedelta(minutes=119)) == 0
        assert events.close_unstarted(s, cfg, at(0) + timedelta(minutes=121)) == 1
    with seeded.tx() as s:
        assert events.close_unstarted(s, cfg, at(0) + timedelta(minutes=122)) == 0
        st, why = s.execute(text("select state, cancelled_reason from events where id = :i"), {"i": eid}).one()
        assert (st, why) == ("cancelled", "Didn't start")
        assert s.execute(text("select count(*) from event_reservations where event_id = :i"),
                         {"i": eid}).scalar() == 0
    monkeypatch.setattr(wm, "now", lambda: at(0) + timedelta(minutes=125))
    assert w.scan_unstarted() == 0
    _drain(w)
    told = [c for _, c in fake.dms if "was cancelled" in c]
    assert len(told) == 2 and all("wasn't started within 2 hours" in c for c in told)


def test_started_event_is_left_alone_and_old_ones_close_quietly(seeded, worker_db, cfg, monkeypatch):
    import questersboard.worker as wm
    fake = FakeDiscord()
    w = _worker(worker_db, cfg, fake)
    with seeded.tx() as s:
        running = event(s, cfg, ORG, 0, 2)
        monkeypatch.setattr(events, "now", lambda: at(0))
        events.start(s, cfg, ORG, running.id)
        old = event(s, cfg, ORG, 3, 4)
        rid, oid = running.id, old.id
    monkeypatch.undo()
    _drain(w)
    fake.dms.clear()
    monkeypatch.setattr(wm, "now", lambda: at(3) + timedelta(hours=30))
    assert w.scan_unstarted() == 0                 # too late to ask
    with seeded.tx() as s:
        assert events.close_unstarted(s, cfg, at(3) + timedelta(hours=30)) == 1
    _drain(w)
    assert fake.dms == []
    with seeded.tx() as s:
        got = dict(s.execute(text("select id, state from events where id in (:a, :b)"), {"a": rid, "b": oid}).all())
        assert got == {rid: "in_progress", oid: "cancelled"}


def test_unstarted_settings_can_turn_it_off(seeded, worker_db, monkeypatch):
    import questersboard.worker as wm
    cfg = make_cfg(scheduling={"no_start_ask_minutes": 0, "no_start_cancel_minutes": 0})
    fake = FakeDiscord()
    w = _worker(worker_db, cfg, fake)
    with seeded.tx() as s:
        event(s, cfg, ORG, 0, 2)
    monkeypatch.setattr(wm, "now", lambda: at(0) + timedelta(hours=50))
    assert w.scan_unstarted() == 0
    with seeded.tx() as s:
        assert events.close_unstarted(s, cfg, at(0) + timedelta(hours=50)) == 0
