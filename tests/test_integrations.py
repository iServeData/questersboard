"""Integration boundaries with FIXTURE transports (no live Blizzard, Discord or AI calls)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from conftest import fake_id, make_cfg

from questersboard.config import ConfigError, validate
from questersboard.domain import calendar, privacy, recurrence
from questersboard.domain.errors import Invalid
from questersboard.integrations import ai, blizzard
from questersboard.integrations import discord_rest as dr


# ------------------------------------------------------------------ Blizzard
def _bz(handler, **over):
    cfg = make_cfg(blizzard={"enabled": True, **over})
    calls = []

    def h(req):
        calls.append(req)
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "FIXTURE", "expires_in": 3600})
        return handler(req)
    b = blizzard.Blizzard(cfg, http=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda _s: None)
    return b, calls


@pytest.fixture(autouse=True)
def _bz_creds(monkeypatch):
    monkeypatch.setenv("QUESTERSBOARD_SECRET_BLIZZARD_CLIENT_ID", "FIXTURE-id")
    monkeypatch.setenv("QUESTERSBOARD_SECRET_BLIZZARD_CLIENT_SECRET", "FIXTURE-secret")


def ch(**kw):
    base = dict(edition="retail", region="us", ruleset=None, realm_slug="area-52", name="Testchar",
                verification="self_reported", api_external_id=None, api_namespace=None, api_refreshed_at=None,
                level=None)
    return SimpleNamespace(**{**base, **kw})


def test_forever_lookup_is_realmless_and_never_falls_back_to_retail():
    b, calls = _bz(lambda r: httpx.Response(200, json={"id": 1, "level": 14, "ruleset": {"id": 3}}))
    m = b.lookup_character(None, ch(edition="forever", realm_slug=None, ruleset="pvp", name="Zulmara Ashveil"))
    prof = [r for r in calls if "/profile/" in r.url.path]
    assert m.found and m.ruleset == "rp" and m.level == 14
    assert [r.url.path for r in prof] == ["/profile/wow/character/zulmara-ashveil"]
    assert prof[0].url.params["namespace"] == "profile-forever-us"          # never profile-us (Retail)


def test_edition_without_character_api_is_refused_without_requests():
    import copy
    import tomllib
    from conftest import ROOT
    eds = copy.deepcopy(tomllib.loads((ROOT / "deploy" / "config.example.toml").read_text())["editions"])
    eds[0]["api_status"]["character"] = "unsupported"
    b = blizzard.Blizzard(make_cfg(blizzard={"enabled": True}, editions=eds),
                          http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    with pytest.raises(blizzard.Unsupported) as e:
        b.lookup_character(None, ch(edition="forever", realm_slug=None, ruleset="pvp"))
    assert "Forever" in str(e.value)


def test_retail_requires_realm():
    b, _ = _bz(lambda r: httpx.Response(200, json={}))
    with pytest.raises(blizzard.Unsupported):
        b.lookup_character(None, ch(realm_slug=None))


def test_retail_lookup_retries_429_and_is_not_ownership():
    seq = [httpx.Response(429, headers={"Retry-After": "1"}),
           httpx.Response(200, json={"id": 77, "level": 80, "race": {"name": "Orc"},
                                     "character_class": {"name": "Warrior"}, "faction": {"type": "HORDE"}})]
    b, calls = _bz(lambda r: seq.pop(0))
    c = ch()
    m = b.apply_lookup(None, c)
    assert m.found and m.faction == "horde" and c.verification == "api_matched"
    assert c.verification != "ownership_verified"
    prof = [r for r in calls if "/profile/" in r.url.path]
    assert len(prof) == 2 and prof[-1].url.params["namespace"] == "profile-us"
    assert prof[-1].url.path == "/profile/wow/character/area-52/testchar"


def test_retail_404_is_not_found_and_5xx_exhausts_to_api_error():
    b, _ = _bz(lambda r: httpx.Response(404))
    assert b.lookup_character(None, ch()).found is False
    b, calls = _bz(lambda r: httpx.Response(503), max_retries=2)
    with pytest.raises(blizzard.ApiError):
        b.lookup_character(None, ch())
    assert len([r for r in calls if "/profile/" in r.url.path]) == 3


def test_ptr_namespace_rejected_by_config():
    raw = make_cfg().raw
    import copy
    bad = copy.deepcopy(raw)
    for ed in bad["editions"]:
        if ed["key"] == "retail":
            ed["profile_namespace"] = "profile-classic-ptr-{region}"
    with pytest.raises(ConfigError):
        validate(bad)


# ------------------------------------------------------------------ Discord REST classification (FIXTURE)
@pytest.mark.parametrize("status,exc", [(429, dr.RateLimited), (404, dr.Gone), (403, dr.Forbidden),
                                        (502, dr.Transient)])
def test_discord_status_classification(status, exc):
    def h(req):
        hdr = {"Retry-After": "2"} if status == 429 else {}
        return httpx.Response(status, json={"code": 50001, "retry_after": 2}, headers=hdr)
    rest = dr.DiscordRest(http=httpx.Client(transport=httpx.MockTransport(h)), token="FIXTURE")
    with pytest.raises(exc):
        rest.post(fake_id(55), {"content": "x"})


def test_discord_request_has_no_mentions_allowed():
    seen = {}

    def h(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": fake_id(66)})
    rest = dr.DiscordRest(http=httpx.Client(transport=httpx.MockTransport(h)), token="FIXTURE")
    assert rest.post(fake_id(55), {"content": "hi @everyone"}) == fake_id(66)
    am = seen["body"].get("allowed_mentions")
    assert am is not None and not am.get("parse")


# ------------------------------------------------------------------ recurrence + DST
def test_weekly_series_keeps_local_time_across_dst():
    starts, notes = recurrence.occurrences("FREQ=WEEKLY", datetime(2026, 10, 24, 19, 0), "America/Los_Angeles", 3)
    # Oct 24/31 are PDT (UTC-7); Nov 7 is PST (UTC-8): same 19:00 local, different UTC
    assert [s.astimezone(timezone.utc).hour for s in starts] == [2, 2, 3]
    assert not notes


def test_nonexistent_local_time_skipped_with_note():
    starts, notes = recurrence.occurrences("FREQ=DAILY", datetime(2027, 3, 13, 2, 30), "America/Los_Angeles", 2)
    assert len(starts) == 1 and notes and "doesn't exist" in notes[0]


def test_recurrence_rejects_other_freq():
    with pytest.raises(Invalid):
        recurrence.occurrences("FREQ=SECONDLY", datetime(2026, 11, 1, 19), "UTC", 3)


# ------------------------------------------------------------------ ICS
def test_ics_escapes_folds_and_uses_utc():
    cfg = make_cfg()
    st = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)
    body = calendar.ics(cfg, "Guild, events", [dict(uid="e1", start=st, end=st + timedelta(hours=2),
                                                     title="Raid; night, " + "x" * 120, cancelled=True)])
    assert body.endswith("\r\n") and "X-WR-CALNAME:Guild\\, events" in body
    assert "DTSTART:20261107T030000Z" in body and "STATUS:CANCELLED" in body
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    assert "UID:e1@community.example.org" in body


# ------------------------------------------------------------------ AI wording (FIXTURE provider)
def _people():
    return [privacy.Participant(1, "Grommash", "orc", "warrior", "tank", "Tank", True, "DiscordAlice"),
            privacy.Participant(2, "Secretname", "troll", "priest", "healer", "Healer", False, "DiscordBob"),
            privacy.Participant(3, "Absentee", "orc", "rogue", None, None, True, "DiscordCarl", attended=False)]


def _rev():
    return SimpleNamespace(title="Deadmines run", outcome="completed",
                           attempted="Clear Deadmines with DiscordBob and Secretname",
                           happened="Secretname saved the run", moments="", body_md="")


def _cfg_ai():
    return make_cfg(ai={"enabled": True, "model": "fixture-model"})


def test_ai_payload_is_publication_safe():
    p = ai.safe_payload(_cfg_ai(), _rev(), _people())
    blob = json.dumps(p)
    assert "Secretname" not in blob and "DiscordBob" not in blob and "DiscordAlice" not in blob
    assert p["participants"] == ["Grommash", "a healer"]       # attended only, neutral for non-consent


def test_ai_reply_used_when_safe_and_template_otherwise():
    replies = iter(["The guild cleared the Deadmines. Grommash held the line.",
                    "Secretname was amazing <script>x</script>",
                    "See https://evil.example"])

    def h(req):
        assert req.url.path.endswith("/chat/completions")
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})
    client = httpx.Client(transport=httpx.MockTransport(h))
    text, src = ai.suggest_story(_cfg_ai(), _rev(), _people(), client=client)
    assert src == "ai" and "Grommash" in text
    assert ai.suggest_story(_cfg_ai(), _rev(), _people(), client=client)[1] == "template"
    assert ai.suggest_story(_cfg_ai(), _rev(), _people(), client=client)[1] == "template"


def test_ai_disabled_or_failing_uses_template():
    assert ai.suggest_story(make_cfg(), _rev(), _people())[1] == "template"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert ai.suggest_story(_cfg_ai(), _rev(), _people(), client=client)[1] == "template"


# ------------------------------------------------------------------ config validation
def test_retention_and_review_channel_validation():
    import copy
    raw = copy.deepcopy(make_cfg().raw)
    raw["retention"]["audit_days"] = 0
    with pytest.raises(ConfigError):
        validate(raw)
    raw = copy.deepcopy(make_cfg().raw)
    raw["discord"]["review_channel_id"] = "not-a-snowflake"
    with pytest.raises(ConfigError):
        validate(raw)
    raw = copy.deepcopy(make_cfg().raw)
    raw["web"]["bind"] = "0.0.0.0:8740"
    with pytest.raises(ConfigError):
        validate(raw)


# ---- probe-character (FIXTURE responses; the live Forever answers are unverified)
def test_probe_character_is_read_only_and_reports_each_page():
    def h(req):
        p = req.url.path
        if p == "/profile/wow/character/zulmara-ashveil":
            return httpx.Response(200, json={"_links": {}, "name": "Zulmara Ashveil", "level": 22,
                                             "guild": {"name": "Fixture Guild", "id": 7},
                                             "equipment": {"href": "https://fixture/equipment"}})
        if p.endswith("/equipment"):
            return httpx.Response(200, json={"equipped_items": [{"level": {"value": 20}}, {"level": {"value": 31}}]})
        if p.endswith("/professions"):
            return httpx.Response(200, json={"primaries": [{"profession": {"name": "Mining"},
                                                            "tiers": [{"skill_points": 75}]}]})
        return httpx.Response(404)
    b, calls = _bz(h)
    eds_ns = b.cfg.edition("forever").namespace("profile", "us")
    rows = b.probe_character("forever", "us", "Zulmara  Ashveil")
    by = {r["page"]: r for r in rows}
    assert len(rows) == len(blizzard.Blizzard.PROFILE_PAGES)
    assert by["summary"]["status"] == "200" and "guild=Fixture Guild" in by["summary"]["shape"]
    assert "equipment->" in by["summary"]["shape"] and "Zulmara" not in by["summary"]["shape"]
    assert "item levels 20-31" in by["equipment"]["shape"]
    assert "Mining 75" in by["professions"]["shape"]
    assert by["raids"]["status"] == "404"
    prof = [r for r in calls if "/profile/" in r.url.path]
    assert all(r.method == "GET" and r.url.params["namespace"] == eds_ns for r in prof)


def test_probe_character_needs_a_realm_where_editions_use_realms():
    b, calls = _bz(lambda r: httpx.Response(200, json={}))
    with pytest.raises(blizzard.Unsupported):
        b.probe_character("retail", "us", "Testchar")
    assert not [r for r in calls if "/profile/" in r.url.path]


def test_probe_character_records_errors_per_page():
    def h(req):
        if req.url.path.endswith("/titles"):
            return httpx.Response(403)
        return httpx.Response(404)
    b, _ = _bz(h)
    rows = {r["page"]: r for r in b.probe_character("forever", "us", "Zulmara Ashveil")}
    assert rows["titles"]["status"] == "error" and "403" in rows["titles"]["shape"]
    assert rows["summary"]["status"] == "404"


def test_probe_character_follows_the_guild_and_shows_one_page_tree():
    def h(req):
        p = req.url.path
        if p == "/profile/wow/character/zulmara-ashveil":
            return httpx.Response(200, json={"name": "Zulmara Ashveil", "level": 22, "guild": {
                "name": "Fixture Guild", "id": 7,
                "key": {"href": "https://us.api.blizzard.com/data/wow/guild/fixture/fixture-guild?namespace=profile-forever-us"}}})
        if p.endswith("/specializations"):
            return httpx.Response(200, json={"character": {"name": "Zulmara Ashveil"}, "specialization_groups": [
                {"is_active": True, "specializations": [{"specialization_name": "Fixture Tree", "spent_points": 5,
                                                          "talents": [{"talent": {"name": "Fixture Talent"},
                                                                       "talent_rank": 2}]}]}]})
        if p == "/data/wow/guild/fixture/fixture-guild":
            return httpx.Response(200, json={"name": "Fixture Guild", "member_count": 3})
        if p == "/data/wow/guild/fixture/fixture-guild/roster":
            return httpx.Response(200, json={"members": [{"character": {"name": "Someone Else"}}] * 3})
        return httpx.Response(404)
    b, calls = _bz(h)
    rows = {r["page"]: r for r in b.probe_character("forever", "us", "Zulmara Ashveil", detail="specializations")}
    assert rows["guild"]["status"] == "200" and rows["guild-roster"]["shape"].startswith("members[3]")
    tree = "\n".join(rows["specializations"]["tree"])
    assert "specialization_name: 'Fixture Tree'" in tree and "talent_rank: 2" in tree
    assert "Zulmara" not in tree                                  # the character block is left out
    assert "tree" not in rows["guild-roster"]                     # other players' names are never shown
    g = [r for r in calls if r.url.path.startswith("/data/wow/guild/")]
    assert all(r.url.params["namespace"] == "profile-forever-us" for r in g)


# ---- probe-media (FIXTURE responses; what Forever really serves is unverified until run on the host)
def _media_handler(icon_host="render.worldofwarcraft.com"):
    def h(req):
        p = req.url.path
        if p.endswith("/equipment"):
            return httpx.Response(200, json={"equipped_items": [
                {"item": {"id": 101}, "slot": {"type": "HEAD", "name": "Head"}, "level": {"value": 9},
                 "name": "Fixture Cap", "quality": {"type": "COMMON"}},
                {"item": {"id": 202}, "slot": {"type": "CHEST", "name": "Chest"}, "level": {"value": 12},
                 "name": "Fixture Vest", "quality": {"type": "UNCOMMON"},
                 "stats": [{"type": {"type": "STAMINA"}, "value": 3, "display": {"display_string": "+3 Stamina"}}],
                 "media": {"key": {"href": "https://us.api.blizzard.com/data/wow/media/item/202?namespace=static-fixture-us"},
                           "id": 202}}]})
        if p.endswith("/specializations"):
            return httpx.Response(200, json={"character": {"name": "Zulmara Ashveil"}, "specialization_groups": [
                {"is_active": True, "specializations": [{"specialization_name": "Fixture Tree", "talents": [
                    {"talent": {"id": 55, "name": "Fixture Talent"}, "talent_rank": 2,
                     "spell_tooltip": {"spell": {"id": 777, "name": "Fixture Spell"}, "description": "Does a thing."}}]}]}]})
        if p == "/data/wow/item/202":
            return httpx.Response(200, json={"id": 202, "name": "Fixture Vest", "preview_item": {"stats": []}})
        if p == "/data/wow/media/item/202":
            return httpx.Response(200, json={"assets": [{"key": "icon", "value": f"https://{icon_host}/us/icons/56/fixture.jpg"}]})
        if p == "/data/wow/media/spell/777":
            return httpx.Response(200, json={"assets": [{"key": "icon", "value": f"https://{icon_host}/us/icons/56/spell.jpg"}]})
        if p == "/data/wow/spell/777":
            return httpx.Response(200, json={"id": 777, "name": "Fixture Spell", "description": "Does a thing."})
        if p.startswith("/us/icons/"):
            return httpx.Response(200, content=b"\xff\xd8FIXTURE", headers={"content-type": "image/jpeg"})
        return httpx.Response(404)
    return h


def test_probe_media_checks_items_spells_icons_and_trees_read_only():
    b, calls = _bz(_media_handler())
    rows = b.probe_media("forever", "us", "Zulmara Ashveil")
    by = {r["check"].strip(): r for r in rows}
    sns = b.cfg.edition("forever").namespace("static", "us")
    assert "item id=202" in by["equipped-item"]["shape"]                 # highest item level picked
    assert by["item"]["status"] == "200" and by["item"]["namespace"] == sns
    assert by["item-media"]["status"] == "200" and by["item-media"]["namespace"] == "static-fixture-us"
    assert by["spell"]["status"] == "200" and by["spell-media"]["status"] == "200"
    icons = [r for r in rows if r["check"].strip() == "icon-image"]
    assert len(icons) == 2 and all(r["status"] == "200" and "discarded" in r["shape"] for r in icons)
    assert by["talent"]["status"] == "404" and by["talent-tree-index"]["status"] == "404"
    assert "spell id=777" in by["talent-entry"]["shape"]
    assert "display_string: '+3 Stamina'" in "\n".join(by["equipped-item"]["tree"])
    assert all(r.method == "GET" for r in calls if r.url.path != "/token")
    assert "Zulmara" not in "\n".join(line for r in rows for line in r.get("tree") or [])


def test_probe_media_does_not_fetch_icons_from_unlisted_hosts():
    b, calls = _bz(_media_handler(icon_host="cdn.example.org"))
    rows = b.probe_media("forever", "us", "Zulmara Ashveil")
    icons = [r for r in rows if r["check"].strip() == "icon-image"]
    assert icons and all(r["status"] == "-" and "not in [blizzard] render_image_hosts" in r["shape"] for r in icons)
    assert not [r for r in calls if r.url.host == "cdn.example.org"]


def test_probe_media_reports_a_character_without_gear_or_talents():
    b, _ = _bz(lambda r: httpx.Response(404))
    by = {r["check"].strip(): r for r in b.probe_media("forever", "us", "Zulmara Ashveil")}
    assert by["equipment"]["status"] == "404" and by["equipped-item"]["status"] == "-"
    assert by["talent-entry"]["status"] == "-" and "item" not in by


def test_probe_media_needs_a_realm_where_editions_use_realms():
    b, calls = _bz(lambda r: httpx.Response(200, json={}))
    with pytest.raises(blizzard.Unsupported):
        b.probe_media("retail", "us", "Testchar")
    assert not [r for r in calls if r.url.path != "/token"]
