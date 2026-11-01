"""Integration boundaries with FIXTURE transports (no live Blizzard, Discord or AI calls)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from conftest import fake_id, make_cfg

from wowquester.config import ConfigError, validate
from wowquester.domain import calendar, privacy, recurrence
from wowquester.domain.errors import Invalid
from wowquester.integrations import ai, blizzard
from wowquester.integrations import discord_rest as dr


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
    monkeypatch.setenv("WOWQUESTER_SECRET_BLIZZARD_CLIENT_ID", "FIXTURE-id")
    monkeypatch.setenv("WOWQUESTER_SECRET_BLIZZARD_CLIENT_SECRET", "FIXTURE-secret")


def ch(**kw):
    base = dict(edition="retail", region="us", ruleset=None, realm_slug="area-52", name="Testchar",
                verification="self_reported", api_external_id=None, api_namespace=None, api_refreshed_at=None,
                level=None)
    return SimpleNamespace(**{**base, **kw})


def test_forever_is_unsupported_and_never_falls_back_to_retail():
    b, calls = _bz(lambda r: httpx.Response(200, json={"id": 1}))
    with pytest.raises(blizzard.Unsupported) as e:
        b.lookup_character(None, ch(edition="forever", realm_slug=None, ruleset="pvp"))
    assert "Forever" in str(e.value) and calls == []          # no request at all, no Retail namespace tried


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
