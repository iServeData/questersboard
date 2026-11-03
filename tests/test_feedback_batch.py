"""Host feedback batch: attendance prefill, group sizes, role picking, co-host names, notices,
chronicle versions, per-attendee stories. Discord is the FIXTURE fake (tests/fake_discord.py)."""
from __future__ import annotations

import pytest
from conftest import actor, at, fake_id, make_cfg, needs_db
from fake_discord import FakeDiscord
from helpers import char, event
from sqlalchemy import select, text

from wowquester.config import ConfigHolder
from wowquester.domain import catalog, chronicle, events, people, roster, signups
from wowquester.domain.errors import Invalid, PermissionDenied
from wowquester.models import Attendance, ChronicleEntry, ChronicleRevision, Member, PublicChronicle, Signup
from wowquester.worker import SUPPRESS_EMBEDS, Worker

pytestmark = needs_db
ORG = actor(1, "event_creator", name="HostOne")
CO = actor(4, "event_creator", name="CoHost")
REV = actor(2, "chronicle_reviewer")
P1 = actor(10, name="PlayerOne")
P2 = actor(11, name="PlayerTwo")
ROLES = [{"key": "tank", "label": "Tank", "quantity": 1}, {"key": "healer", "label": "Healer", "quantity": 1},
         {"key": "damage", "label": "Damage", "quantity": 3}]


def _drain(w, n=20):
    for _ in range(n):
        if not w.tick():
            break


def _mid(s, a):
    return s.scalar(select(Member.id).where(Member.discord_id == a.discord_id))


# ----------------------------------------------------------------- group sizes
def test_quest_and_raid_size_limits(seeded, cfg):
    with seeded.tx() as s:
        with pytest.raises(Invalid, match="at most 5"):
            event(s, cfg, ORG, 10, 2, activity_type="quest", participant_limit=6)
        ev = event(s, cfg, ORG, 10, 2, activity_type="quest")
        assert ev.participant_limit == 5                       # blank limit becomes the group size
        with pytest.raises(Invalid, match="add up to 6 spots but the participant limit is 5"):
            events.update(s, cfg, ORG, ev.id, {"roles": [{"key": "damage", "label": "Damage", "quantity": 6}]})
        raid = event(s, cfg, ORG, 20, 2, activity_type="raid", participant_limit=40)
        assert raid.participant_limit == 40
        with pytest.raises(Invalid, match="at most 40"):
            events.update(s, cfg, ORG, raid.id, {"participant_limit": 41})
        with pytest.raises(Invalid, match="participant limit is 10"):
            events.update(s, cfg, ORG, raid.id, {"participant_limit": 10,
                                                 "roles": [{"key": "tank", "label": "Tank", "quantity": 4},
                                                           {"key": "damage", "label": "Damage", "quantity": 7}]})


def test_party_size_is_config():
    cfg = make_cfg(signups={"party_size_max": {"quest": 3}})
    assert events.party_max(cfg, "quest") == 3 and events.party_max(cfg, "raid") == 40
    assert events.party_max(cfg, "social") == 80
    with pytest.raises(Exception, match="party_size_max"):
        make_cfg(signups={"party_size_max": {"quest": 0}})


# ----------------------------------------------------------------- rulesets / editions
def test_rp_pvp_ruleset_and_classic_editions(seeded, cfg):
    ed = cfg.edition("forever")
    assert "rp_pvp" in ed.rulesets and ed.ruleset_label("rp_pvp") == "RP-PvP" and ed.ruleset_label("pvp") == "PvP"
    for key, lvl in (("classic_era", 60), ("classic_anniversary", 70), ("mists_classic", 90)):
        assert cfg.edition(key).uses_realms and cfg.edition(key).max_level == lvl
    with seeded.tx() as s:
        c = char(s, cfg, P1, "Rpfighter", ruleset="rp_pvp")
        assert c.ruleset == "rp_pvp"
        assert catalog.seed_edition(s, cfg, "mists_classic") > 0
        kinds = dict(s.execute(text("select kind, count(*) from reference_options where edition='mists_classic' "
                                    "group by kind")).all())
        assert kinds["race"] >= 14 and kinds["location"] > 50
        monk = s.execute(text("select count(*) from reference_options where edition='classic_era' "
                              "and key='monk'")).scalar()
        assert monk == 0
        catalog.seed_edition(s, cfg, "forever")
        rows = catalog.options(s, "forever", "location", None, "Dead", limit=5)
        assert any(o.label == "The Deadmines" and o.parent_key == "dungeon" for o in rows)


def test_role_presets_are_the_three_group_roles(cfg):
    assert [p.key for p in cfg.role_presets] == ["tank", "healer", "damage"]


# ----------------------------------------------------------------- attendance prefill
def test_attendance_keeps_saved_values(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2)
        for a, n in ((P1, "Aaa"), (P2, "Bbb")):
            signups.join(s, cfg, a, ev.id, char(s, cfg, a, n).id)
        monkeypatch.setattr(events, "now", lambda: at(1))
        events.start(s, cfg, ORG, ev.id)
        events.end(s, cfg, ORG, ev.id)
        m1, m2 = _mid(s, P1), _mid(s, P2)
        events.record_attendance(s, cfg, ORG, ev.id, {m1: "attended", m2: "absent"})
        # the form re-sends everyone; unchanged rows must keep who recorded them
        first = s.get(Attendance, (ev.id, m1)).recorded_at
        events.record_attendance(s, cfg, ORG, ev.id, {m1: "attended", m2: "attended"})
        assert s.get(Attendance, (ev.id, m1)).recorded_at == first
        assert s.get(Attendance, (ev.id, m2)).status == "attended"


# ----------------------------------------------------------------- role picking
def test_roles_added_later_ask_existing_signups_to_pick(seeded, worker_db, cfg):
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2)
        signups.join(s, cfg, P1, ev.id, char(s, cfg, P1, "Earlybird").id)
        eid = ev.id
    _drain(w)
    fake.dms.clear()
    with seeded.tx() as s:
        events.update(s, cfg, ORG, eid, {"roles": ROLES})
        su = s.scalar(select(Signup).where(Signup.event_id == eid))
        assert su.role_key is None and su.needs_reconfirm
        with pytest.raises(Invalid, match="Pick a role"):
            signups.reconfirm(s, cfg, P1, eid)
        signups.reconfirm(s, cfg, P1, eid, "tank")
        assert su.role_key == "tank" and not su.needs_reconfirm
    _drain(w)
    assert any("now has group roles" in c for _, c in fake.dms)


# ----------------------------------------------------------------- co-host names + notices
def test_cohosts_by_discord_username(seeded, cfg):
    with seeded.tx() as s:
        roster.ensure_member(s, actor(4, name="CoHost"))
        s.execute(text("update members set username='questgiver' where display_name='CoHost'"))
        roster.ensure_member(s, actor(5, name="Same"))
        roster.ensure_member(s, actor(6, name="Same"))
        assert people.split("@questgiver, healbot") == ["@questgiver", "healbot"]
        assert people.resolve(s, ["@QuestGiver"]) == [CO.discord_id]
        assert people.resolve(s, [f"<@{CO.discord_id}>"]) == [CO.discord_id]
        with pytest.raises(Invalid, match="More than one"):
            people.resolve(s, ["Same"])
        with pytest.raises(Invalid, match="Couldn't find nobody"):
            people.resolve(s, ["nobody"])
        nid = fake_id(40)
        found = people.resolve(s, ["newbie"], search=lambda q: [{"id": nid, "username": "newbie"}])
        assert found == [nid]
        assert s.scalar(select(Member.username).where(Member.discord_id == nid)) == "newbie"


def test_cohost_edit_notifies_host_without_link_cards(seeded, worker_db, cfg):
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2)
        events.update(s, cfg, ORG, ev.id, {"cohosts": [CO.discord_id]})
        eid = ev.id
    _drain(w)
    fake.dms.clear()
    fake.dm_flags.clear()
    with seeded.tx() as s:
        events.update(s, cfg, CO, eid, {"description": "Bring water", "title": "Deadmines run"})
    _drain(w)
    host = [c for u, c in fake.dms if u == ORG.discord_id]
    assert host and "changed **Deadmines run** (description)" in host[0]
    assert not [c for u, c in fake.dms if u == CO.discord_id]          # the editor isn't told about their edit
    assert fake.dm_flags and all(f & SUPPRESS_EMBEDS for f in fake.dm_flags)


def test_no_notice_when_nothing_changed(seeded, worker_db, cfg):
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2)
        events.update(s, cfg, ORG, ev.id, {"cohosts": [CO.discord_id]})
        eid = ev.id
    _drain(w)
    fake.dms.clear()
    with seeded.tx() as s:
        events.update(s, cfg, CO, eid, {"title": "Deadmines run"})
    _drain(w)
    assert not fake.dms


# ----------------------------------------------------------------- chronicle
GOOD = {"attempted": "Clear the mine.", "happened": "We cleared it.", "outcome": "completed",
        "moments": "The cannon."}


def _done(s, cfg, monkeypatch):
    ev = event(s, cfg, ORG, 0, 2)
    events.update(s, cfg, ORG, ev.id, {"cohosts": [CO.discord_id]})
    for a, n in ((P1, "Aaa"), (P2, "Bbb")):
        signups.join(s, cfg, a, ev.id, char(s, cfg, a, n).id, recognition=True)
    monkeypatch.setattr(events, "now", lambda: at(1))
    events.start(s, cfg, ORG, ev.id)
    events.end(s, cfg, ORG, ev.id)
    events.record_attendance(s, cfg, ORG, ev.id, {_mid(s, P1): "attended", _mid(s, P2): "absent"})
    return ev, chronicle.lead(s, ev.id)


def test_each_save_is_a_new_version(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev, e = _done(s, cfg, monkeypatch)
        chronicle.save(s, cfg, ORG, e.id, GOOD)
        assert [h.number for h in chronicle.history(s, e.id)] == [1]      # first fill uses the blank v1
        chronicle.save(s, cfg, ORG, e.id, {**GOOD, "happened": "We cleared it fast."})
        chronicle.save(s, cfg, ORG, e.id, {**GOOD, "happened": "We cleared it fast."})   # no change
        chronicle.save(s, cfg, ORG, e.id, {**GOOD, "moments": "Cannon and parrot."})
        assert [h.number for h in chronicle.history(s, e.id)] == [1, 2, 3]


def test_attendees_write_their_own_stories_on_one_page(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev, lead = _done(s, cfg, monkeypatch)
        with pytest.raises(PermissionDenied):
            chronicle.start_story(s, cfg, P2, ev.id)                    # marked absent
        mine = chronicle.start_story(s, cfg, P1, ev.id)
        assert mine.id != lead.id and chronicle.start_story(s, cfg, P1, ev.id).id == mine.id
        co = chronicle.start_story(s, cfg, CO, ev.id)
        with pytest.raises(PermissionDenied):
            chronicle.save(s, cfg, ORG, mine.id, GOOD)                 # stories belong to their authors
        chronicle.save(s, cfg, P1, mine.id, {"happened": "I pulled the boss early."})
        chronicle.save(s, cfg, CO, co.id, {"happened": "I marked targets."})
        chronicle.save(s, cfg, ORG, lead.id, GOOD)
        for e, who in ((mine, P1), (co, CO), (lead, ORG)):
            chronicle.submit(s, cfg, who, e.id)
            chronicle.review(s, cfg, REV, e.id, "approve")
        pcs = s.scalars(select(PublicChronicle)).all()
        assert len(pcs) == 1 and pcs[0].public_id == lead.public_id     # one card per event
        body = pcs[0].body_html
        host_at, co_at, p1_at = body.index("We cleared it"), body.index("I marked targets"), body.index("I pulled")
        assert host_at < co_at < p1_at                                  # host, then co-host, then others
        assert "Told by Aaa" in body and "The plan" in body and "Best moments" in body
        chronicle.unpublish(s, cfg, REV, mine.id, "Names a player who opted out")
        assert "I pulled" not in s.scalar(select(PublicChronicle.body_html))
        kinds = [r[0] for r in s.execute(text("select kind from outbox where payload->>'decision' = 'unpublished'"))]
        assert kinds == ["notify.chronicle_decision"]


def test_coauthors_are_picked_and_removable(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev, lead = _done(s, cfg, monkeypatch)
        choices = dict(chronicle.coauthor_choices(s, cfg, lead))
        p1 = _mid(s, P1)
        assert p1 in choices
        chronicle.set_coauthors(s, cfg, ORG, lead.id, [p1])
        assert chronicle.coauthor_ids(s, lead.id) == {p1}
        chronicle.set_coauthors(s, cfg, ORG, lead.id, [])
        assert chronicle.coauthor_ids(s, lead.id) == set()
        outsider = roster.ensure_member(s, actor(30)).member_id
        with pytest.raises(Invalid):
            chronicle.set_coauthors(s, cfg, ORG, lead.id, [outsider])


def test_chronicle_prompt_invites_attendees(seeded, worker_db, cfg, monkeypatch):
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        _done(s, cfg, monkeypatch)
    _drain(w)
    texts = {u: c for u, c in fake.dms}
    assert "Mark attendance" in texts[ORG.discord_id]
    assert "Add your story as co-host" in texts[CO.discord_id]
    assert "Write your story" in texts[P1.discord_id]
    assert s_count(seeded, "select count(*) from chronicle_entries") == 1   # stories start only when asked


def s_count(db, sql):
    with db.tx() as s:
        return s.execute(text(sql)).scalar()


def test_unique_story_per_author(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev, lead = _done(s, cfg, monkeypatch)
        assert s.scalar(select(ChronicleRevision.number).where(ChronicleRevision.entry_id == lead.id)) == 1
        with pytest.raises(Exception):
            s.add(ChronicleEntry(event_id=ev.id, slug="dup-x", author_id=lead.author_id, state="draft"))
            s.flush()


def test_new_quest_with_too_many_role_spots_names_group_size(seeded, cfg):
    with seeded.tx() as s:
        with pytest.raises(Invalid, match="quest group size is 5"):
            event(s, cfg, ORG, 10, 2, activity_type="quest",
                  roles=[{"key": "damage", "label": "Damage", "quantity": 6}])
