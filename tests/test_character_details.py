"""Imported character details (gear, talents, professions...). All Blizzard answers here are FIXTURES
shaped like the WoW: Forever US pages seen by `blizzard probe-character`; names are made up."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from conftest import actor, needs_db
from helpers import char
from sqlalchemy import text
from fastapi.testclient import TestClient
from test_web import _bz_app, csrf, login

from questersboard.domain import erasure, profiles, roster

BASE = "/profile/wow/character/zulmara-ashveil"
FIX = {
    "": {"id": 501, "name": "Zulmara Ashveil", "level": 17, "average_item_level": 9, "equipped_item_level": 9,
         "achievement_points": 40, "faction": {"type": "HORDE"}, "race": {"name": "Orc"},
         "character_class": {"name": "Hunter"}, "ruleset": {"id": 1},
         "guild": {"name": "Fixture Guild", "id": 9001, "key": {"href": "https://fixture/data/wow/guild/x/fixture-guild"}}},
    "/equipment": {"equipped_items": [
        {"slot": {"type": "HEAD", "name": "Head"}, "name": "Fixture Cap", "level": {"value": 12},
         "quality": {"type": "UNCOMMON"}, "item": {"id": 11},
         "enchantments": [{"display_string": "+3 Stamina"}]},
        {"slot": {"type": "CHEST", "name": "Chest"}, "name": "Fixture Vest", "level": {"value": 22},
         "quality": {"type": "RARE"}, "item": {"id": 12}}]},
    "/specializations": {"specialization_groups": [{"is_active": True, "specializations": [
        {"specialization_name": "Beast Mastery", "spent_points": 8,
         "talents": [{"talent": {"name": "Improved Aspect of the Hawk"}, "talent_rank": 3}]},
        {"specialization_name": "Marksmanship", "spent_points": 0, "talents": []}]}]},
    "/professions": {"primaries": [{"profession": {"name": "Leatherworking"},
                                    "tiers": [{"skill_points": 95, "max_skill_points": 150, "known_recipes": [
                                        {"name": "Fixture Pants", "id": 1355}, {"name": "Fixture Boots", "id": 1353},
                                        {"name": "Fixture Boots", "id": 1353}, {"name": "Leatherworking", "id": 2108}]}]}],
                     "secondaries": [{"profession": {"name": "Cooking"}, "skill_points": 23, "max_skill_points": 75}]},
    "/statistics": {"health": 410, "strength": {"base": 30, "effective": 34}, "melee_crit": {"rating": 0, "value": 5.1234},
                    "power_type": {"name": "Mana"}, "versatility": 0},
    "/character-media": {"assets": [{"key": "avatar", "value": "https://render.worldofwarcraft.com/us/fixture-avatar.jpg"},
                                    {"key": "inset", "value": "https://elsewhere.example.org/fixture.jpg"}]},
    "/pvp-summary": {"honorable_kills": 7, "pvp_rank": 0},
    "/reputations": {"reputations": [{"faction": {"name": "Orgrimmar"}, "standing": {"name": "Friendly", "value": 120, "max": 6000}}]},
    "/quests/completed": {"quests": [{"id": 1}, {"id": 2}, {"id": 3}]},
    "/achievements": {"total_points": 40, "achievements": [{"id": 1}, {"id": 2}]},
    "/collections/mounts": {"mounts": [{"mount": {"name": "Fixture Wolf"}}]},
    "/hunter-pets": {"hunter_pets": [{"name": "Fang", "level": 16, "creature_family": {"name": "Wolf"}}]},
}


def _handler(req):
    p = req.url.path
    if p.startswith(BASE):
        body = FIX.get(p[len(BASE):])
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)
    return httpx.Response(404)


# ------------------------------------------------------------------ extraction (no database)
def test_extract_keeps_a_curated_summary_of_each_page():
    pages = {k: v for k, v in FIX.items()}
    assert profiles.extract("overview", pages) == {"level": 17, "average_item_level": 9, "equipped_item_level": 9,
                                                   "achievement_points": 40}
    assert profiles.extract("guild", pages) == {"id": "9001", "name": "Fixture Guild"}
    gear = profiles.extract("gear", pages)
    assert gear[0] == {"slot": "Head", "name": "Fixture Cap", "item_level": 12, "quality": "uncommon", "item_id": 11,
                       "enchants": ["+3 Stamina"], "slot_type": "HEAD"}
    t = profiles.extract("talents", pages)
    assert t["groups"][0]["specs"][0] == {"name": "Beast Mastery", "points": 8,
                                          "talents": [{"name": "Improved Aspect of the Hawk", "rank": 3}]}
    assert profiles.extract("professions", pages) == [
        {"name": "Leatherworking", "kind": "primary", "skill": 95, "max": 150,      # sorted, once each, no self-entry
         "recipes": [{"id": 1353, "name": "Fixture Boots"}, {"id": 1355, "name": "Fixture Pants"}]},
        {"name": "Cooking", "kind": "secondary", "skill": 23, "max": 75}]
    st = profiles.extract("stats", pages)
    assert st == {"health": 410, "strength": 34, "melee_crit": 5.12, "power_type": "Mana"}   # no unlisted stats
    pr = profiles.extract("progress", pages)
    assert pr["quests_completed"] == 3 and pr["mounts"] == 1 and pr["achievements"] == 2
    assert "hunter_pets" not in pr                                       # pets are their own group now
    assert profiles.extract("pets", pages) == [{"name": "Fang", "level": 16, "family": "Wolf"}]
    assert profiles.extract("pets", {"/hunter-pets": {"hunter_pets": []}}) is None
    assert "name" not in profiles.extract("overview", pages)          # the character's name isn't copied


def test_missing_pages_give_no_data():
    assert profiles.extract("gear", {"": {}, "/equipment": None}) is None
    assert profiles.extract("guild", {"": {"level": 3}}) is None
    assert profiles.extract("progress", {"": {}}) is None


def test_sharing_beyond_private_needs_battlenet_verification_and_a_guild():
    prof = SimpleNamespace(shares={"gear": "guild", "stats": "members"}, guild_id="9001")
    ver, unver = SimpleNamespace(verification="ownership_verified"), SimpleNamespace(verification="api_matched")
    assert profiles.effective_share(prof, ver, "gear") == "guild"
    assert profiles.effective_share(prof, ver, "stats") == "members"
    assert profiles.effective_share(prof, unver, "gear") == "private"
    assert profiles.effective_share(prof, ver, "talents") == "private"
    prof.guild_id = None
    assert profiles.effective_share(prof, ver, "gear") == "private"     # no guild, nobody to share with


# ------------------------------------------------------------------ web + database
@needs_db
def test_import_all_groups_then_untick_some(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(70), "Zulmara Ashveil").id
    c = login(a, 70)
    page = c.get("/wow/quester/characters").text
    assert "Bring in details from Blizzard" in page and "Choose what to bring in" in page
    assert page.count('name="groups"') == len(profiles.GROUPS) and 'checked' in page
    form = {"csrf": csrf(c), "groups": list(profiles.GROUP_KEYS), "share_gear": "guild"}
    r = c.post(f"/wow/quester/characters/{cid}/details", data=form)
    assert r.status_code == 303, r.text
    with seeded.tx() as s:
        row = s.execute(text("select groups, shares, guild_id, guild_name from character_profiles "
                             "where character_id=:i"), {"i": cid}).one()
    groups, shares, gid, gname = row
    assert set(groups) == set(profiles.GROUP_KEYS) and (gid, gname) == ("9001", "Fixture Guild")
    assert shares["gear"] == "guild" and shares["stats"] == "private"
    assert [x["key"] for x in groups["portrait"]] == ["avatar"]          # only allowed image hosts
    page = c.get("/wow/quester/characters").text
    assert "Fixture Vest" in page and "Leatherworking 95/150" in page and "Refresh from Blizzard" in page
    assert "2 known recipes</summary>" in page and "Fixture Boots" in page   # collapsed under the skill
    assert "needs Battle.net verification" in page                       # gear asked for guild, still private
    csp = c.get("/wow/quester/characters").headers["content-security-policy"]
    assert "img-src 'self' data:;" in csp                               # portraits are served by QuestersBoard
    assert f"/wow/quester/characters/{cid}/portrait/avatar" in page and "render.worldofwarcraft.com" not in page

    calls.clear()
    r = c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["gear"]})
    assert r.status_code == 303
    fetched = {q.url.path[len(BASE):] for q in calls if q.url.path.startswith(BASE)}
    assert "/statistics" not in fetched and "/professions" not in fetched   # unticked = not even asked for
    with seeded.tx() as s:
        groups, gid = s.execute(text("select groups, guild_id from character_profiles where character_id=:i"),
                                {"i": cid}).one()
    assert set(groups) == {"gear"} and gid is None


def _recipe_handler(req):
    """FIXTURES: recipe 1353 makes item 501 (media in a version-pinned static namespace); 1355 has no
    recipe data and no item with its name."""
    p = req.url.path
    ns = req.url.params.get("namespace", "")
    if p == "/data/wow/recipe/1353" and ns == "static-forever-us":
        return httpx.Response(200, json={"id": 1353, "name": "Fixture Boots",
                                         "crafted_item": {"id": 501, "name": "Fixture Boots"}})
    if p == "/data/wow/item/501":
        return httpx.Response(200, json={
            "id": 501, "name": "Fixture Boots", "quality": {"type": "UNCOMMON"},
            "media": {"key": {"href": "https://us.api.blizzard.com/data/wow/media/item/501?namespace="
                                      "static-1.60.1_70399-forever-us"}, "id": 501},
            "preview_item": {"binding": {"name": "Binds when equipped"}, "inventory_type": {"name": "Feet"},
                             "item_subclass": {"name": "Leather"}, "armor": {"display": {"display_string": "41 Armor"}},
                             "stats": [{"display": {"display_string": "+3 Agility"}}]}})
    if p == "/data/wow/media/item/501" and ns == "static-1.60.1_70399-forever-us":
        return httpx.Response(200, json={"assets": [{"key": "icon", "value":
                                                     "https://render.worldofwarcraft.com/forever-us/icons/56/fixture_boots.jpg"}]})
    if p == "/data/wow/search/item":
        return httpx.Response(200, json={"results": []})
    return _handler(req)


@needs_db
def test_known_recipes_get_icons_and_tooltips(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _recipe_handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(76), "Zulmara Ashveil").id
    c = login(a, 76)
    assert c.post(f"/wow/quester/characters/{cid}/details",
                  data={"csrf": csrf(c), "groups": ["professions"]}).status_code == 303
    with seeded.tx() as s:
        groups = s.execute(text("select groups from character_profiles where character_id=:i"), {"i": cid}).scalar()
    boots, pants = groups["professions"][0]["recipes"]
    assert boots["item_id"] == 501 and boots["quality"] == "uncommon" and boots["icon"].endswith("fixture_boots.jpg")
    assert boots["tip"]["armor"] == "41 Armor" and boots["tip"]["stats"] == [{"text": "+3 Agility", "green": False}]
    assert "icon" not in pants and pants["seen"]                         # nothing found: retried in a week
    page = c.get("/wow/quester/characters").text
    assert f"/characters/{cid}/icon/recipes/0-0?" in page and "Leatherworking recipe" in page and "41 Armor" in page
    assert 'class="wq-recipe-plain"' in page and "render.worldofwarcraft.com" not in page
    r = c.get(f"/wow/quester/characters/{cid}/icon/recipes/0-1")
    assert r.status_code == 404                                          # no icon for that one
    calls.clear()
    assert c.post(f"/wow/quester/characters/{cid}/details",
                  data={"csrf": csrf(c), "groups": ["professions"]}).status_code == 303
    looked = [q.url.path for q in calls if q.url.path.startswith("/data/wow/")]
    assert looked == []                                                  # kept from the first import
    with seeded.tx() as s:
        groups = s.execute(text("select groups from character_profiles where character_id=:i"), {"i": cid}).scalar()
    assert groups["professions"][0]["recipes"][0]["icon"].endswith("fixture_boots.jpg")


@needs_db
def test_recipe_lookups_budget_and_switch(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _recipe_handler)
    cfg["blizzard"]["recipe_lookups"] = 1
    with seeded.tx() as s:
        cid = char(s, cfg, actor(77), "Zulmara Ashveil").id
    c = login(a, 77)
    c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["professions"]})
    with seeded.tx() as s:
        rs = s.execute(text("select groups from character_profiles where character_id=:i"), {"i": cid}).scalar()
    boots, pants = rs["professions"][0]["recipes"]
    assert "icon" in boots and set(pants) == {"id", "name"}              # budget of 1: the second waits
    cfg["blizzard"]["detail_icons"] = False
    calls.clear()
    c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["professions"]})
    assert not [q for q in calls if q.url.path.startswith("/data/wow/")]


@needs_db
def test_only_the_owner_can_import_and_remove(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(71), "Zulmara Ashveil").id
    o = login(a, 72)
    assert o.post(f"/wow/quester/characters/{cid}/details",
                  data={"csrf": csrf(o), "groups": ["gear"]}).status_code == 404
    c = login(a, 71)
    assert c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c)}).status_code == 422
    assert c.post(f"/wow/quester/characters/{cid}/details",
                  data={"csrf": csrf(c), "groups": ["gear"]}).status_code == 303
    assert o.post(f"/wow/quester/characters/{cid}/details/remove", data={"csrf": csrf(o)}).status_code == 404
    assert c.post(f"/wow/quester/characters/{cid}/details/remove", data={"csrf": csrf(c)}).status_code == 303
    with seeded.tx() as s:
        assert s.execute(text("select count(*) from character_profiles")).scalar() == 0


@needs_db
def test_not_found_on_blizzard_stores_nothing(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, lambda r: httpx.Response(404))
    with seeded.tx() as s:
        cid = char(s, cfg, actor(73), "Zulmara Ashveil").id
    c = login(a, 73)
    r = c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["gear"]})
    assert r.status_code == 422 and "no public profile" in r.text
    with seeded.tx() as s:
        assert s.execute(text("select count(*) from character_profiles")).scalar() == 0


def _stored(seeded, cfg, n, name="Zulmara Ashveil", days_old=0):
    with seeded.tx() as s:
        ch = char(s, cfg, actor(n), name)
        s.execute(text("insert into character_profiles (character_id, member_id, edition, region, groups, shares, "
                       "fetched_at) values (:c, :m, 'forever', 'us', '{\"pvp\": {\"honorable_kills\": 1}}', '{}', "
                       "now() - make_interval(days => :d))"), {"c": ch.id, "m": ch.member_id, "d": days_old})
        return ch.id, ch.member_id


@needs_db
def test_rename_archive_and_erasure_drop_imported_details(seeded, cfg):
    cid, _ = _stored(seeded, cfg, 74)
    with seeded.tx() as s:
        roster.update_character(s, cfg, actor(74), cid, {"name": "Zulmara Renamed"})
        assert s.execute(text("select count(*) from character_profiles where character_id=:i"), {"i": cid}).scalar() == 0
    cid, _ = _stored(seeded, cfg, 75)
    with seeded.tx() as s:
        roster.archive_character(s, cfg, actor(75), cid)
        assert s.execute(text("select count(*) from character_profiles where character_id=:i"), {"i": cid}).scalar() == 0
    cid, mid = _stored(seeded, cfg, 76)
    with seeded.tx() as s:
        r = erasure.erase(s, cfg, mid)
        assert r["character_details"] == 1
        assert s.execute(text("select count(*) from character_profiles where member_id=:m"), {"m": mid}).scalar() == 0


@needs_db
def test_worker_deletes_details_past_the_blizzard_limit(seeded, worker_db, cfg):
    from questersboard.config import ConfigHolder
    from questersboard.worker import Worker
    old, _ = _stored(seeded, cfg, 77, days_old=31)
    new, _ = _stored(seeded, cfg, 78, days_old=2)
    Worker(ConfigHolder(cfg), worker_db, None).housekeeping()
    with seeded.tx() as s:
        left = [r for r, in s.execute(text("select character_id from character_profiles order by 1"))]
    assert left == [new] and old not in left


@needs_db
def test_worker_cannot_read_imported_details():
    import os

    import psycopg
    from test_db_hardening import _connect
    with _connect("questersboard_worker", os.environ.get("WQT_WORKER_PW")) as c:
        c.execute("select character_id, fetched_at from character_profiles")
        c.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select groups from character_profiles")
        c.rollback()


# ------------------------------------------------------------------ display helpers
def test_stat_sections_hide_zeros_and_show_percentages():
    secs = dict(profiles.stat_sections({"health": 488, "power": 495, "power_type": "Mana", "spell_power": 0,
                                        "melee_crit": 9.96, "dodge": 9.92, "parry": 4.0, "block": 0.0,
                                        "mana_regen": 42.0, "armor": 1458}))
    assert secs["Basics"] == [("Health", "488"), ("Mana", "495")]
    assert ("Crit", "9.96%") in secs["Melee"] and ("Parry", "4%") in secs["Defense"]
    assert ("Armor", "1,458") in secs["Defense"] and all(lab != "Block" for lab, _ in secs["Defense"])
    assert "Spell" in secs and ("Mana regen", "42") in secs["Spell"]
    assert "Resistances" not in secs


def test_pet_family_reads_forever_and_retail_shapes():
    assert profiles.pet_family({"creature": {"name": "Cat"}}) == "Cat"
    assert profiles.pet_family({"creature_family": {"name": "Wolf"}, "creature": {"name": "Gray Wolf"}}) == "Wolf"
    assert profiles.pet_family({}) == ""


# ------------------------------------------------------------------ sharing: views, guild page, members list
def _shared(seeded, cfg, n, name, guild="9001", shares=None, verified=True, groups=None):
    import json
    groups = groups or {"overview": {"equipped_item_level": 20 + n % 10}, "guild": {"id": guild, "name": "Fixture Guild"},
                        "gear": [{"slot": "Head", "name": f"Fixture Cap {n}", "item_level": 12}],
                        "professions": [{"name": "Skinning", "kind": "primary", "skill": 10, "max": 75}],
                        "talents": {"groups": [{"active": True, "specs": [{"name": "Marksmanship", "points": 8,
                                                                            "talents": []}]}]},
                        "portrait": [{"key": "avatar", "url": "https://render.worldofwarcraft.com/forever-us/fixture.jpg"}]}
    with seeded.tx() as s:
        ch = char(s, cfg, actor(n), name, eligible_roles=["damage"])
        if verified:
            s.execute(text("update characters set verification='ownership_verified', api_external_id=:x, "
                           "api_refreshed_at=now() where id=:i"), {"i": ch.id, "x": str(1000 + n)})
        s.execute(text("insert into character_profiles (character_id, member_id, edition, region, guild_id, guild_name, "
                       "groups, shares) values (:c, :m, 'forever', 'us', :g, 'Fixture Guild', cast(:gr as jsonb), "
                       "cast(:sh as jsonb))"),
                  {"c": ch.id, "m": ch.member_id, "g": guild if "guild" in groups else None,
                   "gr": json.dumps(groups), "sh": json.dumps(shares or {})})
        return ch.id


@needs_db
def test_character_view_shows_only_what_is_shared(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    cid = _shared(seeded, cfg, 80, "Shared Hunter", shares={"gear": "members", "talents": "guild", "guild": "guild"})
    hidden = _shared(seeded, cfg, 81, "Private Hunter")                          # nothing shared
    unver = _shared(seeded, cfg, 82, "Unverified Hunter", shares={"gear": "members"}, verified=False)
    stranger = login(a, 83)
    page = stranger.get(f"/wow/quester/characters/{cid}/view")
    assert page.status_code == 200 and "Fixture Cap 80" in page.text
    assert "Marksmanship" not in page.text and "Skinning" not in page.text       # guild-only and private groups
    assert "Fixture Boots" not in page.text and "known recipe" not in page.text   # recipes follow Professions sharing
    assert stranger.get(f"/wow/quester/characters/{hidden}/view").status_code == 404
    assert stranger.get(f"/wow/quester/characters/{unver}/view").status_code == 404   # needs Battle.net verification
    owner = login(a, 81)
    own = owner.get(f"/wow/quester/characters/{hidden}/view")
    assert own.status_code == 200 and "Skinning" in own.text and "This is your character" in own.text


@needs_db
def test_guild_page_is_for_confirmed_guild_members_only(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    full = {g: "guild" for g in profiles.GROUP_KEYS}
    m1 = _shared(seeded, cfg, 84, "Guild Hunter", shares=full)
    _shared(seeded, cfg, 85, "Guild Lurker", shares={"gear": "guild"})           # guild membership not shared
    _shared(seeded, cfg, 86, "Otherguild Hunter", guild="9002", shares=full)
    _shared(seeded, cfg, 87, "Fake Member", shares=full, verified=False)         # not verified: not a member
    url = "/wow/quester/guilds/forever/us/9001"
    c = login(a, 84)
    page = c.get(url)
    assert page.status_code == 200 and "Fixture Guild roster" in page.text
    assert "Guild Hunter" in page.text and "Guild Lurker" not in page.text and "Otherguild" not in page.text
    assert "Fake Member" not in page.text and "Skinning" in page.text and f"/characters/{m1}/view" in page.text
    assert ">Fixture Guild</a>" in c.get("/wow/quester/characters").text       # link in the Characters menu
    assert c.get(url + "?profession=Tailoring").text.count("/view") == 0
    assert c.get(url + "?class=warrior&min_ilvl=20").text.count("/view") == 1
    for n in (85, 86, 87, 88):                                                    # lurker, other guild, unverified, nobody
        assert login(a, n).get(url).status_code == 404, n
    other = login(a, 86)
    assert other.get(f"/wow/quester/characters/{m1}/view").status_code == 404    # guild-only, different guild


@needs_db
def test_shared_characters_lists_member_shares(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    _shared(seeded, cfg, 89, "Open Hunter", shares={"overview": "members", "gear": "members"})
    _shared(seeded, cfg, 90, "Guildonly Hunter", shares={"gear": "guild", "guild": "guild"})
    page = login(a, 91).get("/wow/quester/players").text
    assert "Open Hunter" in page and "Guildonly Hunter" not in page


@needs_db
def test_shared_characters_sort_and_sticky_bar(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    sh = {"overview": "members", "gear": "members"}
    _shared(seeded, cfg, 95, "Alpha Sorter", shares=sh)                    # item level 25
    _shared(seeded, cfg, 97, "Bravo Sorter", shares=sh)                    # item level 27
    _shared(seeded, cfg, 96, "Charlie Sorter", shares={"gear": "members"})  # item level not shared: always last
    c = login(a, 98)
    order = lambda html: [n for n in sorted(("Alpha", "Bravo", "Charlie"), key=html.find) if n in html]
    page = c.get("/wow/quester/players").text
    assert order(page) == ["Alpha", "Bravo", "Charlie"] and "data-sticky-bar" in page and "data-sortable" in page
    assert 'data-sort-key="name" aria-sort="ascending"' in page and 'href="?sort=name&amp;dir=desc"' in page
    assert order(c.get("/wow/quester/players?sort=ilvl&dir=desc").text) == ["Bravo", "Alpha", "Charlie"]
    assert order(c.get("/wow/quester/players?sort=ilvl:desc").text) == ["Bravo", "Alpha", "Charlie"]
    assert order(c.get("/wow/quester/players?sort=name&dir=desc").text) == ["Charlie", "Bravo", "Alpha"]
    page = c.get("/wow/quester/players?sort=nonsense&class=warrior").text
    assert order(page) == ["Alpha", "Bravo", "Charlie"] and "(1 active)" in page
    assert 'href="?class=warrior&amp;sort=level"' in page and " style=" not in page


@needs_db
def test_portrait_is_served_by_questersboard(seeded, tmp_path, monkeypatch):
    def h(req):
        if req.url.host == "render.worldofwarcraft.com":
            return httpx.Response(200, content=b"\x89PNG-fixture", headers={"content-type": "image/png"})
        return _handler(req)
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, h)
    cid = _shared(seeded, cfg, 92, "Portrait Hunter", shares={"portrait": "members"})
    priv = _shared(seeded, cfg, 93, "Shy Hunter", shares={"gear": "members"})
    c = login(a, 94)
    r = c.get(f"/wow/quester/characters/{cid}/portrait/avatar")
    assert r.status_code == 200 and r.content == b"\x89PNG-fixture" and r.headers["content-type"] == "image/png"
    assert "private" in r.headers["cache-control"]
    assert c.get(f"/wow/quester/characters/{priv}/portrait/avatar").status_code == 404   # portrait not shared
    assert c.get(f"/wow/quester/characters/{cid}/portrait/main-raw").status_code == 404  # no such image


def test_fetch_image_refuses_other_hosts():
    from questersboard.integrations.blizzard import Blizzard
    from conftest import make_cfg
    seen = []
    b = Blizzard(make_cfg(blizzard={"enabled": True}),
                 http=httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(
                     200, content=b"x", headers={"content-type": "image/png"}))))
    assert b.fetch_image("https://elsewhere.example.org/a.png") is None
    assert b.fetch_image("http://render.worldofwarcraft.com/a.png") is None
    assert not seen
    assert b.fetch_image("https://render.worldofwarcraft.com/a.png") == (b"x", "image/png")


def test_set_all_applies_one_choice_to_every_group():
    from starlette.datastructures import FormData as MultiDict
    form = MultiDict([("groups", "gear"), ("share_all", "guild"), ("share_gear", "private")])
    assert set(profiles.parse_form(form)[1].values()) == {"guild"}
    form = MultiDict([("share_all", ""), ("share_gear", "members")])
    assert profiles.parse_form(form)[1]["gear"] == "members"
    assert profiles.parse_form(MultiDict([("share_all", "bogus")]))[1]["gear"] == "private"


@needs_db
def test_roster_and_view_have_the_characters_menu(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    full = {g: "guild" for g in profiles.GROUP_KEYS}
    cid = _shared(seeded, cfg, 95, "Navi Hunter", guild="9003", shares=full)
    c = login(a, 95)
    for url in ("/wow/quester/guilds/forever/us/9003", f"/wow/quester/characters/{cid}/view", "/wow/quester/players"):
        page = c.get(url).text
        menu = page.split('aria-controls="wq-dd-characters"', 1)[1].split("</div>", 1)[0]
        assert 'href="/wow/quester/characters">Mine' in menu and ">Fixture Guild</a>" in menu, url
    assert 'name="share_all"' in c.get("/wow/quester/characters").text


@needs_db
def test_saving_details_returns_to_the_character_with_a_note(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(96), "Zulmara Ashveil").id
    c = login(a, 96)
    r = c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["gear"]},
               follow_redirects=False)
    assert r.headers["location"].endswith(f"/characters?saved={cid}#char-{cid}")
    page = c.get(r.headers["location"]).text
    assert f'id="char-{cid}"' in page and "Saved. Imported details and sharing are updated for Zulmara Ashveil." in page
    r = c.post(f"/wow/quester/characters/{cid}/details/remove", data={"csrf": csrf(c)}, follow_redirects=False)
    assert r.headers["location"].endswith(f"?removed={cid}#char-{cid}")


@needs_db
def test_help_page_explains_character_details(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    page = login(a, 97).get("/wow/quester/help").text
    assert 'id="details"' in page and 'href="#details"' in page and "All sections" in page and "Who can see what" in page
    assert "deleted 30 days after their last refresh" in page


# ------------------------------------------------------------------ link previews of member-only pages
@needs_db
def test_link_preview_bots_get_a_generic_card_not_discord(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    cid = _shared(seeded, cfg, 98, "Preview Hunter", shares={g: "guild" for g in profiles.GROUP_KEYS})
    bot = TestClient(a, base_url="https://community.example.org",
                     headers={"user-agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"})
    r = bot.get("/wow/quester/characters", follow_redirects=False)
    assert r.status_code == 200
    assert '<meta property="og:title" content="My characters' in r.text and "import gear, talents" in r.text
    assert 'name="robots" content="noindex,nofollow"' in r.text and 'rel="canonical"' not in r.text
    g = bot.get("/wow/quester/guilds/forever/us/9001", follow_redirects=False)
    assert g.status_code == 200 and "Guild roster" in g.text and "Fixture Guild" not in g.text   # no member data
    v = bot.get(f"/wow/quester/characters/{cid}/view", follow_redirects=False)
    assert "Preview Hunter" not in v.text and "Fixture Cap" not in v.text
    human = TestClient(a, base_url="https://community.example.org", headers={"user-agent": "Mozilla/5.0 Safari"})
    assert human.get("/wow/quester/characters", follow_redirects=False).status_code == 303   # people still log in


def test_link_preview_agents_must_be_a_list():
    from conftest import make_cfg
    from questersboard.config import ConfigError
    with pytest.raises(ConfigError, match="link_preview_agents"):
        make_cfg(web={"link_preview_agents": "Discordbot"})


@needs_db
def test_character_page_refreshes_from_blizzard_with_the_same_choices(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(97), "Zulmara Ashveil").id
    c, o = login(a, 97), login(a, 98)
    url = f"/wow/quester/characters/{cid}/details/refresh"
    assert c.post(url, data={"csrf": csrf(c)}).status_code == 404                  # nothing imported yet
    c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["gear"]})
    page = c.get(f"/wow/quester/characters/{cid}/view").text
    assert f'action="{url}"' in page and "Refresh from Blizzard" in page
    assert "Check it exists on Blizzard" not in c.get("/wow/quester/characters").text   # imported: not needed
    assert o.post(url, data={"csrf": csrf(o)}).status_code == 404                  # only the owner
    r = c.post(url, data={"csrf": csrf(c)}, follow_redirects=False)
    assert r.headers["location"].endswith(f"/characters/{cid}/view?refreshed=1")
    assert "Refreshed from Blizzard." in c.get(r.headers["location"]).text
    with seeded.tx() as s:
        assert list(s.execute(text("select groups from character_profiles")).scalar()) == ["gear"]


def _crafter_groups(n, recipes):
    return {"overview": {"equipped_item_level": 20}, "guild": {"id": "9001", "name": "Fixture Guild"},
            "professions": [{"name": "Leatherworking", "kind": "primary", "skill": 95, "max": 150,
                             "recipes": [{"id": 1000 + i, "name": r} for i, r in enumerate(recipes)]}]}


@needs_db
def test_who_can_craft_searches_shared_recipes_only(seeded, tmp_path, monkeypatch):
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    guild_all = {g: "guild" for g in profiles.GROUP_KEYS}
    _shared(seeded, cfg, 90, "Belt Maker", shares=guild_all,
            groups=_crafter_groups(90, ["Fixture Leather Belt", "Fixture Boots"]))
    _shared(seeded, cfg, 91, "Boot Maker", shares=guild_all, groups=_crafter_groups(91, ["Fixture Boots"]))
    _shared(seeded, cfg, 92, "Quiet Maker", shares={"guild": "guild"},             # Professions kept private
            groups=_crafter_groups(92, ["Fixture Leather Belt"]))
    url = "/wow/quester/guilds/forever/us/9001"
    c = login(a, 90)
    page = c.get(url).text
    assert 'name="craft"' in page and '<option value="Fixture Leather Belt">' in page   # suggestions
    belt = c.get(url + "?craft=%20leather%20%20BELT%20").text                      # any case, spaces tidied
    assert "Belt Maker" in belt and "Boot Maker" not in belt and "Quiet Maker" not in belt
    assert "<th scope=\"col\">Can craft</th>" in belt and "Fixture Leather Belt <span class=\"note\">Leatherworking" in belt
    assert 'value="leather BELT"' in belt
    boots = c.get(url + "?craft=boots").text
    assert "Belt Maker" in boots and "Boot Maker" in boots and "Quiet Maker" not in boots
    none = c.get(url + "?craft=fixture%20sword").text
    assert "Nobody here has shared a recipe matching" in none and none.count("/view") == 0
    assert c.get(url + "?craft=" + "x" * 300).status_code == 200                  # long input is cut, not an error


def test_craft_query_and_filter_rows_without_a_database():
    assert profiles.craft_query("  a   b ") == "a b" and len(profiles.craft_query("y" * 200)) == 80
    assert profiles.craft_query(None) == ""
