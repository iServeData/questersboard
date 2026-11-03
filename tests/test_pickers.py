"""Time zone pickers, the packaged Retail race/class list and race/class dropdowns (iPad feedback)."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from conftest import actor, needs_db
from helpers import char
from sqlalchemy import select
from test_web import app, csrf, login, wcfg  # noqa: F401  (fixtures)

from wowquester.config import ConfigError, validate
from wowquester.domain import catalog
from wowquester.domain.errors import Invalid
from wowquester.domain.timeutil import tz_choices, tz_label
from wowquester.models import Member


def test_tz_choices_are_iana_region_city_names_with_offsets():
    winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    groups = dict(tz_choices(winter))
    assert "America" in groups and "Europe" in groups and "Other" in groups
    names = [z for zs in groups.values() for z, _ in zs]
    assert "America/Los_Angeles" in names and "UTC" in names
    assert not any(n.startswith(("US/", "Etc/")) or n in ("EST5EDT", "GMT") for n in names)
    la = dict(groups["America"])["America/Los_Angeles"]
    assert la == "Los Angeles \u00b7 UTC\u221208:00"
    summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    assert tz_label("America/Los_Angeles", summer).endswith("UTC\u221207:00")
    assert tz_label("Asia/Kolkata", summer).endswith("UTC+05:30")


def test_common_timezones_config_is_validated(cfg):
    raw = dict(cfg.raw)
    raw["community"] = dict(raw["community"], common_timezones=["Mars/Olympus"])
    with pytest.raises(ConfigError, match="common_timezones"):
        validate(raw)


@needs_db
def test_retail_seed_lists_races_and_blocks_bad_combos(seeded, cfg):
    with seeded.tx() as s:
        assert catalog.seed_edition(s, cfg, "retail") > 200
        m = catalog.race_class_map(s, "retail")
        horde = {r["key"]: r["classes"] for r in m["factions"]["horde"]}
        alliance = {r["key"]: r["classes"] for r in m["factions"]["alliance"]}
        assert "shaman" not in horde["undead"] and "paladin" in horde["undead"]
        assert set(horde["haranir"]) == set(alliance["haranir"]) == {
            "druid", "hunter", "mage", "monk", "priest", "rogue", "shaman", "warlock", "warrior"}
        assert "demon_hunter" in alliance["void_elf"] and "evoker" in horde["dracthyr"]
        assert m["classes"]["death_knight"] == "Death Knight"
        with pytest.raises(Invalid):
            char(s, cfg, actor(10), "Nope", edition="retail", ruleset=None, realm_slug="area-52",
                 race_key="undead", class_key="shaman")
        c = char(s, cfg, actor(10), "Ok", edition="retail", ruleset=None, realm_slug="area-52",
                 race_key="Mag'har Orc", class_key="Shaman")
        assert (c.race_key, c.class_key) == ("maghar_orc", "shaman")


@needs_db
def test_character_form_has_dropdowns_and_text_fallback(app, seeded):  # noqa: F811
    c = login(app, 10)
    page = c.get("/wow/quester/characters").text
    assert "data-race-select" in page and "data-class-select" in page
    assert 'name="race_key"' in page and 'name="class_key"' in page   # what the server reads
    assert "<datalist id=\"wq-race-list\"" not in page                 # iOS datalists are gone
    assert '"retail"' in page                                          # race map includes Retail


@needs_db
def test_settings_time_zone_picker_saves_and_clears(app, seeded):  # noqa: F811
    c = login(app, 10)
    page = c.get("/wow/quester/me").text
    sel = re.search(r'<select name="timezone">(.*?)</select>', page, re.S).group(1)
    assert '<option value="" selected>Community default (America/Los Angeles' in sel
    assert '<optgroup label="Common">' in sel and 'value="Europe/Berlin"' in sel
    tok = csrf(c)
    assert c.post("/wow/quester/me", data={"csrf": tok, "timezone": "Europe/Berlin"}).status_code == 303
    with seeded.tx() as s:
        assert s.scalar(select(Member.timezone).where(Member.discord_id == actor(10).discord_id)) == "Europe/Berlin"
    page = c.get("/wow/quester/me").text
    assert '<option value="Europe/Berlin" selected>' in page
    assert c.post("/wow/quester/me", data={"csrf": csrf(c), "timezone": ""}).status_code == 303
    with seeded.tx() as s:
        assert s.scalar(select(Member.timezone).where(Member.discord_id == actor(10).discord_id)) is None
    r = c.post("/wow/quester/me", data={"csrf": csrf(c), "timezone": "Mars/Olympus"})
    assert r.status_code == 422 and "Unknown time zone" in r.text


@needs_db
def test_event_form_time_zone_is_a_picker(app, seeded):  # noqa: F811
    c = login(app, 1, "event_creator")
    page = c.get("/wow/quester/events/new").text
    sel = re.search(r'<select name="timezone" required data-tz-default>(.*?)</select>', page, re.S).group(1)
    assert '<option value="America/Los_Angeles" selected>' in sel
    assert '<optgroup label="Asia">' in sel


@needs_db
def test_new_event_starts_in_my_saved_zone(app, seeded):  # noqa: F811
    c = login(app, 1, "event_creator")
    c.post("/wow/quester/me", data={"csrf": csrf(c, "/wow/quester/me"), "timezone": "Europe/London"})
    page = c.get("/wow/quester/events/new").text
    sel = re.search(r'<select name="timezone" required>(.*?)</select>', page, re.S).group(1)  # no browser override
    assert '<option value="Europe/London" selected>' in sel
