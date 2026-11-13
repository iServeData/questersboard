"""Event chat threads ([event_threads]): who is added, private vs public. FIXTURE Discord only."""
from __future__ import annotations

import pytest
from conftest import actor, make_cfg, needs_db
from fake_discord import FakeDiscord
from test_event_voice import CH, GUILD, _started

from questersboard.config import ConfigError, ConfigHolder
from questersboard.worker import Worker

pytestmark = needs_db


def _cfg(**th):
    return make_cfg(community={"guild_id": GUILD}, discord={"event_channel_id": CH},
                    event_threads={"enabled": True, **th})


def test_member_states_default_to_join_and_tentative_and_are_checked():
    assert _cfg()["event_threads"]["member_states"] == ["confirmed", "tentative", "waitlisted"]
    with pytest.raises(ConfigError, match="member_states"):
        _cfg(member_states=["going"])
    with pytest.raises(ConfigError, match="member_states"):
        _cfg(member_states=[])


@pytest.mark.parametrize("private", [True, False])
def test_thread_adds_join_tentative_and_the_waitlist(seeded, worker_db, monkeypatch, private):
    cfg, fake = _cfg(private=private), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    a, b, c = actor(180), actor(181), actor(182)          # confirmed, tentative, waitlisted
    _started(seeded, cfg, monkeypatch, w, a, b, c)
    assert len(fake.threads) == 1
    th = next(iter(fake.threads.values()))
    assert th["type"] == (12 if private else 11)
    assert {a.discord_id, b.discord_id, c.discord_id} <= th["members"]
    welcome = th["posts"][0]
    if private:
        assert welcome.startswith("Private chat for the people signed up to **Mine run** and its hosts.")
    else:
        assert "anyone who can see the event channel can read and reply here" in welcome
    assert "webhook" not in welcome.lower() and "outbox" not in welcome.lower()


def test_waitlist_can_be_left_out_by_config(seeded, worker_db, monkeypatch):
    cfg, fake = _cfg(member_states=["confirmed", "tentative"]), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    a, b, c = actor(183), actor(184), actor(185)
    _started(seeded, cfg, monkeypatch, w, a, b, c)
    assert c.discord_id not in next(iter(fake.threads.values()))["members"]
