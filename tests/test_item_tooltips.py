"""Gear and talent icons and tooltips from Blizzard's own answers. All Blizzard answers here are
FIXTURES shaped like the WoW: Forever US answers seen by `blizzard probe-media`; names are made up."""
from __future__ import annotations

import copy

import httpx
import pytest
from conftest import actor, make_cfg, needs_db
from helpers import char
from sqlalchemy import text
from test_web import _bz_app, csrf, login

from questersboard.config import ConfigError, validate
from questersboard.domain import profiles

BASE = "/profile/wow/character/zulmara-ashveil"
VNS = "static-1.60.1_70399-forever-us"          # Forever links item media to a version-pinned namespace
ICON_HOST = "https://render.worldofwarcraft.com/forever-us/icons/56"

CLOAK = {"item": {"id": 9101}, "slot": {"type": "BACK", "name": "Back"}, "quantity": 1,
         "quality": {"type": "UNCOMMON", "name": "Uncommon"}, "name": "Fixture Cloak",
         "media": {"key": {"href": f"https://us.api.blizzard.com/data/wow/media/item/9101?namespace={VNS}"}, "id": 9101},
         "item_class": {"name": "Armor", "id": 4}, "item_subclass": {"name": "Cloth", "id": 1},
         "inventory_type": {"type": "CLOAK", "name": "Back"},
         "binding": {"type": "ON_EQUIP", "name": "Binds when equipped"},
         "armor": {"value": 18, "display": {"display_string": "18 Armor"}},
         "stats": [{"type": {"type": "AGILITY"}, "value": 3, "display": {"display_string": "+3 Agility"}},
                   {"type": {"type": "STRENGTH"}, "value": 2, "display": {"display_string": "+2 Strength"}}],
         "sell_price": {"value": 408, "display_strings": {"header": "Sell Price:", "gold": "0", "silver": "4", "copper": "8"}},
         "requirements": {"level": {"value": 17, "display_string": "Requires Level 17"}},
         "level": {"value": 22, "display_string": "Item Level 22"},
         "transmog": {"item": {"name": "Fixture Skyseer Cloak", "id": 9999}, "display_string": "Transmogrified to:"},
         "is_subclass_hidden": True}
BOW = {"item": {"id": 9102}, "slot": {"type": "RANGED", "name": "Ranged"}, "name": "Fixture Bow",
       "quality": {"type": "RARE"}, "level": {"value": 20, "display_string": "Item Level 20"},
       "inventory_type": {"name": "Ranged"}, "item_subclass": {"name": "Bow"},
       "weapon": {"damage": {"display_string": "14 - 27 Damage"}, "attack_speed": {"display_string": "Speed 2.80"},
                  "dps": {"display_string": "(7.3 damage per second)"}},
       "spells": [{"description": "Equip: Fixture effect."}],
       "media": {"key": {"href": "https://us.api.blizzard.com/data/wow/media/item/9102?namespace=static-classic-us"}}}
HEAD = {"item": {"id": 9103}, "slot": {"type": "HEAD", "name": "Head"}, "name": "Fixture Cap",
        "quality": {"type": "COMMON"}, "level": {"value": 9}}
SPECS = {"specialization_groups": [{"is_active": True, "specializations": [
    {"specialization_name": "Marksmanship", "spent_points": 5, "talents": [
        {"talent": {"name": "Fixture Aim", "id": 134476}, "talent_rank": 5,
         "spell_tooltip": {"spell": {"name": "Fixture Aim", "id": 19426},
                           "description": "Increases your fixture chance by 5%.", "cast_time": "Passive"}}]}]}]}


def _handler(req):
    p, ns = req.url.path, req.url.params.get("namespace")
    if p == BASE:
        return httpx.Response(200, json={"id": 501, "name": "Zulmara Ashveil", "level": 22})
    if p == BASE + "/equipment":
        return httpx.Response(200, json={"equipped_items": [BOW, CLOAK, HEAD]})
    if p == BASE + "/specializations":
        return httpx.Response(200, json=SPECS)
    if p == "/data/wow/media/item/9101" and ns == VNS:
        return httpx.Response(200, json={"assets": [{"key": "icon", "value": f"{ICON_HOST}/133762.jpg"}]})
    if p == "/data/wow/media/spell/19426" and ns == "static-forever-us":
        return httpx.Response(200, json={"assets": [{"key": "icon", "value": f"{ICON_HOST}/132312.jpg"}]})
    if p.startswith("/forever-us/icons/56/"):
        return httpx.Response(200, content=b"\xff\xd8FIXTURE", headers={"content-type": "image/jpeg"})
    return httpx.Response(404)


# ------------------------------------------------------------------ extraction (no database)
def test_gear_keeps_tooltip_lines_and_the_media_link():
    gear = profiles.extract("gear", {"/equipment": {"equipped_items": [CLOAK, BOW]}})
    cloak, bow = gear
    assert cloak["slot_type"] == "BACK" and cloak["_media"] == {"path": "/data/wow/media/item/9101", "ns": VNS}
    t = cloak["tip"]
    assert t["binding"] == "Binds when equipped" and t["type"] == "Back" and "subtype" not in t   # hidden for cloaks
    assert t["armor"] == "18 Armor" and [x["text"] for x in t["stats"]] == ["+3 Agility", "+2 Strength"]
    assert t["requirements"] == ["Requires Level 17"] and t["level"] == "Item Level 22"
    assert t["sell"] == {"gold": "0", "silver": "4", "copper": "8"} and t["transmog"] == "Fixture Skyseer Cloak"
    b = bow["tip"]
    assert b["subtype"] == "Bow" and b["damage"] == "14 - 27 Damage" and b["speed"] == "Speed 2.80"
    assert b["dps"] == "(7.3 damage per second)" and b["spells"] == ["Equip: Fixture effect."]


def test_talents_keep_spell_tooltips():
    t = profiles.extract("talents", {"/specializations": SPECS})["groups"][0]["specs"][0]["talents"][0]
    assert t == {"name": "Fixture Aim", "rank": 5, "spell_id": 19426,
                 "description": "Increases your fixture chance by 5%.", "cast_time": "Passive"}


def test_only_the_editions_static_namespace_or_a_pinned_copy_is_followed():
    assert profiles._same_static("static-forever-us", "static-forever-us")
    assert profiles._same_static(VNS, "static-forever-us")
    assert not profiles._same_static("static-classic-us", "static-forever-us")
    assert not profiles._same_static("static-1.2_3-forever-eu", "static-forever-us")
    assert not profiles._same_static("static-1.2-ptr-forever-us", "static-forever-us")
    assert not profiles._same_static(VNS, None)


def test_gear_columns_follow_the_character_sheet():
    items = [{"slot_type": "RANGED"}, {"slot_type": "BACK"}, {"slot_type": "HANDS"}, {"slot_type": "HEAD"}, {}]
    left, right, bottom = profiles.gear_columns(items)
    assert [n for n, _ in left] == [3, 1] and [n for n, _ in right] == [2] and [n for n, _ in bottom] == [0, 4]


def test_icon_url_reads_gear_and_talent_positions():
    gear = [{"icon": "https://render.worldofwarcraft.com/a.jpg"}, {}]
    tal = {"groups": [{"specs": [{"talents": [{"icon": "https://render.worldofwarcraft.com/b.jpg"}]}]}]}
    assert profiles.icon_url(gear, "gear", "0").endswith("a.jpg")
    assert profiles.icon_url(gear, "gear", "1") is None and profiles.icon_url(gear, "gear", "9") is None
    assert profiles.icon_url(tal, "talents", "0-0-0").endswith("b.jpg")
    assert profiles.icon_url(tal, "talents", "0-1-0") is None and profiles.icon_url(tal, "gear", "0-0-0") is None


def test_link_templates_and_icon_settings_are_validated():
    from pathlib import Path
    import tomllib
    raw = tomllib.loads((Path(__file__).resolve().parents[1] / "deploy" / "config.example.toml").read_text())
    ok = make_cfg()
    ed = ok.edition("forever")
    assert ed.item_link(9101) == "https://www.wowhead.com/forever/item=9101"
    assert ed.spell_link(19426) == "https://www.wowhead.com/forever/spell=19426" and ed.item_link(None) == ""
    for bad in ("http://www.wowhead.com/item={id}", "https://www.wowhead.com/item=", "javascript:alert({id})"):
        r = copy.deepcopy(raw)
        r["editions"][0]["item_link_template"] = bad
        with pytest.raises(ConfigError):
            validate(r)
    r = copy.deepcopy(raw)
    r["blizzard"]["icon_cache_hours"] = 48
    with pytest.raises(ConfigError):
        validate(r)


# ------------------------------------------------------------------ web + database
@needs_db
def test_import_shows_icons_and_tooltips_served_by_questersboard(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(140), "Zulmara Ashveil").id
    c = login(a, 140)
    r = c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["gear", "talents"]})
    assert r.status_code == 303, r.text
    with seeded.tx() as s:
        groups = s.execute(text("select groups from character_profiles where character_id=:i"), {"i": cid}).scalar_one()
    bow, cloak, head = groups["gear"]
    assert cloak["icon"] == f"{ICON_HOST}/133762.jpg" and "_media" not in cloak
    assert "icon" not in bow and "icon" not in head          # other edition's namespace / no media link
    assert groups["talents"]["groups"][0]["specs"][0]["talents"][0]["icon"] == f"{ICON_HOST}/132312.jpg"
    media = [q for q in calls if q.url.path.startswith("/data/wow/media/")]
    assert all(q.url.params["namespace"] in (VNS, "static-forever-us") for q in media)

    page = c.get(f"/wow/quester/characters/{cid}/view").text
    assert f"/wow/quester/characters/{cid}/icon/gear/1" in page and "render.worldofwarcraft.com" not in page
    assert f"/wow/quester/characters/{cid}/icon/talents/0-0-0" in page
    assert "18 Armor" in page and "+3 Agility" in page and "Requires Level 17" in page and "Item Level 22" in page
    assert "Fixture Skyseer Cloak" in page and "14 - 27 Damage" in page and "Equip: Fixture effect." in page
    assert "Increases your fixture chance by 5%." in page and "Rank 5" in page
    assert 'href="https://www.wowhead.com/forever/item=9101"' in page and "wowhead.com/forever/spell=19426" in page
    assert "<script src=\"https://" not in page                              # links only, no third-party script
    assert page.index("Fixture Cap") < page.index("Fixture Cloak") < page.index("Fixture Bow")   # sheet order

    calls.clear()
    r1 = c.get(f"/wow/quester/characters/{cid}/icon/gear/1")
    r2 = c.get(f"/wow/quester/characters/{cid}/icon/gear/1")
    assert r1.status_code == r2.status_code == 200 and r1.headers["content-type"] == "image/jpeg"
    assert r1.headers["cache-control"].startswith("private")
    assert len([q for q in calls if "/icons/" in q.url.path]) == 1            # second one from the memory cache
    assert c.get(f"/wow/quester/characters/{cid}/icon/gear/0").status_code == 404      # no icon stored
    assert c.get(f"/wow/quester/characters/{cid}/icon/stats/0").status_code == 404
    assert c.get(f"/wow/quester/characters/{cid}/icon/gear/x1").status_code == 404
    stranger = login(a, 141)                                                 # nothing shared with them
    assert stranger.get(f"/wow/quester/characters/{cid}/icon/gear/1").status_code == 404


@needs_db
def test_detail_icons_off_skips_media_lookups(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    cfg["blizzard"]["detail_icons"] = False
    with seeded.tx() as s:
        cid = char(s, cfg, actor(142), "Zulmara Ashveil").id
    c = login(a, 142)
    assert c.post(f"/wow/quester/characters/{cid}/details",
                  data={"csrf": csrf(c), "groups": ["gear", "talents"]}).status_code == 303
    assert not [q for q in calls if q.url.path.startswith("/data/wow/media/")]
    with seeded.tx() as s:
        groups = s.execute(text("select groups from character_profiles where character_id=:i"), {"i": cid}).scalar_one()
    assert not any("icon" in i or "_media" in i for i in groups["gear"]) and groups["gear"][1]["tip"]["armor"]


# ------------------------------------------------------------------ portrait between the gear columns
def test_gear_portrait_needs_both_groups_visible_and_prefers_the_full_body_render():
    g = {"gear": [], "portrait": [{"key": "avatar", "url": "x"}, {"key": "main-raw", "url": "y"}]}
    assert profiles.gear_portrait_key(g) == "main-raw"
    assert profiles.gear_portrait_key(g, {"gear", "portrait"}) == "main-raw"
    assert profiles.gear_portrait_key(g, {"gear"}) is None              # portrait not shared with this viewer
    assert profiles.gear_portrait_key({"gear": []}) is None
    assert profiles.gear_portrait_key({"gear": [], "portrait": [{"key": "inset"}, {"key": "avatar"}]}) == "inset"
    assert profiles.gear_portrait_key({"gear": [], "portrait": [{"key": "other"}]}) is None


def _media_handler(req):
    if req.url.path == BASE + "/character-media":
        return httpx.Response(200, json={"assets": [{"key": "avatar", "value": f"{ICON_HOST}/fixture-avatar.jpg"},
                                                    {"key": "main-raw", "value": f"{ICON_HOST}/fixture-main-raw.png"}]})
    return _handler(req)


@needs_db
def test_portrait_sits_between_the_gear_columns(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _media_handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(142), "Zulmara Ashveil").id
    c = login(a, 142)
    assert c.post(f"/wow/quester/characters/{cid}/details",
                  data={"csrf": csrf(c), "groups": ["gear", "portrait"]}).status_code == 303
    page = c.get(f"/wow/quester/characters/{cid}/view").text
    assert 'class="wq-gear has-portrait"' in page and "Shown in the middle of Gear." in page
    centre = page.split('class="wq-gear-center"', 1)[1].split("</div>", 1)[0]
    assert f"/wow/quester/characters/{cid}/portrait/main-raw" in centre
    assert page.index("Fixture Cloak") < page.index("wq-gear-center") < page.index("Fixture Bow")  # left, centre, weapons
    assert page.count("wq-gear-portrait") == 1                             # not repeated in the Portrait group
    assert c.post(f"/wow/quester/characters/{cid}/details",                # re-import without the portrait
                  data={"csrf": csrf(c), "groups": ["gear"]}).status_code == 303
    page2 = c.get(f"/wow/quester/characters/{cid}/view").text
    assert "wq-gear-center" not in page2 and 'class="wq-gear"' in page2
