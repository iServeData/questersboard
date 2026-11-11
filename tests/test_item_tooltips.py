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
    assert t == {"name": "Fixture Aim", "rank": 5, "spell_id": 19426, "talent_id": 134476,
                 "description": "Increases your fixture chance by 5%.", "cast_time": "Passive"}


def test_only_the_editions_static_namespace_or_a_pinned_copy_is_followed():
    assert profiles._same_static("static-forever-us", "static-forever-us")
    assert profiles._same_static(VNS, "static-forever-us")
    assert not profiles._same_static("static-classic-us", "static-forever-us")
    assert not profiles._same_static("static-1.2_3-forever-eu", "static-forever-us")
    assert not profiles._same_static("static-1.2-ptr-forever-us", "static-forever-us")
    assert not profiles._same_static(VNS, None)


def test_gear_columns_follow_the_character_sheet():
    items = [{"slot_type": "RANGED"}, {"slot_type": "BACK"}, {"slot_type": "HANDS"}, {"slot_type": "HEAD"}, {},
             {"slot": "Ring 1"}, {"slot_type": "AMMO", "slot": "Ammo"}]
    left, right, bottom = profiles.gear_columns(items)
    assert len(left) == len(right) == 8 and [x[2] for x in bottom] == ["Main Hand", "Off Hand", "Ranged", "Ammo", ""]
    assert [n for n, _, _ in left] == [3, None, None, 1, None, None, None, None]      # every slot, empty ones too
    assert [x[2] for x in left][:4] == ["Head", "Neck", "Shoulder", "Back"]
    assert right[0][0] == 2 and right[4][0] == 5                                       # old import: slot name only
    assert [n for n, _, _ in bottom] == [None, None, 0, 6, 4]                          # unlisted slots after weapons
    l2, r2, b2 = profiles.gear_columns(items, ("MAIN_HAND", "OFF_HAND", "RANGED", "AMMO"))
    assert [n for n, _, _ in b2] == [None, None, 0, 6, 4]
    _, _, b3 = profiles.gear_columns([], ("MAIN_HAND", "OFF_HAND"))
    assert [x[2] for x in b3] == ["Main Hand", "Off Hand"]                             # same shape with no gear


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
    assert page.count("/portrait/main-raw") == 1                            # not repeated in the Portrait group
    assert 'wq-placeholder" data-portrait-missing hidden' in centre          # race outline only if the picture fails
    assert c.post(f"/wow/quester/characters/{cid}/details",                # re-import without the portrait
                  data={"csrf": csrf(c), "groups": ["gear"]}).status_code == 303
    page2 = c.get(f"/wow/quester/characters/{cid}/view").text
    centre2 = page2.split('class="wq-gear-center"', 1)[1].split("</div>", 1)[0]
    assert "/portrait/" not in centre2 and "Tick Portrait and press Refresh" in centre2      # panel keeps its shape
    assert "/static/silhouettes/orc.svg" in centre2 and "data-portrait-missing hidden" not in centre2


def test_silhouette_for_every_catalog_race_and_a_generic_fallback():
    import tomllib
    from pathlib import Path
    root = Path(profiles.__file__).resolve().parents[1]
    have = frozenset(f.stem for f in (root / "web/static/silhouettes").glob("*.svg"))
    races = set()
    for f in (root / "catalog").glob("*.toml"):
        for by_faction in tomllib.loads(f.read_text()).get("races", {}).values():
            races |= set(by_faction)
    assert races and races <= have and "generic" in have
    assert profiles.silhouette_key("Night Elf", have) == "night_elf"
    assert profiles.silhouette_key("fixture_unknown", have) == "generic" == profiles.silhouette_key(None, have)
    for k in have:                                                          # strict CSP: no styles or scripts inside
        body = (root / "web/static/silhouettes" / f"{k}.svg").read_text()
        assert "<style" not in body and "style=" not in body and "<script" not in body and "href" not in body


def test_portrait_state():
    pic = {"gear": [], "portrait": [{"key": "main-raw"}]}
    assert profiles.portrait_state(pic) == "picture"
    assert profiles.portrait_state({"gear": [], "portrait": []}) == "none_yet"
    assert profiles.portrait_state({"gear": [], "portrait": []}, {"gear"}) == "hidden"   # not shared with them
    assert profiles.portrait_state({"gear": []}) == "hidden"


def _no_media_handler(req):
    if req.url.path == BASE + "/character-media":
        return httpx.Response(200, json={"assets": []})
    return _handler(req)


@needs_db
def test_no_picture_from_blizzard_yet_keeps_portrait_and_shows_the_race_outline(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _no_media_handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(143), "Zulmara Ashveil").id
    c = login(a, 143)
    assert c.post(f"/wow/quester/characters/{cid}/details",
                  data={"csrf": csrf(c), "groups": ["gear", "portrait"]}).status_code == 303
    with seeded.tx() as s:
        g = s.execute(text("select groups from character_profiles where character_id=:i"), {"i": cid}).scalar()
    assert g["portrait"] == []                                              # remembered, asked for again on refresh
    page = c.get(f"/wow/quester/characters/{cid}/view").text
    centre = page.split('class="wq-gear-center"', 1)[1].split("</div>", 1)[0]
    assert "/static/silhouettes/orc.svg" in centre and "No picture from Blizzard yet." in centre
    assert "Press Refresh from Blizzard later." in centre and "/portrait/" not in centre
    assert any(q.url.path.endswith("/character-media") for q in calls)
    assert c.post(f"/wow/quester/characters/{cid}/details/refresh", data={"csrf": csrf(c)}).status_code == 303
    with seeded.tx() as s:                                                  # Refresh keeps asking for it
        g = s.execute(text("select groups from character_profiles where character_id=:i"), {"i": cid}).scalar()
    assert g["portrait"] == [] and "gear" in g
    stranger = login(a, 144)                                                # not shared: no reason given
    other = stranger.get(f"/wow/quester/characters/{cid}/view").text
    if 'class="wq-gear-center"' in other:
        assert "No picture from Blizzard yet." not in other


# ------------------------------------------------------------------ full-body render trimmed to the figure
def _png(size, box=None, mode="RGBA"):
    import io

    from PIL import Image, ImageDraw
    im = Image.new(mode, size, (0, 0, 0, 0) if mode == "RGBA" else (0, 0, 0))
    if box:
        ImageDraw.Draw(im).rectangle(box, fill=(200, 180, 150, 255) if mode == "RGBA" else (200, 180, 150))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def test_render_is_cropped_to_the_character_with_a_small_margin():
    import io

    from PIL import Image
    body, ctype = profiles.trim_render(_png((1600, 1200), (700, 300, 899, 1099)), "image/png")
    assert ctype == "image/png"
    w, h = Image.open(io.BytesIO(body)).size
    assert 800 < h < 900 and 200 < w < 300                                  # figure 200x800 plus ~4 %
    jpg = b"\xff\xd8FIXTURE"
    assert profiles.trim_render(jpg, "image/jpeg") == (jpg, "image/jpeg")   # avatars untouched
    rgb = _png((40, 40), (5, 5, 10, 10), mode="RGB")
    assert profiles.trim_render(rgb, "image/png") == (rgb, "image/png")     # no transparency: unchanged
    empty = _png((40, 40))
    assert profiles.trim_render(empty, "image/png") == (empty, "image/png")
    assert profiles.trim_render(b"not a png", "image/png") == (b"not a png", "image/png")


@needs_db
def test_pets_are_their_own_group_with_their_own_sharing(seeded, tmp_path, monkeypatch):
    def h(req):
        if req.url.path == BASE + "/hunter-pets":
            return httpx.Response(200, json={"hunter_pets": [{"name": "Fixture Pet", "level": 17,
                                                              "creature_family": {"name": "Fixture Family"}}]})
        return _handler(req)
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, h)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(143), "Zulmara Ashveil").id
    c = login(a, 143)
    me = c.get("/wow/quester/characters").text
    assert 'value="pets"' in me and 'name="share_pets"' in me
    assert c.post(f"/wow/quester/characters/{cid}/details",
                  data={"csrf": csrf(c), "groups": ["pets", "progress"]}).status_code == 303
    page = c.get(f"/wow/quester/characters/{cid}/view").text
    assert "<summary>Pets" in page and "Fixture Pet" in page and "Fixture Family · level 17" in page
    assert profiles.GROUP_KEYS.index("progress") + 1 == profiles.GROUP_KEYS.index("pets")


@needs_db
def test_refresh_all_and_one_sharing_choice_for_every_character(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(144), "Zulmara Ashveil").id
    c = login(a, 144)
    assert "Refresh all from Blizzard" not in c.get("/wow/quester/characters").text     # nothing imported yet
    c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["gear", "talents"],
                                                           "share_gear": "private", "share_talents": "private"})
    page = c.get("/wow/quester/characters").text
    assert "Refresh all from Blizzard" in page and "Who can see all of them" in page

    r = c.post("/wow/quester/characters/details/all", data={"csrf": csrf(c), "action": "share", "share_level": ""})
    assert r.status_code == 303 and r.headers["location"].endswith("bulk=pick")     # no level: back, nothing changed
    assert "Choose who can see them first" in c.get(r.headers["location"]).text
    assert "data-bulk-level" in page and "data-bulk-share" in page
    calls.clear()
    r = c.post("/wow/quester/characters/details/all", data={"csrf": csrf(c), "action": "share", "share_level": "members"})
    assert r.status_code == 303 and "bulk=shared&n=1" in r.headers["location"]
    assert not calls                                                           # sharing only: no Blizzard calls
    with seeded.tx() as s:
        sh, t1 = s.execute(text("select shares, fetched_at from character_profiles where character_id=:i"),
                           {"i": cid}).one()
    assert sh == {"gear": "members", "talents": "members"}
    assert "Sharing updated for 1 character." in c.get(r.headers["location"]).text

    r = c.post("/wow/quester/characters/details/all", data={"csrf": csrf(c), "action": "refresh", "share_level": ""})
    assert r.status_code == 303 and "bulk=refreshed&n=1&failed=0" in r.headers["location"]
    with seeded.tx() as s:
        g, sh, t2 = s.execute(text("select groups, shares, fetched_at from character_profiles where character_id=:i"),
                              {"i": cid}).one()
    assert set(g) == {"gear", "talents"} and sh == {"gear": "members", "talents": "members"} and t2 >= t1
    page = c.get(r.headers["location"]).text
    assert "Refreshed 1 character from Blizzard." in page
    assert f'action="/wow/quester/characters/{cid}/details"' in page and 'name="share_gear"' in page  # still per character
    assert c.post("/wow/quester/characters/details/all",
                  data={"csrf": csrf(c), "action": "share", "share_level": "everyone"}).status_code in (400, 422)


def test_bulk_refresh_keeps_groups_split_out_later():
    class P:
        groups = {"gear": [], "progress": {}}
        shares = {"progress": "members"}
    assert profiles._bulk_groups(P()) == {"gear", "progress", "pets"}
    assert profiles.share_choice(P(), "pets") == "members" and profiles.share_choice(None, "pets") == "private"


@needs_db
def test_hunter_pets_show_blizzards_picture_through_questersboard(seeded, tmp_path, monkeypatch):
    def h(req):
        p = req.url.path
        if p == BASE + "/hunter-pets":
            return httpx.Response(200, json={"hunter_pets": [
                {"name": "Fixture Pet", "level": 17, "creature": {"name": "Fixture Beast"},
                 "creature_display": {"id": 4242}},
                {"name": "Fixture Plain", "level": 9}]})                       # no display: text only
        if p == "/data/wow/media/creature-display/4242" and req.url.params.get("namespace") == "static-forever-us":
            return httpx.Response(200, json={"assets": [{"key": "zoom", "value": f"{ICON_HOST}/creature-display-4242.jpg"}]})
        if p.endswith("/creature-display-4242.jpg"):
            return httpx.Response(200, content=b"\xff\xd8PET", headers={"content-type": "image/jpeg"})
        return _handler(req)
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, h)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(145), "Zulmara Ashveil").id
    c = login(a, 145)
    assert c.post(f"/wow/quester/characters/{cid}/details",
                  data={"csrf": csrf(c), "groups": ["pets"]}).status_code == 303
    with seeded.tx() as s:
        g = s.execute(text("select groups from character_profiles where character_id=:i"), {"i": cid}).scalar()
    assert "_display" not in str(g) and g["pets"][0]["icon"].endswith("creature-display-4242.jpg")
    page = c.get(f"/wow/quester/characters/{cid}/view").text
    assert page.count('class="wq-pet-img"') == 1 and f"/wow/quester/characters/{cid}/icon/pets/0?v=" in page
    r = c.get(f"/wow/quester/characters/{cid}/icon/pets/0")
    assert r.status_code == 200 and r.content == b"\xff\xd8PET"
    assert c.get(f"/wow/quester/characters/{cid}/icon/pets/1").status_code == 404
    assert login(a, 146).get(f"/wow/quester/characters/{cid}/icon/pets/0").status_code == 404   # Only me


# ------------------------------------------------------------------ Classic-style talent trees (FIXTURE trees)
SNS = "static-forever-us"


def _rank(tid, name, sid, desc):
    return {"tooltip": {"talent": {"id": tid, "name": name},
                        "spell_tooltip": {"spell": {"id": sid, "name": name}, "description": desc}}}


def _tree_handler(req):
    p, ns = req.url.path, req.url.params.get("namespace")
    tree = f"https://us.api.blizzard.com/data/wow/talent-tree/%s?namespace={VNS}"
    if p == "/data/wow/talent-tree/index" and ns == SNS:
        return httpx.Response(200, json={"class_talent_trees": [
            {"name": "Mage", "key": {"href": tree % "900"}},
            {"name": "Warrior", "key": {"href": tree % "901"}}]})
    if p == "/data/wow/talent-tree/901" and ns == VNS:
        return httpx.Response(200, json={"spec_talent_trees": [
            {"name": "Fixture Arms", "key": {"href": tree % "901/playable-specialization/71"}},
            {"name": "Fixture Fury", "key": {"href": tree % "901/playable-specialization/72"}}],
            "talent_nodes": [{"id": 2, "locked_by": [1]}]})
    if p == "/data/wow/talent-tree/901/playable-specialization/71":
        return httpx.Response(200, json={"playable_specialization": {"name": "Fixture Arms"}, "spec_talent_nodes": [
            {"id": 1, "display_row": 1, "display_col": 1,
             "ranks": [_rank(134476, "Fixture Aim", 19426, f"Aim {i}%.") for i in (5, 10, 15, 20, 25)]},
            {"id": 2, "display_row": 3, "display_col": 1, "ranks": [_rank(134477, "Fixture Strike", 19427, "Strikes.")]}]})
    if p == "/data/wow/talent-tree/901/playable-specialization/72":
        return httpx.Response(200, json={"playable_specialization": {"name": "Fixture Fury"}, "spec_talent_nodes": [
            {"id": 3, "display_row": 1, "display_col": 7, "ranks": [_rank(134478, "Fixture Rage", 19428, "Rage.")] * 2},
            # Past the old four-column block (columns 5-8): this one used to be dropped. Its talent id
            # doesn't match the character's, but its spell does.
            {"id": 4, "display_row": 2, "display_col": 11,
             "ranks": [_rank(134499, "Fixture Blitz", 19426, f"Blitz {i}.") for i in (1, 2, 3)]}]})
    return _handler(req)


@needs_db
def test_talents_are_drawn_as_trees_from_blizzards_layout(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _tree_handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(147), "Zulmara Ashveil").id
    c = login(a, 147)
    assert c.post(f"/wow/quester/characters/{cid}/details",
                  data={"csrf": csrf(c), "groups": ["talents"], "share_talents": "members"}).status_code == 303
    with seeded.tx() as s:
        t = s.execute(text("select groups from character_profiles where character_id=:i"), {"i": cid}).scalar()["talents"]
    arms, fury = t["trees"]
    assert arms["name"] == "Fixture Arms" and arms["rows"] == 3 and arms["points"] == 5
    aim, strike = arms["nodes"]
    assert (aim["row"], aim["col"], aim["rank"], aim["max"]) == (1, 1, 5, 5) and aim["description"] == "Aim 25%."
    assert strike["rank"] == 0 and strike["requires"] == ["Fixture Aim"] and "next" not in strike
    assert fury["nodes"][0]["col"] == 1 and fury["points"] == 3          # its own leftmost column is column 1
    assert fury["cols"] == arms["cols"] == 5 and fury["nodes"][1]["col"] == 5    # widened, nothing cut off
    assert fury["nodes"][1]["rank"] == 3 and fury["nodes"][1]["description"] == "Blitz 3."   # matched by spell
    assert aim["icon"].endswith("/132312.jpg")                           # spell media, like the talent list
    page = c.get(f"/wow/quester/characters/{cid}/view").text
    assert page.count('class="wq-tree"') == 2 and "wq-r3 wq-c1" in page and ">5/5<" in page and ">0/1<" in page
    assert "Requires Fixture Aim" in page and 'class="wq-talents"' not in page        # trees replace the list
    assert c.get(f"/wow/quester/characters/{cid}/icon/tree/0-0").status_code == 200
    assert c.get(f"/wow/quester/characters/{cid}/icon/tree/0-1").status_code == 404   # no icon for that spell
    assert c.get(f"/wow/quester/characters/{cid}/icon/tree/9-9").status_code == 404


@needs_db
def test_trees_are_only_as_wide_as_their_talents(seeded, tmp_path, monkeypatch):
    # FIXTURE: both trees use two columns while the edition allows four, so the grid is two wide
    # (a wider grid left the talents hugging the left of their frame).
    def narrow(req):
        p = req.url.path
        if p == "/data/wow/talent-tree/901/playable-specialization/72":
            return httpx.Response(200, json={"playable_specialization": {"name": "Fixture Fury"}, "spec_talent_nodes": [
                {"id": 3, "display_row": 1, "display_col": 7, "ranks": [_rank(134478, "Fixture Rage", 19428, "Rage.")]},
                {"id": 4, "display_row": 2, "display_col": 8, "ranks": [_rank(134499, "Fixture Blitz", 19426, "Blitz.")]}]})
        if p == "/data/wow/talent-tree/901/playable-specialization/71":
            return httpx.Response(200, json={"playable_specialization": {"name": "Fixture Arms"}, "spec_talent_nodes": [
                {"id": 1, "display_row": 1, "display_col": 1, "ranks": [_rank(134476, "Fixture Aim", 19426, "Aim.")]},
                {"id": 2, "display_row": 2, "display_col": 2, "ranks": [_rank(134477, "Fixture Strike", 19427, "Strikes.")]}]})
        return _tree_handler(req)
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, narrow)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(148), "Zulmara Ashveil").id
    c = login(a, 148)
    c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["talents"]})
    with seeded.tx() as s:
        t = s.execute(text("select groups from character_profiles where character_id=:i"), {"i": cid}).scalar()["talents"]
    assert [tr["cols"] for tr in t["trees"]] == [2, 2]
    assert 'wq-cols-2 ' in c.get(f"/wow/quester/characters/{cid}/view").text


@needs_db
def test_operator_refresh_rebuilds_everyones_details_with_their_sharing(seeded, tmp_path, monkeypatch):
    # FIXTURE: the server-side refresh-details command does what each owner's Refresh would do.
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _tree_handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(149), "Zulmara Ashveil").id
        other = char(s, cfg, actor(150), "Fixture Other").id          # nothing imported: left alone
    c = login(a, 149)
    c.post(f"/wow/quester/characters/{cid}/details",
           data={"csrf": csrf(c), "groups": ["talents"], "share_talents": "members"})
    with seeded.tx() as s:          # make it look like data stored by an older release
        s.execute(text("update character_profiles set groups = jsonb_set(groups, '{talents,trees}', '[]') "
                       "where character_id=:i"), {"i": cid})
        ids = [r[0] for r in profiles.operator_refresh_list(s, cfg)]
    assert cid in ids and other not in ids
    with seeded.tx() as s:
        profiles.operator_refresh(s, cfg, a.state.web.bz, cid)
    with seeded.tx() as s:
        g, sh = s.execute(text("select groups, shares from character_profiles where character_id=:i"), {"i": cid}).one()
    assert [t["name"] for t in g["talents"]["trees"]] == ["Fixture Arms", "Fixture Fury"] and sh == {"talents": "members"}


def test_talent_tree_columns_are_validated():
    from pathlib import Path
    import tomllib
    raw = tomllib.loads((Path(__file__).resolve().parents[1] / "deploy" / "config.example.toml").read_text())
    assert make_cfg().edition("forever").talent_tree_columns == 4 and make_cfg().edition("retail").talent_tree_columns == 0
    for bad in (1, 9, True, "4"):
        r = copy.deepcopy(raw)
        r["editions"][0]["talent_tree_columns"] = bad
        with pytest.raises(ConfigError, match="talent_tree_columns"):
            validate(r)


def test_pet_pictures_get_a_transparent_backdrop():
    import io

    from PIL import Image, ImageDraw
    im = Image.new("RGB", (400, 300), (24, 24, 24))
    d = ImageDraw.Draw(im)
    d.ellipse((150, 100, 250, 220), fill=(200, 150, 90))
    d.rectangle((190, 150, 210, 170), fill=(24, 24, 24))         # a dark patch inside the animal
    raw = io.BytesIO()
    im.save(raw, "JPEG", quality=95)
    body, ctype = profiles.cut_backdrop(raw.getvalue(), "image/jpeg")
    out = Image.open(io.BytesIO(body))
    assert ctype == "image/png" and out.mode == "RGBA" and 100 <= out.width <= 115
    assert out.getpixel((0, 0))[3] == 0                                    # backdrop: transparent
    assert out.getpixel((out.width // 2, out.height // 2))[3] == 255      # inside patch: kept
    assert profiles.cut_backdrop(b"junk", "image/jpeg") == (b"junk", "image/jpeg")
    flat = io.BytesIO()
    Image.new("RGB", (50, 50), (9, 9, 9)).save(flat, "JPEG")             # nothing to cut out
    assert profiles.cut_backdrop(flat.getvalue(), "image/jpeg")[0] == flat.getvalue()


def test_talents_are_placed_by_their_block_of_the_class_grid():
    # FIXTURE shaped like WoW: Forever's hunter pages: three 4-column trees (columns 1-4, 7-10, 13-16),
    # where the first two pages also carry the next tree's leftmost column.
    def nodes(start, rows=3):
        return [{"id": start * 100 + r * 10 + c, "display_row": r, "display_col": start + c}
                for r in range(1, rows + 1) for c in range(4)]
    a, b, c = nodes(1), nodes(7), nodes(13)
    left = lambda ns: [n for n in ns if n["display_col"] in (7, 13)]           # noqa: E731
    pages = [("a", {}, a + left(b)), ("b", {}, [n for n in b if n["display_col"] != 7] + left(c)),
             ("c", {}, [n for n in c if n["display_col"] != 13])]
    out = profiles._regroup_by_block(pages)
    assert [r for r, _b, _n in out] == ["a", "b", "c"]
    for (_r, _b, ns), want in zip(out, (a, b, c)):
        assert sorted(n["id"] for n in ns) == sorted(n["id"] for n in want)
    # A tree with an empty column inside it stays together.
    gap = [("a", {}, [n for n in nodes(1) if n["display_col"] != 3]), ("b", {}, nodes(9))]
    assert [len(ns) for _r, _b, ns in profiles._regroup_by_block(gap)] == [9, 12]
    # Blocks that don't line up with the pages: drawn as sent.
    odd = [("a", {}, nodes(1)), ("b", {}, nodes(5))]                           # columns 1-8, no gap
    assert profiles._regroup_by_block(odd) is odd
