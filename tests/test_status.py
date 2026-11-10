"""status.json for the community site's Community status and Next event cards.

Discord is a FIXTURE here: the bot's check-in is written directly, the gateway is never contacted.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import actor, fake_id, make_cfg, needs_db
from fastapi.testclient import TestClient
from helpers import char, event
from sqlalchemy import text

from questersboard.config import ConfigError, ConfigHolder
from questersboard.domain import events, signups, status
from questersboard.domain.timeutil import now
from questersboard.models import PublicEventVoice
from questersboard.web.app import create_app
from questersboard.worker import Worker

ORG = actor(1, "event_creator")
BASE = "https://community.example.org"


# ------------------------------------------------------------------ config (no DB)

def test_status_section_is_optional_with_defaults():
    cfg = make_cfg(status={})
    assert cfg["status"] == {"starting_soon_minutes": 15, "heartbeat_seconds": 30, "stale_after_seconds": 120,
                             "voice_stale_after_seconds": 180, "cache_seconds": 15}
    assert "status" in __import__("questersboard.config", fromlist=["x"]).LIVE_SECTIONS


@pytest.mark.parametrize("bad", [{"heartbeat_seconds": 2}, {"stale_after_seconds": 40},
                                 {"starting_soon_minutes": -1}, {"surprise": 1}, {"cache_seconds": True}])
def test_status_section_rejects_bad_values(bad):
    with pytest.raises(ConfigError, match=r"\[status\]"):
        make_cfg(status=bad)


def _pe(state, start_min, end_min):
    t = now()
    return SimpleNamespace(state=state, start_at=t + timedelta(minutes=start_min),
                           end_at=t + timedelta(minutes=end_min))


@pytest.mark.parametrize("pe, want", [
    (_pe("scheduled", 120, 240), "scheduled"),
    (_pe("scheduled", 10, 120), "starting_soon"),
    (_pe("scheduled", -5, 120), "waiting"),          # the clock alone never makes it live
    (_pe("in_progress", 20, 120), "live"),           # launched early
    (_pe("in_progress", -90, -10), "running_over"),  # never ended automatically
])
def test_phase_follows_the_organiser_launch(pe, want):
    assert status.phase(pe, now(), 15) == want


# ------------------------------------------------------------------ endpoint (DB)

@pytest.fixture
def client(seeded, cfg):
    return TestClient(create_app(ConfigHolder(cfg), seeded, None, None), base_url=BASE)


@needs_db
def test_empty_board_reports_unknown_services_and_no_event(client):
    r = client.get("/wow/quester/status.json")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=15"
    j = r.json()
    assert j["schema_version"] == 1 and j["event"] is None and j["live_count"] == 0
    assert j["services"]["event_board"]["state"] == "responding"
    assert {k: v["state"] for k, v in j["services"].items() if k != "event_board"} == \
        {"bot": "unknown", "discord": "unknown", "worker": "unknown"}


@needs_db
def test_heartbeats_from_the_service_roles(client, seeded, worker_db, cfg, admin_engine):
    w = Worker(ConfigHolder(cfg), worker_db, None)
    w.heartbeat()                                   # worker role: INSERT then UPDATE via its own grants
    w.heartbeat()
    with seeded.tx() as s:                          # bot runs as the app role
        status.beat(s, "bot", "connected", now())
    sv = client.get("/wow/quester/status.json").json()["services"]
    assert (sv["bot"]["state"], sv["discord"]["state"], sv["worker"]["state"]) == ("running", "connected", "running")

    with seeded.tx() as s:
        status.beat(s, "bot", "disconnected", now())
    assert client.get("/wow/quester/status.json").json()["services"]["discord"]["state"] == "disconnected"

    with admin_engine.connect() as c:
        c.execute(text("UPDATE service_heartbeats SET beat_at = now() - interval '10 minutes'"))
    sv = client.get("/wow/quester/status.json").json()["services"]
    assert (sv["bot"]["state"], sv["discord"]["state"], sv["worker"]["state"]) == ("stale", "unknown", "stale")


@needs_db
def test_bot_heartbeat_writes_through_run_tx(seeded):
    from questersboard.bot import client as bc
    bc.Ctx.db = seeded
    bot = bc.Bot()
    assert bot.gateway_state() == "disconnected"     # never logged in: no gateway
    asyncio.run(bot.heartbeat("connected"))
    with seeded.tx() as s:
        assert s.execute(text("select state from service_heartbeats where name='bot'")).scalar() == "connected"


@needs_db
def test_next_event_roster_and_needs_without_names(client, seeded, cfg):
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, start=3, roles=[{"key": "tank", "quantity": 1}, {"key": "healer", "quantity": 2}])
        p = actor(5)
        c = char(s, cfg, p, "Brakka")
        signups.join(s, cfg, p, ev.id, c.id, role_key="tank")
    j = client.get("/wow/quester/status.json").json()
    e = j["event"]
    assert e["title"] == "Deadmines run" and e["phase"] == "scheduled" and e["voice"] is None
    assert (e["signed_up"], e["capacity"]) == (1, 3)
    assert e["needs"] == [{"label": "Healer", "missing": 2}]
    assert e["url"].startswith(BASE + "/wow/quester/events/")
    assert "Brakka" not in str(j) and fake_id(5) not in str(j) and fake_id(1) not in str(j)


@needs_db
def test_starting_soon_then_live_only_after_launch(client, seeded, cfg, admin_engine):
    t = now().replace(microsecond=0)
    with seeded.tx() as s:
        later = event(s, cfg, ORG, start_at=t + timedelta(hours=30), end_at=t + timedelta(hours=31))
        soon = event(s, cfg, actor(2, "event_creator"), title="Wailing Caverns",
                     start_at=t + timedelta(minutes=10), end_at=t + timedelta(hours=2))
    e = client.get("/wow/quester/status.json").json()["event"]
    assert (e["title"], e["phase"]) == ("Wailing Caverns", "starting_soon")

    with seeded.tx() as s:
        events.start(s, cfg, actor(2, "event_creator"), soon.id)
    j = client.get("/wow/quester/status.json").json()
    assert (j["event"]["phase"], j["live_count"]) == ("live", 1)

    # a launched event wins over a scheduled one and stays listed past its planned end
    with admin_engine.connect() as c:
        c.execute(text("UPDATE public_events SET start_at = now() - interval '3 hours', "
                       "end_at = now() - interval '5 minutes' WHERE title = 'Wailing Caverns'"))
    j = client.get("/wow/quester/status.json").json()
    assert (j["event"]["title"], j["event"]["phase"]) == ("Wailing Caverns", "running_over")
    up = client.get("/wow/quester/upcoming.json").json()["items"]
    assert [i["title"] for i in up] == ["Wailing Caverns", "Deadmines run"]
    assert later.id


@needs_db
def test_voice_count_only_while_live_and_fresh(client, seeded, cfg, admin_engine):
    t = now().replace(microsecond=0)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, start_at=t + timedelta(minutes=30), end_at=t + timedelta(hours=2))
        s.add(PublicEventVoice(public_id=ev.public_id, in_voice=4))
    assert client.get("/wow/quester/status.json").json()["event"]["voice"] is None   # not launched yet
    with seeded.tx() as s:
        events.start(s, cfg, ORG, ev.id)
    v = client.get("/wow/quester/status.json").json()["event"]["voice"]
    assert v["in_voice"] == 4
    with admin_engine.connect() as c:
        c.execute(text("UPDATE public_event_voice SET observed_at = now() - interval '10 minutes'"))
    assert client.get("/wow/quester/status.json").json()["event"]["voice"] is None   # stale count hidden

    with seeded.tx() as s:
        events.end(s, cfg, ORG, ev.id)
    with admin_engine.connect() as c:                  # the projection row goes, and the count with it
        assert c.execute(text("select count(*) from public_event_voice")).scalar() == 0
    assert client.get("/wow/quester/status.json").json()["event"] is None
