"""Members without the event-creator role send drafts for publishing; moderators see and publish them.
Discord is a FIXTURE; names are made up."""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from conftest import actor, fake_id, make_cfg, needs_db
from fake_discord import FakeDiscord
from helpers import event
from sqlalchemy import select
from test_event_voice import _drain

from wowquester.config import ConfigHolder
from wowquester.domain import events
from wowquester.domain.timeutil import now
from wowquester.domain.errors import Invalid, InvalidTransition, PermissionDenied
from wowquester.models import AuditLog, Event
from wowquester.worker import Worker

pytestmark = needs_db
STAFF_CH, EVENT_CH = fake_id(88), fake_id(89)
MEMBER, MOD = actor(40, name="Zulmara"), actor(41, "event_creator", "event_moderator", name="Corwin")


def _cfg(**discord):
    return make_cfg(discord={"event_channel_id": EVENT_CH, "review_channel_id": STAFF_CH, **discord})


def test_request_then_moderator_publishes(seeded, worker_db):
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        ev = event(s, cfg, MEMBER, 24, publish=False, title="Continuing the Grind")
        with pytest.raises(PermissionDenied):
            events.publish(s, cfg, MEMBER, ev.id)                     # no role: can't publish
        events.request_publish(s, cfg, MEMBER, ev.id)
        events.request_publish(s, cfg, MEMBER, ev.id)                 # again: still one notice
        assert ev.publish_requested_at is not None and ev.state == "draft"
        with pytest.raises(PermissionDenied):
            events.request_publish(s, cfg, actor(42), ev.id)          # not the owner
        assert [e.id for e, m in events.awaiting_publish(s)] == [ev.id]
        eid = ev.id
    _drain(w)
    notes = [m["content"] for m in fake.messages.values() if "waiting to be published" in m["content"]]
    assert len(notes) == 1 and "Continuing the Grind" in notes[0] and "/wow/quester/events/" in notes[0]
    with seeded.tx() as s:
        events.publish(s, cfg, MOD, eid)
        ev = s.get(Event, eid)
        assert ev.state == "scheduled" and ev.publish_requested_at is None and ev.creator_id != MOD.member_id
        assert not events.awaiting_publish(s)
    _drain(w)
    assert any(u == MEMBER.discord_id and "was published by a moderator" in t for u, t in fake.dms)


def test_request_rules(seeded, worker_db):
    cfg, fake = _cfg(publish_request_channel_id=fake_id(87)), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        ev = event(s, cfg, MEMBER, 24, publish=False)
        events.request_publish(s, cfg, MEMBER, ev.id)
        events.withdraw_publish_request(s, cfg, MEMBER, ev.id)
        assert ev.publish_requested_at is None and not events.awaiting_publish(s)
        events.request_publish(s, cfg, MEMBER, ev.id)
        ev.start_at, ev.end_at = now() - timedelta(hours=2), now() - timedelta(hours=1)   # passed while waiting
        s.flush()
        with pytest.raises(Invalid, match="past"):
            events.publish(s, cfg, MOD, ev.id)
        mine = event(s, cfg, MOD, 30, publish=False)
        events.request_publish(s, cfg, MOD, mine.id)                  # has the role: just publishes
        assert mine.state == "scheduled"
        with pytest.raises(InvalidTransition):
            events.request_publish(s, cfg, MOD, mine.id)
        acts = set(s.scalars(select(AuditLog.action).where(AuditLog.action.like("event.publish%"))))
        assert {"event.publish_request", "event.publish_withdraw", "event.publish"} <= acts
    _drain(w)
    chans = {k for k, m in fake.messages.items() if "waiting to be published" in m["content"]}
    assert chans                                                     # posted to the configured channel


def test_web_send_for_publishing_and_moderator_list(seeded):
    from fastapi.testclient import TestClient  # noqa: F401
    from test_web import FakeOAuth, FakeRest, csrf, login

    from wowquester.web.app import create_app
    cfg = _cfg()
    cfg.raw["web"]["cookie_secure"] = False
    rest = FakeRest()
    app = create_app(ConfigHolder(cfg), seeded, FakeOAuth(), rest)
    app.state.fake_rest = rest
    with seeded.tx() as s:
        ev = event(s, cfg, actor(40), 24, publish=False, title="Continuing the Grind")
        pid = str(ev.public_id)
    m = login(app, 40)
    page = m.get(f"/wow/quester/events/{pid}").text
    assert "Send for publishing" in page and f"/events/{pid}/publish\"" not in page
    form = m.get(f"/wow/quester/events/{pid}/edit").text
    assert "Save and send for publishing" in form and "Save and publish" not in form
    r = m.post(f"/wow/quester/events/{pid}/request_publish", data={"csrf": csrf(m, f"/wow/quester/events/{pid}")})
    assert r.status_code == 303
    page = m.get(f"/wow/quester/events/{pid}").text
    assert "Sent for publishing." in page and "Withdraw request" in page
    assert "sent for publishing" in m.get("/wow/quester/").text
    mod = login(app, 41, "event_creator", "event_moderator")
    home = mod.get("/wow/quester/").text
    assert "Waiting to be published" in home and "Continuing the Grind" in home
    page = mod.get(f"/wow/quester/events/{pid}").text
    assert re.search(r"Sent for publishing by \w+", page) and f"/events/{pid}/publish\"" in page
    r = mod.post(f"/wow/quester/events/{pid}/publish", data={"csrf": csrf(mod, f"/wow/quester/events/{pid}")})
    assert r.status_code == 303
    assert "Waiting to be published" not in mod.get("/wow/quester/").text
    other = login(app, 43, "event_creator")                            # role but not a moderator: no list
    assert "Waiting to be published" not in other.get("/wow/quester/").text


# ------------------------------------------------------------------ open publishing with probation
def _member(n, days, *caps):
    from dataclasses import replace
    a = actor(n, *caps)
    return replace(a, joined_at=None if days is None else now() - timedelta(days=days))


def test_everyone_publishes_after_probation():
    from wowquester.config import ConfigError
    from wowquester.domain import permissions as perm
    cfg = make_cfg()
    assert cfg["publishing"] == {"open_to_all": True, "probation_days": 7}
    assert perm.has(cfg, _member(50, 30), "event_creator")                 # older member: may publish
    assert not perm.has(cfg, _member(50, 2), "event_creator")              # new: probation
    assert not perm.has(cfg, _member(50, None), "event_creator")           # unknown join date: probation
    assert perm.has(cfg, _member(50, 2, "event_creator"), "event_creator") # the role always may
    assert not perm.has(cfg, _member(50, 30), "event_moderator")           # only publishing opens up
    assert perm.has(make_cfg(publishing={"probation_days": 0}), _member(50, 0), "event_creator")
    assert not perm.has(make_cfg(publishing={"open_to_all": False}), _member(50, 30), "event_creator")
    assert "less than 7 days" in perm.probation_message(cfg) and "approval first" in perm.probation_message(cfg)
    for bad in ({"open_to_all": "yes"}, {"probation_days": -1}, {"probation": 3}):
        with pytest.raises(ConfigError):
            make_cfg(publishing=bad)


def test_new_member_is_told_and_can_send_for_publishing(seeded):
    cfg = _cfg()
    new, old = _member(52, 3), _member(53, 40)
    with seeded.tx() as s:
        ev = event(s, cfg, new, 24, publish=False)
        with pytest.raises(PermissionDenied, match="approval first"):
            events.publish(s, cfg, new, ev.id)
        events.request_publish(s, cfg, new, ev.id)
        assert ev.state == "draft" and ev.publish_requested_at is not None
        mine = event(s, cfg, old, 30, publish=False)
        events.publish(s, cfg, old, mine.id)                               # no role needed any more
        assert mine.state == "scheduled"


def test_web_probation_message_and_open_publish(seeded):
    from test_web import FakeOAuth, FakeRest, login

    from wowquester.web.app import create_app
    cfg = _cfg()
    cfg.raw["web"]["cookie_secure"] = False
    rest = FakeRest()
    rest.joined = {fake_id(54): now() - timedelta(days=2), fake_id(55): now() - timedelta(days=60)}
    app = create_app(ConfigHolder(cfg), seeded, FakeOAuth(), rest)
    app.state.fake_rest = rest
    new, old = login(app, 54), login(app, 55)
    form = new.get("/wow/quester/events/new").text
    assert "Save and send for publishing" in form and "approval first" in form
    form = old.get("/wow/quester/events/new").text
    assert "Save and publish" in form and "approval first" not in form
