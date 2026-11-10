"""Web side of the host feedback batch (FIXTURE OAuth/REST from test_web)."""
from __future__ import annotations

import re

from conftest import actor, at, needs_db
from helpers import char, event
from sqlalchemy import select, text
from test_web import app, csrf, login, wcfg  # noqa: F401  (fixtures)

from questersboard.domain import chronicle, events, signups
from questersboard.models import ChronicleEntry, EventCohost, Member

pytestmark = needs_db
ORG = actor(1, "event_creator", name="HostOne")
P1 = actor(10, name="PlayerOne")
P2 = actor(11, name="PlayerTwo")
ROLES = [{"key": "tank", "label": "Tank", "quantity": 1}, {"key": "damage", "label": "Damage", "quantity": 3}]


def _ended(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2, roles=ROLES)
        signups.join(s, cfg, P1, ev.id, char(s, cfg, P1, "Aaa").id, role_key="tank", recognition=True)
        signups.join(s, cfg, P2, ev.id, char(s, cfg, P2, "Bbb").id, role_key="damage", recognition=True)
        monkeypatch.setattr(events, "now", lambda: at(1))
        events.start(s, cfg, ORG, ev.id)
        events.end(s, cfg, ORG, ev.id)
        ids = dict(s.execute(text("select discord_id, id from members")).all())
        events.record_attendance(s, cfg, ORG, ev.id, {ids[P1.discord_id]: "attended", ids[P2.discord_id]: "absent"})
        return ev.public_id, ids


def test_attendance_menu_shows_saved_value(app, seeded, wcfg, monkeypatch):  # noqa: F811
    pid, ids = _ended(seeded, wcfg, monkeypatch)
    c = login(app, 1, "event_creator")
    page = c.get(f"/wow/quester/events/{pid}").text
    sel = re.search(rf'name="att_{ids[P1.discord_id]}">(.*?)</select>', page, re.S).group(1)
    assert '<option value="attended" selected>' in sel
    sel = re.search(rf'name="att_{ids[P2.discord_id]}">(.*?)</select>', page, re.S).group(1)
    assert '<option value="absent" selected>' in sel


def test_role_counts_on_signup_form(app, seeded, wcfg):  # noqa: F811
    with seeded.tx() as s:
        ev = event(s, wcfg, ORG, 30, 2, roles=ROLES)
        signups.join(s, wcfg, P1, ev.id, char(s, wcfg, P1, "Tanky").id, role_key="tank")
        pid = ev.public_id
    c = login(app, 12)
    with seeded.tx() as s:
        char(s, wcfg, actor(12), "Newbie")
    page = c.get(f"/wow/quester/events/{pid}").text
    assert "Tank · 1 of 1 taken (full, you&#39;d be waitlisted)" in page or "Tank · 1 of 1 taken (full, you'd be waitlisted)" in page
    assert "Damage · 0 of 3 taken" in page and "Open spots: Tank 0 · Damage 3" in page
    assert '<option value="" disabled selected>Pick a role</option>' in page


def test_roleless_signup_is_visible_and_asked(app, seeded, wcfg):  # noqa: F811
    with seeded.tx() as s:
        ev = event(s, wcfg, ORG, 30, 2)
        signups.join(s, wcfg, P1, ev.id, char(s, wcfg, P1, "Earlybird").id, recognition=True)
        events.update(s, wcfg, ORG, ev.id, {"roles": ROLES})
        pid = ev.public_id
    c = login(app, 10)
    page = c.get(f"/wow/quester/events/{pid}").text
    assert "No role picked yet" in page and "Earlybird" in page
    assert "Pick a role below" in page
    t = csrf(c, f"/wow/quester/events/{pid}")
    r = c.post(f"/wow/quester/events/{pid}/reconfirm", data={"csrf": t, "role_key": "damage"})
    assert r.status_code == 303
    assert "No role picked yet" not in c.get(f"/wow/quester/events/{pid}").text


def test_cohosts_typed_as_names(app, seeded, wcfg):  # noqa: F811
    with seeded.tx() as s:
        ev = event(s, wcfg, ORG, 30, 2)
        pid = ev.public_id
    co = login(app, 4, "event_creator")                    # signs in once: username is learned
    with seeded.tx() as s:
        s.execute(text("update members set username='questgiver' where discord_id=:d"), {"d": actor(4).discord_id})
    c = login(app, 1, "event_creator")
    form = c.get(f"/wow/quester/events/{pid}/edit").text
    assert "Discord usernames, separated by commas" in form
    t = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
    data = {k: v for k, v in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form)}
    data.update({"csrf": t, "cohosts": "@questgiver", "title": "Deadmines run", "edition": "forever",
                 "ruleset": "pvp", "faction": "horde", "activity_type": "dungeon", "signup_policy": "open"})
    r = c.post(f"/wow/quester/events/{pid}/edit", data=data)
    assert r.status_code == 303, r.text[:500]
    with seeded.tx() as s:
        mid = s.scalar(select(Member.id).where(Member.username == "questgiver"))
        assert s.scalar(select(EventCohost.member_id).where(EventCohost.member_id == mid)) == mid
    assert 'value="@questgiver"' in c.get(f"/wow/quester/events/{pid}/edit").text
    r = c.post(f"/wow/quester/events/{pid}/edit", data={**data, "cohosts": "@nobody_here"})
    assert r.status_code == 422 and "find @nobody_here" in r.text
    del co


def test_attendee_story_button_and_editor(app, seeded, wcfg, monkeypatch):  # noqa: F811
    pid, ids = _ended(seeded, wcfg, monkeypatch)
    c = login(app, 10)
    page = c.get(f"/wow/quester/events/{pid}").text
    assert "Write your story" in page
    r = c.post(f"/wow/quester/events/{pid}/story", data={"csrf": csrf(c, f"/wow/quester/events/{pid}")})
    assert r.status_code == 303 and "/wow/quester/chronicle/" in r.headers["location"]
    ed = c.get(r.headers["location"]).text
    assert "PlayerOne" in ed or "Nick" in ed
    assert "The plan" in ed and "History (1 version)" in ed
    assert 'type="checkbox" name="coauthor"' in ed                     # pick co-authors, no ID field
    absent = login(app, 11)
    assert "Write your story" not in absent.get(f"/wow/quester/events/{pid}").text
    with seeded.tx() as s:
        assert s.scalar(select(text("count(*)")).select_from(ChronicleEntry)) == 2
        assert chronicle.lead(s, s.scalar(select(ChronicleEntry.event_id).limit(1))).author_id == ids[ORG.discord_id]
