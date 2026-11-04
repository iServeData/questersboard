"""When an edit asks people to reconfirm: time changes ask everyone; edition/ruleset/faction/realm
changes only move people whose character no longer fits (they pick another); small end-only changes
can be notice-only via [scheduling] end_change_notice_only_minutes."""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from conftest import actor, at, make_cfg, needs_db
from helpers import char, event
from sqlalchemy import select
from test_web import app, csrf, login, wcfg  # noqa: F401  (fixtures)

from wowquester.domain import events, signups
from wowquester.domain.errors import Invalid
from wowquester.models import Character, Outbox, Signup

pytestmark = needs_db
ORG = actor(1, "event_creator", name="HostOne")
HUMAN = dict(faction="alliance", race_key="human", class_key="warrior")


def _state(s, ev_id, a):
    su = s.scalar(select(Signup).join(Signup.member).where(
        Signup.event_id == ev_id, Signup.state != "withdrawn")
        .where(Signup.member.has(discord_id=a.discord_id)))
    return su.state, su.needs_reconfirm


def _last_change(s, ev_id):
    return s.scalars(select(Outbox).where(Outbox.kind == "notify.material_change")
                     .order_by(Outbox.id.desc())).first().payload


def test_faction_change_moves_only_misfits(seeded, cfg):
    p1, p2 = actor(50), actor(51)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2, faction="any")
        signups.join(s, cfg, p1, ev.id, char(s, cfg, p1, "Hordeone").id)
        signups.join(s, cfg, p2, ev.id, char(s, cfg, p2, "Allyone", **HUMAN).id)
        # the warning preview names the misfit and changes nothing
        assert events.fit_impact(s, cfg, ORG, ev.id, {"faction": "horde"}) == ["Allyone"]
        assert _state(s, ev.id, p2) == ("confirmed", False) and ev.faction is None
        r = events.update(s, cfg, ORG, ev.id, {"faction": "horde"})
        assert len(r["repick"]) == 1 and r["reconfirm"] == []
        assert _state(s, ev.id, p1) == ("confirmed", False)      # still fits: notice only
        assert _state(s, ev.id, p2) == ("tentative", True)       # asked to pick another character
        pay = _last_change(s, ev.id)
        assert pay["changes"] == ["requirements"] and pay["repick"] == r["repick"] and pay["reconfirm"] is False


def test_misfit_must_pick_a_fitting_character(seeded, cfg):
    p2 = actor(51)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2, faction="any")
        signups.join(s, cfg, p2, ev.id, char(s, cfg, p2, "Allytwo", **HUMAN).id)
        events.update(s, cfg, ORG, ev.id, {"faction": "horde"})
        with pytest.raises(Invalid, match="doesn't fit"):
            signups.reconfirm(s, cfg, p2, ev.id)
        orc = char(s, cfg, p2, "Orctwo")
        su = signups.reconfirm(s, cfg, p2, ev.id, character_id=orc.id)
        assert su.state == "confirmed" and not su.needs_reconfirm and su.character_id == orc.id


def test_freed_spot_skips_waitlisted_misfits(seeded, cfg):
    a1, a2, h3 = actor(60), actor(61), actor(62)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2, faction="any", participant_limit=1)
        signups.join(s, cfg, a1, ev.id, char(s, cfg, a1, "Allyfirst", **HUMAN).id)
        signups.join(s, cfg, a2, ev.id, char(s, cfg, a2, "Allysecond", **HUMAN).id)
        signups.join(s, cfg, h3, ev.id, char(s, cfg, h3, "Hordethird").id)
        assert _state(s, ev.id, a2)[0] == "waitlisted" and _state(s, ev.id, h3)[0] == "waitlisted"
        events.update(s, cfg, ORG, ev.id, {"faction": "horde"})
        assert _state(s, ev.id, a1) == ("tentative", True)
        assert _state(s, ev.id, a2) == ("tentative", True)
        assert _state(s, ev.id, h3)[0] == "confirmed"            # promoted past the misfit


def test_time_change_still_asks_everyone(seeded, cfg):
    p1 = actor(50)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2)
        signups.join(s, cfg, p1, ev.id, char(s, cfg, p1, "Timely").id)
        r = events.update(s, cfg, ORG, ev.id, {"end_at": ev.end_at + timedelta(minutes=30)})
        assert r["reconfirm"] and _state(s, ev.id, p1) == ("confirmed", True)
        assert _last_change(s, ev.id)["reconfirm"] is True


def test_small_end_change_can_be_notice_only(seeded):
    cfg = make_cfg(scheduling={"max_simultaneous_wow_events": 1, "end_change_notice_only_minutes": 30})
    p1 = actor(50)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2)
        signups.join(s, cfg, p1, ev.id, char(s, cfg, p1, "Patient").id)
        r = events.update(s, cfg, ORG, ev.id, {"end_at": ev.end_at + timedelta(minutes=30)})
        assert r["reconfirm"] == [] and _state(s, ev.id, p1) == ("confirmed", False)
        pay = _last_change(s, ev.id)
        assert pay["changes"] == ["time"] and pay["reconfirm"] is False
        r = events.update(s, cfg, ORG, ev.id, {"end_at": ev.end_at + timedelta(minutes=45)})
        assert r["reconfirm"]                                      # over the limit: reconfirm
        r = events.update(s, cfg, ORG, ev.id, {"start_at": at(31), "end_at": at(33)})
        assert r["reconfirm"]                                      # start moved: always reconfirm


def test_web_warns_before_saving_and_misfit_repicks(app, seeded, wcfg):  # noqa: F811
    p2 = actor(51)
    with seeded.tx() as s:
        ev = event(s, wcfg, ORG, 30, 2, faction="any")
        signups.join(s, wcfg, p2, ev.id, char(s, wcfg, p2, "Allyweb", **HUMAN).id)
        char(s, wcfg, p2, "Orcweb")
        pid = ev.public_id
    c = login(app, 1, "event_creator")
    form = c.get(f"/wow/quester/events/{pid}/edit").text
    t = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
    data = {k: v for k, v in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form)}
    data.update({"csrf": t, "title": "Deadmines run", "edition": "forever", "ruleset": "pvp",
                 "faction": "horde", "activity_type": "dungeon", "signup_policy": "open"})
    r = c.post(f"/wow/quester/events/{pid}/edit", data=data)
    assert r.status_code == 409 and "1 signed-up character won" in r.text and "Allyweb" in r.text
    assert 'name="confirm_fit" value="1"' in r.text and "Save and ask them" in r.text
    with seeded.tx() as s:
        assert s.scalar(select(Signup.state).where(Signup.member.has(discord_id=p2.discord_id))) == "confirmed"
    r = c.post(f"/wow/quester/events/{pid}/edit", data={**data, "confirm_fit": "1"})
    assert r.status_code == 303, r.text[:400]
    m = login(app, 51)
    page = m.get(f"/wow/quester/events/{pid}").text
    assert "Allyweb no longer fits" in page and "Pick another character" in page
    with seeded.tx() as s:
        orc = s.scalar(select(Signup.character_id).where(Signup.member.has(discord_id=p2.discord_id)))
        orc_id = s.scalar(select(Character.id).where(Character.name == "Orcweb"))
        assert orc != orc_id
    t = csrf(m, f"/wow/quester/events/{pid}")
    r = m.post(f"/wow/quester/events/{pid}/reconfirm", data={"csrf": t, "character_id": str(orc_id)})
    assert r.status_code == 303
    with seeded.tx() as s:
        su = s.scalar(select(Signup).where(Signup.member.has(discord_id=p2.discord_id)))
        assert su.state == "confirmed" and su.character_id == orc_id


def test_worker_dms_repick_and_notice(seeded, worker_db, cfg):
    from fake_discord import FakeDiscord

    from wowquester.config import ConfigHolder
    from wowquester.worker import Worker
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    p1, p2 = actor(50), actor(51)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2, faction="any")
        signups.join(s, cfg, p1, ev.id, char(s, cfg, p1, "Hordenote").id)
        signups.join(s, cfg, p2, ev.id, char(s, cfg, p2, "Allynote", **HUMAN).id)
        events.update(s, cfg, ORG, ev.id, {"faction": "horde"})
    for _ in range(10):
        if not w.tick():
            break
    texts = [c for _, c in fake.dms if "changed (who can join)" in c]
    assert len(texts) == 3                      # player that fits, player who must repick, organizer
    assert sum("no longer fits" in c for c in texts) == 1
    assert not any("Please reconfirm" in c for c in texts)
