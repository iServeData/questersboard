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
    with pytest.raises(Invalid, match="Tauren can't be a Mage in WoW: Forever. Tauren classes: Druid, Hunter, "
                                      "Shaman, Warrior"), seeded.tx() as s:
        char(s, cfg, actor(10), "Moo", race_key="tauren", class_key="mage")


def test_wrong_faction_race_explains_the_fix(seeded, cfg):
    # the bug seen on the prototype: Gnome offered to a Horde character
    with pytest.raises(Invalid, match="Gnome is an Alliance race in WoW: Forever. Switch the faction to "
                                      "Alliance"), seeded.tx() as s:
        char(s, cfg, actor(10), "Tink", race_key="Gnome", class_key="Druid")
    with pytest.raises(Invalid, match="is a Horde race"), seeded.tx() as s:
        char(s, cfg, actor(10), "Moo", faction="alliance", race_key="tauren", class_key="druid")


def test_labels_and_typed_entries_are_accepted(seeded, cfg):
    p = actor(10)
    with seeded.tx() as s:
        c = char(s, cfg, p, "Shandris", faction="alliance", race_key="Night Elf", class_key="Druid")
        assert (c.race_key, c.class_key) == ("night_elf", "druid")
        c = char(s, cfg, p, "Aerie", faction="alliance", race_key="Skyborne", class_key="mage")
        assert c.race_key == "skyborne"
        c = char(s, cfg, p, "Galewind", race_key="Skyborne (Windshaper)", class_key="Shaman")
        assert c.race_key == "skyborne"
        # not in the catalog: kept as typed (normalised), no guessing
        c = char(s, cfg, p, "Odd", race_key="Vulpera", class_key="Monk")
        assert (c.race_key, c.class_key) == ("vulpera", "monk")
    with pytest.raises(Invalid, match="Skyborne \\(Windshaper\\) can't be a Mage"), seeded.tx() as s:
        char(s, cfg, p, "Nope", race_key="skyborne", class_key="mage")


def test_skyborne_labels_follow_blizzard_and_seed_updates_labels(seeded, cfg):
    from wowquester.domain import catalog
    from wowquester.models import ReferenceOption
    with seeded.tx() as s:
        m = catalog.race_class_map(s, "forever")
        labels = {f: {r["key"]: r["label"] for r in rs} for f, rs in m["factions"].items()}
        assert labels["horde"]["skyborne"] == "Skyborne (Windshaper)"
        assert labels["alliance"]["skyborne"] == "Skyborne (High Order)"
        sky = {f: next(r["classes"] for r in rs if r["key"] == "skyborne") for f, rs in m["factions"].items()}
        assert sky == {"horde": ["druid", "hunter", "rogue", "shaman", "warrior"],
                       "alliance": ["druid", "hunter", "mage", "rogue", "warrior"]}
        row = s.query(ReferenceOption).filter_by(edition="forever", kind="race", key="skyborne",
                                                 parent_key="alliance").one()
        row.label = "Skyborne (Windshaper)"          # what an older seed wrote
    with seeded.tx() as s:
        assert catalog.seed_edition(s, cfg, "forever") == 1
        assert s.query(ReferenceOption).filter_by(kind="race", key="skyborne",
                                                  parent_key="alliance").one().label == "Skyborne (High Order)"


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


def test_seed_catalog_reports_rows_added_then_zero(db, cfg):
    """seed-catalog output must be a real count (it printed -75 on the first production run)."""
    from sqlalchemy import text as _t

    from wowquester.domain import catalog
    with db.tx() as s:
        first = catalog.seed_edition(s, cfg, "forever")
        total = s.execute(_t("select count(*) from reference_options where edition='forever'")).scalar()
    with db.tx() as s:
        again = catalog.seed_edition(s, cfg, "forever")
    assert first == total > 0
    assert again == 0


def test_host_can_sign_up_their_own_character(seeded, cfg):
    """The organizer is also a player: joining their own event must not clash with their host hold."""
    host = actor(1, "event_creator")
    with seeded.tx() as s:
        mine = char(s, cfg, host, "Hosty")
        ev = event(s, cfg, host, 0, 2)
        su = signups.join(s, cfg, host, ev.id, mine.id, role_key=None)
        assert su.state == "confirmed"
    with seeded.tx() as s:
        ev2 = event(s, cfg, host, 5, 2)
        su2 = signups.join(s, cfg, host, ev2.id, char(s, cfg, host, "Hostess").id, tentative=True)
        assert su2.state == "tentative"


def test_fit_problems_explain_why_a_character_cant_join(seeded, cfg):
    """Regression: a Forever PVP druid got only "you don't have a character that fits" for an RP event."""
    from helpers import char, event
    from conftest import actor
    from wowquester.bot.client import _fitting, no_fit_text
    from wowquester.domain import signups as sg
    host, p = actor(60, "event_creator"), actor(61)
    with seeded.tx() as s:
        ev = event(s, cfg, host, start=30, ruleset="rp", title="Test2")
        char(s, cfg, p, "Zanra")                                   # Forever PVP Horde
        char(s, cfg, p, "Allytank", faction="alliance", race_key="human", ruleset="rp")
        assert sg.event_requirement(cfg, ev) == "Horde WoW: Forever characters (US) on the RP ruleset"
        title, state, chars, roles, why = _fitting(s, cfg, p, ev.id)
        assert chars == []
        assert dict(why["misfits"]) == {"Zanra": ["PVP ruleset, not RP"], "Allytank": ["Alliance, not Horde"]}
        char(s, cfg, p, "Rptroll", ruleset="rp")
        assert [c[1].split(" · ")[0] for c in _fitting(s, cfg, p, ev.id)[2]] == ["Rptroll"]
    text = no_fit_text(cfg, title, why)
    assert "**Test2** is for Horde WoW: Forever characters (US) on the RP ruleset." in text
    assert "• Zanra: PVP ruleset, not RP" in text and "<https://" in text and "/characters>" in text


def test_name_rule_explains_blizzards_policy(seeded, cfg):
    import pytest
    from helpers import char
    from conftest import actor
    from wowquester.domain.errors import Invalid
    with seeded.tx() as s:
        with pytest.raises(Invalid, match="Blizzard doesn't allow numbers"):
            char(s, cfg, actor(62), "Zanra3")
        assert char(s, cfg, actor(62), "Zanra Stormhide").name == "Zanra Stormhide"
        assert char(s, cfg, actor(62), "Ñandú").name == "Ñandú"


def test_character_edit_cant_strand_its_signups(seeded, cfg):
    """Regression: a Forever character signed up for a Forever event was edited to Retail and stayed on the roster."""
    import pytest
    from helpers import char, event
    from conftest import actor
    from wowquester.domain import roster as rs, signups as sg
    from wowquester.domain.errors import Invalid
    host, p = actor(63, "event_creator"), actor(64)
    with seeded.tx() as s:
        ev = event(s, cfg, host, start=40, title="Test")
        c = char(s, cfg, p, "Zanra")
        sg.join(s, cfg, p, ev.id, c.id)
        to_retail = {"edition": "retail", "realm_slug": "area-52", "ruleset": None}
        with pytest.raises(Invalid, match="has been in WoW: Forever events \\(Test\\).*add it as a new one"):
            rs.update_character(s, cfg, p, c.id, to_retail)
        with pytest.raises(Invalid, match="signed up for Test.*Leave that event first"):
            rs.update_character(s, cfg, p, c.id, {"ruleset": "rp"})
        assert rs.update_character(s, cfg, p, c.id, {"level": 42, "spec": "Feral"}).level == 42   # harmless edits fine
        sg.leave(s, cfg, p, ev.id)
        assert rs.update_character(s, cfg, p, c.id, {"ruleset": "rp"}).ruleset == "rp"         # after leaving: ok
        with pytest.raises(Invalid, match="can't become a WoW \\(Retail\\) character"):
            rs.update_character(s, cfg, p, c.id, to_retail)                                   # history still counts
        fresh = char(s, cfg, p, "Voljin")                     # never joined anything: a wrong pick can be fixed
        assert rs.update_character(s, cfg, p, fresh.id, to_retail).edition == "retail"
        back = rs.update_character(s, cfg, p, fresh.id, {"edition": "forever", "realm_slug": None, "ruleset": "pvp"})
        assert back.edition == "forever"
