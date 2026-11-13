"""Timed refresh from Blizzard ([blizzard] auto_refresh_hours, questersboard-refresh.timer).
FIXTURE data only: fake Blizzard answers through httpx.MockTransport; no live calls."""

from __future__ import annotations

import copy
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from conftest import actor, make_cfg, needs_db
from helpers import char
from sqlalchemy import text
from test_web import _bz_app

from questersboard import cli
from questersboard.config import ConfigError, validate
from questersboard.domain import profiles
from questersboard.integrations.blizzard import char_slug

ROOT = Path(__file__).resolve().parents[1]
RAW = tomllib.loads((ROOT / "deploy" / "config.example.toml").read_text())


# ------------------------------------------------------------------ config and schedule (no database)
def test_auto_refresh_hours_defaults_to_four_and_must_divide_the_day():
    assert make_cfg()["blizzard"].get("auto_refresh_hours", 4) == 4
    assert RAW["blizzard"]["auto_refresh_hours"] == 4
    for ok in (0, 1, 2, 3, 4, 6, 8, 12, 24):
        r = copy.deepcopy(RAW)
        r["blizzard"]["auto_refresh_hours"] = ok
        validate(r)
    for bad in (5, 7, 25, -4, True, "4", 2.5):
        r = copy.deepcopy(RAW)
        r["blizzard"]["auto_refresh_hours"] = bad
        with pytest.raises(ConfigError, match="auto_refresh_hours"):
            validate(r)


def test_runs_start_at_midnight_and_repeat_every_n_hours():
    cfg = make_cfg()
    assert [h for h in range(24) if profiles.auto_refresh_due(cfg, h)] == [0, 4, 8, 12, 16, 20]
    tz = timezone(timedelta(hours=-8))
    assert profiles.auto_refresh_next(cfg, datetime(2026, 11, 13, 21, 30, tzinfo=tz)) == \
        datetime(2026, 11, 14, 0, 0, tzinfo=tz)
    assert profiles.auto_refresh_next(cfg, datetime(2026, 11, 13, 4, 0, tzinfo=tz)) == \
        datetime(2026, 11, 13, 8, 0, tzinfo=tz)
    off = make_cfg(blizzard={"auto_refresh_hours": 0})
    assert not any(profiles.auto_refresh_due(off, h) for h in range(24))
    assert profiles.auto_refresh_next(off, datetime(2026, 11, 13, 1, tzinfo=tz)) is None
    daily = make_cfg(blizzard={"auto_refresh_hours": 24})
    assert [h for h in range(24) if profiles.auto_refresh_due(daily, h)] == [0]


def test_timer_and_service_are_installed_but_not_enabled():
    timer = (ROOT / "deploy" / "systemd" / "questersboard-refresh.timer").read_text()
    svc = (ROOT / "deploy" / "systemd" / "questersboard-refresh.service").read_text()
    install = (ROOT / "deploy" / "install.sh").read_text()
    assert "OnCalendar=*-*-* *:00:00" in timer and "Persistent=false" in timer
    assert "refresh-details --scheduled" in svc and "Type=oneshot" in svc and "User=questersboard" in svc
    creds = sorted(line.split(".", 1)[1] for line in svc.splitlines() if line.startswith("ImportCredential="))
    assert creds == ["blizzard-client-id", "blizzard-client-secret", "db-app-password"]   # least privilege
    assert "NoNewPrivileges=yes" in svc and "ProtectSystem=strict" in svc
    assert "questersboard-refresh.service questersboard-refresh.timer" in install
    assert "enable" not in install.split("SERVICES=")[1].split("\n")[0]
    for script in ("refresh_details.sh", "enable_auto_refresh.sh"):
        assert (ROOT / "deploy" / script).stat().st_mode & 0o111


def test_scheduled_run_does_nothing_when_off_or_not_due(tmp_path, monkeypatch, capsys):
    t = (ROOT / "deploy" / "config.example.toml").read_text()
    from conftest import fake_id
    t = t.replace('guild_id = ""', f'guild_id = "{fake_id(1)}"', 1).replace(
        'application_id = ""', f'application_id = "{fake_id(2)}"', 1)
    p = tmp_path / "config.toml"
    p.write_text(t)                               # [blizzard] enabled = false in the example
    assert cli.main(["--config", str(p), "refresh-details", "--scheduled"]) == 0
    assert "automatic refresh is off" in capsys.readouterr().out
    p.write_text(t.replace("enabled = false                               # turn on after credentials",
                           "enabled = true                               # turn on after credentials", 1))
    monkeypatch.setattr(profiles, "auto_refresh_due", lambda cfg, hour: False)
    assert cli.main(["--config", str(p), "refresh-details", "--scheduled"]) == 0
    assert "not due: runs every 4 hour(s) from midnight server time" in capsys.readouterr().out
    assert cli.main(["--config", str(p), "refresh-details", "--next"]) == 0
    assert "every 4 hour(s) from midnight server time; next run " in capsys.readouterr().out


# ------------------------------------------------------------------ the run itself (database)
GONE: set[str] = set()


def _page(name: str) -> str:
    return "/profile/wow/character/" + char_slug(name)


def _handler(req):
    p = req.url.path
    for name, cid in (("Fixture Ashtail", 601), ("Fixture Brightmoss", 602), ("Fixture Duskvale", 604)):
        if p == _page(name):
            if name in GONE:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": cid, "name": name, "level": 30})
        if p == _page(name) + "/equipment" and name not in GONE:
            return httpx.Response(200, json={"equipped_items": []})
    return httpx.Response(404)


@needs_db
def test_timed_refresh_refreshes_details_keeps_checks_and_removes_gone_characters(seeded, tmp_path, monkeypatch):
    GONE.clear()
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    bz = a.state.web.bz
    with seeded.tx() as s:
        ash = char(s, cfg, actor(161), "Fixture Ashtail").id               # imported details
        bright = char(s, cfg, actor(161), "Fixture Brightmoss").id         # Blizzard check only
        cold = char(s, cfg, actor(162), "Fixture Coldwater").id            # self-reported: never looked up
        dusk = char(s, cfg, actor(162), "Fixture Duskvale").id             # Blizzard check, then gone
    with seeded.tx() as s:
        from questersboard.domain.actor import SYSTEM
        owner = SYSTEM.with_member(s.get(profiles.Character, ash).member_id)
        profiles.import_profile(s, cfg, bz, owner, ash, {"portrait", "gear"}, {"portrait": "guild", "gear": "members"})
        for cid in (bright, dusk):
            ch = s.get(profiles.Character, cid)
            bz.apply_lookup(s, ch)
        s.execute(text("update characters set verification = 'ownership_verified' where id = :i"), {"i": bright})
        s.execute(text("update character_profiles set fetched_at = now() - interval '5 hours' where character_id=:i"),
                  {"i": ash})
    with seeded.tx() as s:
        rows = {cid: has for cid, _n, _m, has in profiles.auto_refresh_list(s, cfg)}
    assert rows.get(ash) is True and rows.get(bright) is False and dusk in rows and cold not in rows

    GONE.add("Fixture Duskvale")
    bz.fresh_profiles_after = profiles.now()        # what the scheduled run sets
    before = sum(1 for r in calls if r.url.path == _page("Fixture Ashtail"))
    got = {}
    for cid in (ash, bright, dusk):
        with seeded.tx() as s:
            got[cid] = profiles.auto_refresh_one(s, cfg, bz, cid)
    assert got == {ash: "refreshed", bright: "checked", dusk: "removed"}
    # the character page was read from Blizzard again, though the profile cache (6 hours) still held it
    assert sum(1 for r in calls if r.url.path == _page("Fixture Ashtail")) == before + 1
    with seeded.tx() as s:
        prof = s.execute(text("select shares, fetched_at > now() - interval '1 minute' from character_profiles "
                              "where character_id=:i"), {"i": ash}).one()
        assert prof[0] == {"portrait": "guild"} and prof[1]          # same sharing, fresh
        v = dict(s.execute(text("select id, verification from characters where id = any(:ids)"),
                           {"ids": [bright, cold, dusk]}).all())
        assert v == {bright: "ownership_verified", cold: "self_reported", dusk: "self_reported"}
        assert s.execute(text("select api_external_id from characters where id=:i"), {"i": dusk}).scalar() is None
        assert s.execute(text("select count(*) from audit_log where action = 'character.blizzard_gone'")).scalar() >= 1


@needs_db
def test_a_blizzard_outage_changes_nothing(seeded, tmp_path, monkeypatch):
    from questersboard.integrations.blizzard import ApiError
    a, cfg, _calls = _bz_app(seeded, tmp_path, monkeypatch, lambda r: httpx.Response(503))
    bz = a.state.web.bz
    with seeded.tx() as s:
        cid = char(s, cfg, actor(163), "Fixture Emberfall").id
        s.execute(text("update characters set verification = 'api_matched', api_refreshed_at = now() where id=:i"),
                  {"i": cid})
    with pytest.raises(ApiError), seeded.tx() as s:
        profiles.auto_refresh_one(s, cfg, bz, cid)
    with seeded.tx() as s:
        assert s.execute(text("select verification from characters where id=:i"), {"i": cid}).scalar() == "api_matched"


@needs_db
def test_owner_note_says_details_refresh_automatically(seeded, tmp_path, monkeypatch):
    from test_web import login
    from test_item_tooltips import _handler as tip_handler
    from questersboard.domain.actor import SYSTEM
    a, cfg, _calls = _bz_app(seeded, tmp_path, monkeypatch, tip_handler)
    with seeded.tx() as s:
        ch = char(s, cfg, actor(164), "Zulmara Ashveil")
        profiles.import_profile(s, cfg, a.state.web.bz, SYSTEM.with_member(ch.member_id), ch.id, {"gear"}, {})
    page = login(a, 164).get(f"/wow/quester/characters/{ch.id}/view").text
    assert "Refreshed automatically every 4 hours" in page and "unless you refresh it" not in page
    assert "Last refreshed from Blizzard just now" in page
