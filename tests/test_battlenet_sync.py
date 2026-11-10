"""Battle.net character sync (WoW: Forever, realmless). FIXTURE transport only: no live Blizzard API.

The account list shape follows community reports from after the Forever launch (Nov 2026): characters
carry `name` + `surname` and no realm; the ruleset comes as {id, name} where present.
"""
from __future__ import annotations

import re

import pytest
from conftest import actor, make_cfg, needs_db
from helpers import char
from sqlalchemy import text

from questersboard.config import ConfigError
from questersboard.integrations.blizzard import char_slug, ruleset_key

ACCOUNT = {"wow_accounts": [{"characters": [
    {"id": 501, "name": "Grimtusk", "surname": "Ironhide", "level": 22,
     "playable_race": {"name": "Orc"}, "playable_class": {"name": "Warrior"}, "faction": {"type": "HORDE", "name": "Horde"}},
    {"id": 502, "name": "Zulmara", "surname": "Ashveil", "level": 15, "ruleset": {"id": 3, "name": "Roleplay"},
     "playable_race": {"name": "Troll"}, "playable_class": {"name": "Priest"}, "faction": {"type": "HORDE", "name": "Horde"}},
    {"id": 503, "name": "Vashka", "surname": "Duskmane", "level": 11,
     "playable_race": {"name": "Tauren"}, "playable_class": {"name": "Druid"}, "faction": {"type": "HORDE", "name": "Horde"}},
    {"id": 504, "name": "Corwin", "surname": "Brightmere", "level": 30, "ruleset": {"id": 99, "name": "Mystery"},
     "playable_race": {"name": "Human"}, "playable_class": {"name": "Mage"}, "faction": {"type": "ALLIANCE", "name": "Alliance"}},
    {"id": 505, "name": "Elspeth", "surname": "Rowan", "level": 12},          # nothing but the name yet
    {"id": 506, "name": "Zulma4C7D21", "level": 14,                           # placeholder: awaiting a rename
     "playable_race": {"name": "Troll"}, "playable_class": {"name": "Mage"}, "faction": {"type": "HORDE", "name": "Horde"}},
]}]}


def _handler(calls_seen):
    import httpx

    def h(req):
        calls_seen.append(req.url.path)
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "FIXTURE-user", "expires_in": 86399})
        if req.url.path == "/profile/user/wow":
            assert req.headers["authorization"] == "Bearer FIXTURE-user"
            assert req.url.params["namespace"] == "profile-forever-us"
            return httpx.Response(200, json=ACCOUNT)
        if req.url.path == "/profile/wow/character/vashka-duskmane":
            assert req.headers["authorization"] == "Bearer FIXTURE-app"         # app token, not the user's
            return httpx.Response(200, json={"id": 503, "ruleset": {"id": 15, "name": "Roleplay PvP"}})
        return httpx.Response(404)
    return h


def _link(a, c):
    r = c.get("/wow/quester/characters/battlenet?edition=forever&region=us")
    assert r.status_code == 303 and "scope=wow.profile" in r.headers["location"]
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    return c.get(f"/wow/quester/battlenet/callback?code=FIXTURE-code&state={state}")


@needs_db
def test_forever_sync_verifies_offers_and_imports(seeded, tmp_path, monkeypatch):
    from test_web import _bz_app, csrf, login
    seen = []
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _handler(seen))
    with seeded.tx() as s:
        mine = char(s, cfg, actor(60), "Grimtusk Ironhide", level=12).id
    c = login(a, 60)
    assert "Sync from Battle.net" in c.get("/wow/quester/characters").text
    r = _link(a, c)
    assert r.status_code == 200, r.text
    page = r.text
    assert "Already here, now verified" in page and "Grimtusk Ironhide" in page
    for n in ("Zulmara Ashveil", "Vashka Duskmane", "Corwin Brightmere", "Elspeth Rowan"):
        assert n in page
    assert "level 10" in page                                  # the Forever visibility caveat is shown
    with seeded.tx() as s:
        v = s.execute(text("select verification, level, api_external_id from characters where id=:i"),
                      {"i": mine}).one()
        snap = bytes(s.execute(text("select snapshot_enc from battlenet_links")).scalar_one())
    assert v == ("ownership_verified", 22, "501")             # verified and level brought up to date
    assert b"Zulmara" not in snap and b"503" not in snap        # encrypted at rest
    # the ruleset select is prefilled from Blizzard's ID (3 = rp; 15 via the character profile = rp_pvp)
    idx = {n: re.search(rf'name="pick" value="(\d+)"[^>]*>\s*<strong>{n}', page).group(1)
           for n in ("Zulmara Ashveil", "Vashka Duskmane", "Corwin Brightmere")}
    assert re.search(rf'name="ruleset_{idx["Zulmara Ashveil"]}">.*?value="rp" selected', page, re.S)
    assert re.search(rf'name="ruleset_{idx["Vashka Duskmane"]}">.*?value="rp_pvp" selected', page, re.S)
    assert re.search(r'value="\d+" disabled>\s*<strong>Elspeth Rowan', page)      # can't be added yet
    assert re.search(r'value="(\d+)" disabled>\s*<strong>Zulma4C7D21', page) and "temporary name" in page
    z = re.search(r'value="(\d+)" disabled>\s*<strong>Zulma4C7D21', page).group(1)
    form = {"csrf": csrf(c), "region": "us", "pick": [*idx.values(), z],
            f"ruleset_{idx['Zulmara Ashveil']}": "rp", f"ruleset_{idx['Vashka Duskmane']}": "rp_pvp",
            f"ruleset_{idx['Corwin Brightmere']}": "normal"}
    r = c.post("/wow/quester/characters/battlenet/import", data=form)
    assert r.status_code == 200 and "Added" in r.text and "Elspeth Rowan" in r.text   # one left on offer
    with seeded.tx() as s:
        rows = {n: (rs, f, rc, lv, ver, ext) for n, rs, f, rc, lv, ver, ext in s.execute(text(
            "select name, ruleset, faction, race_key || '/' || class_key, level, verification, api_external_id "
            "from characters where member_id=(select member_id from characters where id=:i) order by name"),
            {"i": mine}).all()}
    assert rows["Zulmara Ashveil"] == ("rp", "horde", "troll/priest", 15, "ownership_verified", "502")
    assert rows["Vashka Duskmane"] == ("rp_pvp", "horde", "tauren/druid", 11, "ownership_verified", "503")
    assert rows["Corwin Brightmere"] == ("normal", "alliance", "human/mage", 30, "ownership_verified", "504")
    assert "Zulma4C7D21" not in rows                               # a forced-rename placeholder is never added
    # sending the same page again adds nothing twice
    r = c.post("/wow/quester/characters/battlenet/import", data=form)
    assert r.status_code == 200
    with seeded.tx() as s:
        assert s.execute(text("select count(*) from characters where api_external_id in ('502','503','504')")
                         ).scalar_one() == 3
    assert "/profile/wow/character/vashka-duskmane" in seen and "/profile/wow/character/zulmara-ashveil" not in seen


@needs_db
def test_forever_sync_other_member_and_expiry(seeded, tmp_path, monkeypatch):
    from test_web import _bz_app, csrf, login
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _handler([]))
    c = login(a, 61)
    page = _link(a, c).text
    i = re.search(r'name="pick" value="(\d+)"[^>]*>\s*<strong>Zulmara Ashveil', page).group(1)
    assert c.post("/wow/quester/characters/battlenet/import",
                  data={"csrf": csrf(c), "region": "us", "pick": [i], f"ruleset_{i}": "rp"}).status_code == 200
    # Someone else's Battle.net list can't offer a character already verified as member 61's
    o = login(a, 62)
    page = _link(a, o).text
    assert "Zulmara Ashveil" not in page and "Vashka Duskmane" in page
    # The list expires: an old snapshot can't be used to add characters
    with seeded.tx() as s:
        s.execute(text("update battlenet_links set linked_at = now() - interval '3 hours'"))
    j = re.search(r'name="pick" value="(\d+)"[^>]*>\s*<strong>Vashka Duskmane', page).group(1)
    r = o.post("/wow/quester/characters/battlenet/import",
               data={"csrf": csrf(o), "region": "us", "pick": [j], f"ruleset_{j}": "rp_pvp"})
    assert r.status_code == 422 and "expired" in r.text
    # Nothing ticked, or a made-up index, adds nothing
    with seeded.tx() as s:
        s.execute(text("update battlenet_links set linked_at = now()"))
    assert o.post("/wow/quester/characters/battlenet/import",
                  data={"csrf": csrf(o), "region": "us"}).status_code == 422
    r = o.post("/wow/quester/characters/battlenet/import", data={"csrf": csrf(o), "region": "us", "pick": ["99"]})
    with seeded.tx() as s:
        assert s.execute(text("select count(*) from characters where name='Vashka Duskmane'")).scalar_one() == 0


def test_ruleset_key_and_slug():
    ed = make_cfg().edition("forever")
    assert ruleset_key(ed, {"id": 1, "name": "PvP"}) == "pvp"
    assert ruleset_key(ed, {"id": 2}) == "normal"
    assert ruleset_key(ed, 15) == "rp_pvp"
    assert ruleset_key(ed, {"name": "Roleplay"}) == "rp"
    assert ruleset_key(ed, {"name": "RP-PvP"}) == "rp_pvp"
    assert ruleset_key(ed, {"id": 99, "name": "Mystery"}) is None
    assert ruleset_key(ed, None) is None
    assert char_slug(" Grimtusk  Ironhide ") == "grimtusk-ironhide"


def test_ruleset_api_ids_validated():
    import copy
    import tomllib
    from conftest import ROOT
    eds = tomllib.loads((ROOT / "deploy" / "config.example.toml").read_text())["editions"]
    bad = copy.deepcopy(eds)
    bad[0]["ruleset_api_ids"] = {"hardcore": 4}                 # not one of the edition's rulesets
    with pytest.raises(ConfigError):
        make_cfg(editions=bad)
    ok = copy.deepcopy(eds)
    ok[0]["static_namespace"] = "static-1.60.1_70399-forever-{region}"   # pinned game version
    assert make_cfg(editions=ok).edition("forever").namespace("static", "us") == "static-1.60.1_70399-forever-us"


@needs_db
def test_worker_applies_blizzard_30_day_limit(seeded, worker_db, cfg):
    """Blizzard's API terms: no Blizzard data older than 30 days. The worker deletes Battle.net lists after
    the pick window and clears unrefreshed Blizzard checks; the character itself stays, self-reported."""
    from cryptography.fernet import Fernet

    from questersboard.config import ConfigHolder
    from questersboard.worker import Worker
    with seeded.tx() as s:
        old = char(s, cfg, actor(63), "Grimtusk Ironhide", level=20)
        new = char(s, cfg, actor(63), "Zulmara Ashveil")
        mid = old.member_id
        s.execute(text("update characters set verification='ownership_verified', api_external_id='501', "
                       "api_refreshed_at = now() - interval '31 days' where id=:i"), {"i": old.id})
        s.execute(text("update characters set verification='api_matched', api_external_id='502', "
                       "api_refreshed_at = now() - interval '2 days' where id=:i"), {"i": new.id})
        blob = Fernet(Fernet.generate_key()).encrypt(b"{}")
        s.execute(text("insert into battlenet_links (member_id, region, snapshot_enc, linked_at) "
                       "values (:m, 'us', :b, now() - interval '2 hours'), (:m, 'eu', :b, now())"),
                  {"m": mid, "b": blob})
        s.execute(text("insert into api_cache (key, status, body, fetched_at, expires_at) values "
                       "('k-old', 200, '{}', now() - interval '31 days', now() + interval '1 day'), "
                       "('k-new', 200, '{}', now(), now() + interval '1 day')"))
    Worker(ConfigHolder(cfg), worker_db, None).housekeeping()
    with seeded.tx() as s:
        rows = dict(s.execute(text("select name, verification || '|' || coalesce(api_external_id,'') || '|' || "
                                   "coalesce(level::text,'') from characters where member_id=:m"), {"m": mid}).all())
        links = [r for r, in s.execute(text("select region from battlenet_links where member_id=:m"), {"m": mid})]
        cache = [k for k, in s.execute(text("select key from api_cache where key like 'k-%' order by key"))]
    assert rows == {"Grimtusk Ironhide": "self_reported||20", "Zulmara Ashveil": "api_matched|502|"}
    assert links == ["eu"] and cache == ["k-new"]


@needs_db
def test_worker_cannot_read_battlenet_lists():
    import os

    import psycopg
    from test_db_hardening import _connect
    with _connect("questersboard_worker", os.environ.get("WQT_WORKER_PW")) as c:
        c.execute("select member_id, region, linked_at from battlenet_links")     # when, not what
        c.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select snapshot_enc from battlenet_links")
        c.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("update characters set name = 'x' where false")
        c.rollback()


def test_data_ttl_capped_at_30_days():
    with pytest.raises(ConfigError):
        make_cfg(blizzard={"enabled": True, "data_ttl_days": 31})
    assert make_cfg(blizzard={"enabled": True, "data_ttl_days": 7})["blizzard"]["data_ttl_days"] == 7
