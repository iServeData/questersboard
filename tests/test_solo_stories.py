"""Solo adventure stories ([solo_stories]): a member writes up something they did alone. Discord is a FIXTURE
(tests/fake_discord.py); nothing live is exercised."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import ROLE, actor, at, make_cfg, needs_db
from fake_discord import FakeDiscord
from helpers import char, event
from sqlalchemy import select, text

from questersboard.config import ConfigError, ConfigHolder
from questersboard.domain import calendar, chronicle, erasure, events, recommendations, roster, signups, solo
from questersboard.domain.errors import Invalid, InvalidTransition, NotFound, PermissionDenied
from questersboard.models import (ChronicleEntry, ChronicleInterest, Event, Member, PublicChronicle, PublicEvent,
                                  Signup)
from questersboard.worker import Worker

pytestmark = needs_db
AUTHOR = actor(20, name="FixtureAuthor")
AUTHOR_EC = actor(20, "event_creator", name="FixtureAuthor")   # the same member, allowed to publish events
READER = actor(21, name="FixtureReader")
REV = actor(2, "chronicle_reviewer")
ORG = actor(1, "event_creator")
TODAY = date.today()
GOOD = {"attempted": "Finish the long road to the coast.", "happened": "FixtureWanderer walked the whole way "
        "and only died twice.", "outcome": "completed", "moments": "The view from the cliffs."}


def _start(s, cfg, who=AUTHOR, name="Fixture Wanderer", named=True, day=None, title="The long walk"):
    c = char(s, cfg, who, name.replace(" ", ""))
    return solo.start(s, cfg, who, c.id, title, day or TODAY - timedelta(days=1), "quest", named=named,
                      tz="America/Los_Angeles"), c


def _publish(s, cfg, e, fields=None):
    chronicle.save(s, cfg, AUTHOR, e.id, fields or GOOD)
    chronicle.submit(s, cfg, AUTHOR, e.id)
    chronicle.review(s, cfg, REV, e.id, "approve")
    return s.get(PublicChronicle, e.public_id)


def test_start_makes_private_completed_record(seeded, cfg):
    with seeded.tx() as s:
        e, c = _start(s, cfg)
        ev = s.get(Event, e.event_id)
        assert ev.kind == "solo" and ev.state == "completed" and not ev.public_listing
        assert ev.participant_limit == 1 and ev.host_character_id == c.id
        me, them = roster.ensure_member(s, AUTHOR), roster.ensure_member(s, READER)
        assert chronicle.story_eligible(s, cfg, me, ev) and not chronicle.story_eligible(s, cfg, them, ev)
        assert chronicle.coauthor_choices(s, cfg, e) == []
        # never listed: no public event row, no calendar entries, no recommendations
        assert s.scalar(select(PublicEvent).where(PublicEvent.public_id == ev.public_id)) is None
        mid = e.author_id
        assert not any(ev.title in str(i) for i in calendar.personal_items(s, cfg, mid))
        assert not any(ev.title in str(x) for x in recommendations.for_member(s, cfg, mid))
        assert not any(ev.title in str(i) for i in calendar.public_items(s, cfg))
        assert "Say what you set out to do." in chronicle.check(s, cfg, e, chronicle.working(s, e))


def test_start_validates_inputs(seeded, cfg):
    with seeded.tx() as s:
        c = char(s, cfg, AUTHOR, "Fixturewalker")
        other = char(s, cfg, READER, "Fixtureother")
        with pytest.raises(Invalid):
            solo.start(s, cfg, AUTHOR, other.id, "Not mine", TODAY, "quest")
        with pytest.raises(Invalid):
            solo.start(s, cfg, AUTHOR, c.id, "x", TODAY, "quest")
        with pytest.raises(Invalid):
            solo.start(s, cfg, AUTHOR, c.id, "Tomorrow", TODAY + timedelta(days=3), "quest")
        with pytest.raises(Invalid):
            solo.start(s, cfg, AUTHOR, c.id, "Ancient", TODAY - timedelta(days=400), "quest")
        with pytest.raises(Invalid):
            solo.start(s, cfg, AUTHOR, c.id, "Odd kind", TODAY, "bogus")


def test_weekly_cap_roles_and_switch(seeded, tmp_path):
    cfg = make_cfg(tmp_path, solo_stories={"per_week": 2})
    with seeded.tx() as s:
        c = char(s, cfg, AUTHOR, "Fixturecap")
        for i in range(2):
            solo.start(s, cfg, AUTHOR, c.id, f"Story {i}", TODAY, "quest")
        assert solo.left_this_week(s, cfg, s.scalar(select(Member.id).where(
            Member.discord_id == AUTHOR.discord_id))) == 0
        with pytest.raises(Invalid):
            solo.start(s, cfg, AUTHOR, c.id, "One too many", TODAY, "quest")
    only = make_cfg(tmp_path, solo_stories={"role_ids": [ROLE["trusted_author"]]})
    assert not only.solo_allowed(frozenset()) and only.solo_allowed(frozenset({ROLE["trusted_author"]}))
    off = make_cfg(tmp_path, solo_stories={"enabled": False})
    with seeded.tx() as s:
        c = char(s, off, READER, "Fixtureoff")
        with pytest.raises(PermissionDenied):
            solo.start(s, off, READER, c.id, "Turned off", TODAY, "quest")


def test_config_validation(tmp_path):
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, solo_stories={"per_week": 99})
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, solo_stories={"enabled": "yes"})
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, solo_stories={"surprise": 1})
    assert make_cfg(tmp_path)["solo_stories"]["in_feeds"] is False


def test_review_publish_and_naming(seeded, cfg):
    with seeded.tx() as s:
        e, c = _start(s, cfg, named=False)
        with pytest.raises(Invalid):                        # opted out, so the text can't name the character
            _publish(s, cfg, e)
        pc = _publish(s, cfg, e, {**GOOD, "happened": "Our adventurer walked the whole way."})
        assert pc.kind == "solo"
        assert not any("FixtureWanderer" in p["label"] for p in pc.participants)
        solo.set_named(s, cfg, AUTHOR, e.id, True)
        s.refresh(pc)
        assert any("Fixture" in p["label"] for p in pc.participants)
        with pytest.raises(PermissionDenied):
            solo.set_named(s, cfg, READER, e.id, False)
        with pytest.raises(InvalidTransition):
            solo.discard(s, cfg, AUTHOR, e.id)


def test_join_me_plan_and_publish_notify(seeded, worker_db, cfg):
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        e, _ = _start(s, cfg)
        _publish(s, cfg, e)
        pid, eid = str(e.public_id), e.id
        with pytest.raises(Invalid):
            solo.join_me(s, cfg, AUTHOR, pid)
        solo.join_me(s, cfg, READER, pid)
        solo.join_me(s, cfg, READER, pid)                 # twice is still once
        assert solo.is_interested(s, eid, s.scalar(select(Member.id).where(Member.discord_id == READER.discord_id)))
        assert [n for _, n in solo.interested(s, eid)] == ["FixtureReader"]
    for _ in range(10):
        if not w.tick():
            break
    assert sum("would like to join you next time" in c for _, c in fake.dms) == 1
    fake.dms.clear()
    with seeded.tx() as s:
        with pytest.raises(PermissionDenied):
            solo.plan_event(s, cfg, READER, eid, at(24), at(26), "America/Los_Angeles")
        ev = solo.plan_event(s, cfg, AUTHOR, eid, at(24), at(26), "America/Los_Angeles")
        assert ev.state == "draft" and ev.kind == "event" and ev.public_listing
        assert solo.solo_source(s, ev) is not None
        events.publish(s, cfg, AUTHOR_EC, ev.id)
    for _ in range(10):
        if not w.tick():
            break
    hits = [c for _, c in fake.dms if "You asked to join next time" in c]
    assert len(hits) == 1 and "/events/" in hits[0]
    for _, c in fake.dms:
        assert "webhook" not in c.lower() and "outbox" not in c.lower()


def test_join_me_withdraw_and_switch(seeded, tmp_path):
    cfg = make_cfg(tmp_path)
    with seeded.tx() as s:
        e, _ = _start(s, cfg)
        with pytest.raises(NotFound):                      # not published yet
            solo.join_me(s, cfg, READER, str(e.public_id))
        _publish(s, cfg, e)
        solo.join_me(s, cfg, READER, str(e.public_id))
        solo.join_me(s, cfg, READER, str(e.public_id), want=False)
        assert solo.interested(s, e.id) == []
    off = make_cfg(tmp_path, solo_stories={"join_me": False})
    with seeded.tx() as s:
        with pytest.raises(PermissionDenied):
            solo.join_me(s, off, READER, str(e.public_id))


def test_discard_unpublished(seeded, cfg):
    with seeded.tx() as s:
        e, _ = _start(s, cfg)
        evid, eid = e.event_id, e.id
        with pytest.raises(PermissionDenied):
            solo.discard(s, cfg, READER, eid)
        solo.discard(s, cfg, AUTHOR, eid)
        assert s.get(ChronicleEntry, eid) is None and s.get(Event, evid) is None
        assert s.scalar(select(Signup).where(Signup.event_id == evid)) is None


def test_erasure_removes_interest(seeded, cfg):
    with seeded.tx() as s:
        e, _ = _start(s, cfg)
        _publish(s, cfg, e)
        solo.join_me(s, cfg, READER, str(e.public_id))
        rid = s.scalar(select(Member.id).where(Member.discord_id == READER.discord_id))
    with seeded.tx() as s:
        erasure.erase(s, cfg, rid)
        assert s.scalar(select(ChronicleInterest).where(ChronicleInterest.member_id == rid)) is None


def test_archive_character_after_finished_event(seeded, cfg, monkeypatch):
    """Archiving a character that took part in a finished event (or a solo story) no longer errors."""
    with seeded.tx() as s:
        e, c = _start(s, cfg)
        ev = event(s, cfg, ORG, 0, 2)
        signups.join(s, cfg, AUTHOR, ev.id, c.id)
        monkeypatch.setattr(events, "now", lambda: at(1))
        events.start(s, cfg, ORG, ev.id)
        events.end(s, cfg, ORG, ev.id)
        roster.archive_character(s, cfg, AUTHOR, c.id)
        assert s.execute(text("select state from signups where event_id = :i"), {"i": ev.id}).scalar() != "withdrawn"


# ------------------------------------------------------------------ web pages (FIXTURE Discord login)
import test_web as tw  # noqa: E402  (FIXTURE OAuth/REST and the login/csrf helpers)
from questersboard.web.app import create_app  # noqa: E402

login, csrf = tw.login, tw.csrf


@pytest.fixture
def wcfg(tmp_path):
    return make_cfg(tmp_path, web={"cookie_secure": False})


@pytest.fixture
def web_app(seeded, wcfg):
    rest = tw.FakeRest()
    a = create_app(ConfigHolder(wcfg), seeded, tw.FakeOAuth(), rest)
    a.state.fake_rest = rest
    return a


def test_web_solo_flow(web_app, seeded, wcfg):
    app = web_app
    c = login(app, 20)
    with seeded.tx() as s:
        ch = char(s, wcfg, AUTHOR, "Fixtureweb")
        cid = ch.id
    r = c.get("/wow/quester/chronicle/solo")
    assert r.status_code == 200 and "Write a solo adventure" in r.text and "Fixtureweb" in r.text
    tok = csrf(c, "/wow/quester/chronicle/solo")
    r = c.post("/wow/quester/chronicle/solo", data={"csrf": tok, "character_id": cid, "title": "Fixture walk",
                                                    "day": TODAY.isoformat(), "activity_type": "quest",
                                                    "named": "1"})
    assert r.status_code == 303, r.text
    edit = r.headers["location"]
    page = c.get(edit)
    assert page.status_code == 200 and "Solo adventure" in page.text and "Co-authors" not in page.text
    assert "Delete this solo story" in page.text and "Plan a group event" in page.text
    with seeded.tx() as s:
        e = s.scalars(select(ChronicleEntry)).one()
        ev = s.get(Event, e.event_id)
        evpid = ev.public_id
    assert c.get(f"/wow/quester/events/{evpid}").status_code == 404       # the record is never an event page
    assert "Write a solo adventure" in c.get("/wow/quester/").text      # submenu + home button
    with seeded.tx() as s:
        pc = _publish(s, wcfg, s.get(ChronicleEntry, e.id), {**GOOD, "happened": "Fixtureweb walked far."})
        slug, pid = pc.slug, str(pc.public_id)
    pub = TestClient_public(app)
    arch = pub.get("/wow/chronicle/").text
    assert "Solo adventures" in arch and "All solo adventures" in arch
    assert "Fixture walk" in pub.get("/wow/chronicle/solo/").text
    story = pub.get(f"/wow/chronicle/{slug}/").text
    assert "Solo adventure" in story and "Adventurer" in story and f"/stories/{pid}/join" in story
    assert "Fixture walk" not in pub.get("/wow/chronicle/feed.json").text   # feeds: group stories only
    assert "Fixture walk" not in pub.get("/wow/chronicle/latest.json").text
    assert slug in pub.get("/wow/chronicle/sitemap.xml").text
    r2 = login(app, 21)
    jp = r2.get(f"/wow/quester/stories/{pid}/join")
    assert jp.status_code == 200 and "I'd like to come along" in jp.text
    tok = csrf(r2, f"/wow/quester/stories/{pid}/join")
    assert r2.post(f"/wow/quester/stories/{pid}/join", data={"csrf": tok, "want": "1"}).status_code == 303
    assert "You're on the list" in r2.get(f"/wow/quester/stories/{pid}/join").text
    page = c.get(edit).text
    assert "1 person would like to come along" in page, page[page.find("Join me next time"):][:600]


def TestClient_public(a):
    from fastapi.testclient import TestClient
    return TestClient(a, base_url="https://community.example.org", follow_redirects=False)


def test_web_discard_and_plan(web_app, seeded, wcfg):
    app = web_app
    c = login(app, 20, "event_creator")
    with seeded.tx() as s:
        cid = char(s, wcfg, AUTHOR, "Fixtureplan").id
    tok = csrf(c, "/wow/quester/chronicle/solo")
    edit = c.post("/wow/quester/chronicle/solo", data={"csrf": tok, "character_id": cid, "title": "Fixture plan",
                                                       "day": TODAY.isoformat(), "activity_type": "dungeon"}
                  ).headers["location"]
    tok = csrf(c, edit)
    r = c.post(edit + "/plan", data={"csrf": tok, "date": (TODAY + timedelta(days=7)).isoformat(),
                                     "start_time": "19:00", "minutes": "120", "timezone": "America/Los_Angeles"})
    assert r.status_code == 303 and r.headers["location"].endswith("/edit")
    r = c.post(edit + "/discard", data={"csrf": tok})
    assert r.status_code == 303 and r.headers["location"] == "/wow/quester/chronicle/solo"
    with seeded.tx() as s:
        assert s.scalar(select(Event).where(Event.kind == "solo")) is None
        assert s.scalar(select(Event).where(Event.title == "Fixture plan", Event.kind == "event")) is not None
