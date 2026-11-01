from __future__ import annotations

import pytest
from conftest import actor, needs_db
from helpers import char, event
from sqlalchemy import text

from wowquester.domain import events, roster, signups
from wowquester.domain.errors import Conflict, Invalid, PermissionDenied

pytestmark = needs_db
ORG = actor(1, "event_creator")


def test_forever_has_no_realm_retail_requires_realm(seeded, cfg):
    p = actor(10)
    with pytest.raises(Invalid, match="no realms"), seeded.tx() as s:
        char(s, cfg, p, "Ragna", realm_slug="stormrage")
    with pytest.raises(Invalid, match="need a realm"), seeded.tx() as s:
        roster.add_character(s, cfg, p, {"edition": "retail", "name": "Ragna", "faction": "horde",
                                         "race_key": "orc", "class_key": "warrior"})
    with seeded.tx() as s:
        c = roster.add_character(s, cfg, p, {"edition": "retail", "realm_slug": "stormrage", "name": "Ragna",
                                             "faction": "horde", "race_key": "orc", "class_key": "warrior"})
        assert c.ruleset is None and c.realm_slug == "stormrage"
        c2 = char(s, cfg, p, "Ragna")
        assert c2.ruleset == "pvp" and c2.realm_slug is None


def test_invalid_race_class_rejected(seeded, cfg):
    with pytest.raises(Invalid, match="not a valid"), seeded.tx() as s:
        char(s, cfg, actor(10), "Moo", race_key="tauren", class_key="mage")


def test_cannot_edit_others_character(seeded, cfg):
    with seeded.tx() as s:
        cid = char(s, cfg, actor(10), "Mine").id
    with pytest.raises(PermissionDenied), seeded.tx() as s:
        roster.update_character(s, cfg, actor(11), cid, {"notes": "hi"})


def test_duplicate_signup_and_wrong_character(seeded, cfg):
    p, q = actor(10), actor(11)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2)
        c = char(s, cfg, p, "Mine")
        cq = char(s, cfg, q, "Theirs")
        signups.join(s, cfg, p, ev.id, c.id)
        ids = ev.id, c.id, cq.id
    with pytest.raises(Conflict, match="already signed up"), seeded.tx() as s:
        signups.join(s, cfg, p, ids[0], ids[1])
    with pytest.raises(PermissionDenied), seeded.tx() as s:
        signups.join(s, cfg, p, ids[0], ids[2])


def test_ruleset_mismatch(seeded, cfg):
    p = actor(10)
    with pytest.raises(Invalid, match="ruleset"), seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2)
        c = char(s, cfg, p, "Normy", ruleset="normal")
        signups.join(s, cfg, p, ev.id, c.id)


def test_waitlist_promotion_on_leave(seeded, cfg):
    a, b = actor(10), actor(11)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2, roles=[{"key": "tank", "quantity": 1}, {"key": "healer", "quantity": 1}])
        ca, cb = char(s, cfg, a, "Aaa"), char(s, cfg, b, "Bbb")
        assert signups.join(s, cfg, a, ev.id, ca.id, role_key="tank").state == "confirmed"
        assert signups.join(s, cfg, b, ev.id, cb.id, role_key="tank").state == "waitlisted"
        signups.leave(s, cfg, a, ev.id)
        st = s.execute(text("select state from signups where member_id = (select id from members where "
                            "discord_id=:d) and state <> 'withdrawn'"), {"d": b.discord_id}).scalar()
        assert st == "confirmed"


def test_approval_policy_and_permissions(seeded, cfg):
    a = actor(10)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2, signup_policy="approval")
        su = signups.join(s, cfg, a, ev.id, char(s, cfg, a, "Aaa").id)
        assert su.state == "pending"
        sid = su.id
    with pytest.raises(PermissionDenied), seeded.tx() as s:
        signups.approve(s, cfg, actor(12), sid)
    with seeded.tx() as s:
        assert signups.approve(s, cfg, ORG, sid).state == "confirmed"
        n = s.execute(text("select count(*) from outbox where kind='notify.signup_decision'")).scalar()
        assert n == 1


def test_copy_makes_clean_draft(seeded, cfg):
    a = actor(10)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2, roles=[{"key": "tank", "quantity": 2}])
        signups.join(s, cfg, a, ev.id, char(s, cfg, a, "Aaa").id, role_key="tank")
        from conftest import at
        cp = events.copy(s, cfg, actor(11), ev.id, at(24), at(26))
        assert cp.state == "draft" and cp.copied_from_id == ev.id and [r.quantity for r in cp.roles] == [2]
        assert s.execute(text("select count(*) from signups where event_id=:e"), {"e": cp.id}).scalar() == 0
        assert s.execute(text("select count(*) from event_reservations where event_id=:e"),
                         {"e": cp.id}).scalar() == 0


def test_public_projection_has_counts_not_names(seeded, cfg):
    a = actor(10, name="SecretDiscordName")
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2, roles=[{"key": "tank", "quantity": 2}])
        signups.join(s, cfg, a, ev.id, char(s, cfg, a, "Aaa").id, role_key="tank")
        row = s.execute(text("select row_to_json(p)::text from public_events p")).scalar()
        assert '"filled": 1' in row.replace('"filled":1', '"filled": 1')
        assert "SecretDiscordName" not in row and "Aaa" not in row and a.discord_id not in row
