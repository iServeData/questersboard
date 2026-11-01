"""Worker + outbox against a FIXTURE Discord (tests/fake_discord.py). Live Discord is not exercised."""
from __future__ import annotations

from datetime import timedelta

from conftest import actor, at, make_cfg, needs_db
from fake_discord import FakeDiscord
from helpers import char, event
from sqlalchemy import text

from wowquester.config import ConfigHolder
from wowquester.domain import events, signups
from wowquester.worker import Worker

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
    import wowquester.worker as wm
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
    import wowquester.worker as wm
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
