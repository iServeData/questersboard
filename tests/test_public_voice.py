"""[public_voice]: head counts for open voice channels on the community status card.

Discord is a FIXTURE: channels and members are stand-ins, the gateway is never contacted. IDs are synthetic.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import fake_id, make_cfg, needs_db
from fastapi.testclient import TestClient
from sqlalchemy import text

from questersboard.bot.client import Bot
from questersboard.config import LIVE_SECTIONS, ConfigError, ConfigHolder
from questersboard.domain import voice
from questersboard.domain.timeutil import now
from questersboard.models import PublicVoiceChannel
from questersboard.web.app import create_app

A, B, C = fake_id(7001), fake_id(7002), fake_id(7003)
BASE = "https://community.example.org"


# ------------------------------------------------------------------ config (no DB)

def test_public_voice_is_live_and_empty_by_default():
    assert "public_voice" in LIVE_SECTIONS
    assert make_cfg()["public_voice"] == {"channels": []}
    assert make_cfg(public_voice={"channels": [A, B]})["public_voice"]["channels"] == [A, B]


@pytest.mark.parametrize("bad", [{"channels": [A, A]}, {"channels": ["general"]}, {"channels": A},
                                 {"channels": [fake_id(7100 + i) for i in range(11)]},
                                 {"join_links": True}])
def test_public_voice_rejects_bad_values(bad):
    with pytest.raises(ConfigError, match=r"\[public_voice\]"):
        make_cfg(public_voice=bad)


# ------------------------------------------------------------------ bot count (no DB)

def _member(bot=False):
    return SimpleNamespace(bot=bot)


def test_bot_counts_people_not_bots_and_reports_unseen_channels():
    chans = {int(A): SimpleNamespace(name="Voice One", members=[_member(), _member(), _member(bot=True)]),
             int(B): SimpleNamespace(name="Voice Two", members=[])}
    fake = SimpleNamespace(get_channel=lambda i: chans.get(i))
    seen = Bot.public_voice(fake, [A, B, C])
    assert seen == {A: ("Voice One", 2), B: ("Voice Two", 0), C: None}


# ------------------------------------------------------------------ recording + status.json (DB)

def _rows(db):
    with db.tx() as s:
        return {r.channel_id: (r.position, r.name, r.in_voice, r.occupied_since, r.last_active_at)
                for r in s.query(PublicVoiceChannel)}


@needs_db
def test_record_public_tracks_since_and_last_active(seeded):
    t0 = now().replace(microsecond=0)
    with seeded.tx() as s:
        voice.record_public(s, [A, B], {A: ("Voice One", 0), B: None}, t0)
    r = _rows(seeded)
    assert r[A][:3] == (0, "Voice One", 0) and r[A][3] is None and r[A][4] is None
    assert r[B][0] == 1 and r[B][2] is None

    with seeded.tx() as s:                                        # someone joins
        voice.record_public(s, [A, B], {A: ("Voice One", 2)}, t0 + timedelta(minutes=5))
    r = _rows(seeded)
    assert r[A][2] == 2 and r[A][3] == t0 + timedelta(minutes=5)
    with seeded.tx() as s:                                        # a third joins: since unchanged
        voice.record_public(s, [A, B], {A: ("Voice One", 3)}, t0 + timedelta(minutes=9))
    assert _rows(seeded)[A][3] == t0 + timedelta(minutes=5)
    with seeded.tx() as s:                                        # everyone leaves
        voice.record_public(s, [A, B], {A: ("Voice One", 0)}, t0 + timedelta(minutes=60))
    r = _rows(seeded)
    assert r[A][2] == 0 and r[A][3] is None and r[A][4] == t0 + timedelta(minutes=60)

    with seeded.tx() as s:                                        # config change: B removed, C first
        voice.record_public(s, [C, A], {C: ("Voice Three", 1)}, t0 + timedelta(minutes=61))
    r = _rows(seeded)
    assert set(r) == {A, C} and r[C][0] == 0 and r[A][0] == 1
    with seeded.tx() as s:                                        # empty list clears everything
        voice.record_public(s, [], {}, t0 + timedelta(minutes=62))
    assert _rows(seeded) == {}


@needs_db
def test_status_json_lists_voice_channels(seeded, tmp_path, admin_engine):
    cfg = make_cfg(tmp_path, public_voice={"channels": [A, B, C]})
    t = now().replace(microsecond=0)
    with seeded.tx() as s:
        voice.record_public(s, [A, B, C], {A: ("Voice One", 0), B: ("Voice Two", 0), C: None},
                            t - timedelta(minutes=30))
        voice.record_public(s, [A, B, C], {A: ("Voice One", 0)}, t - timedelta(minutes=2))  # last active? no
        voice.record_public(s, [A, B, C], {A: ("Voice One", 4), B: ("Voice Two", 0)}, t)
    c = TestClient(create_app(ConfigHolder(cfg), seeded, None, None), base_url=BASE)
    j = c.get("/wow/quester/status.json").json()
    assert j["schema_version"] == 1
    assert j["features"] == {"event_voice": cfg["voice"]["enabled"]}
    v = j["voice_channels"]
    assert [x["id"] for x in v] == [A, B, C]
    assert v[0]["state"] == "live" and v[0]["in_voice"] == 4 and v[0]["since"] and v[0]["name"] == "Voice One"
    assert v[0]["url"] == f"https://discord.com/channels/{cfg['community']['guild_id']}/{A}"
    assert v[1]["state"] == "quiet" and v[1]["in_voice"] == 0 and v[1]["since"] is None
    assert v[2]["state"] == "unknown" and v[2]["in_voice"] is None
    assert not any("members" in x or "users" in x for x in v)                  # counts only, no names

    with admin_engine.connect() as conn:                                        # bot gone quiet: stale
        conn.execute(text("UPDATE public_voice_channels SET observed_at = now() - interval '10 minutes'"))
    v = c.get("/wow/quester/status.json").json()["voice_channels"]
    assert [x["state"] for x in v] == ["unknown", "unknown", "unknown"]
    assert all(x["in_voice"] is None and x["since"] is None for x in v)


@needs_db
def test_status_json_without_channels(seeded, tmp_path):
    c = TestClient(create_app(ConfigHolder(make_cfg(tmp_path)), seeded, None, None), base_url=BASE)
    assert c.get("/wow/quester/status.json").json()["voice_channels"] == []
