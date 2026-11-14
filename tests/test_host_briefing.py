"""Host briefing: hosts and co-hosts get a short DM when the event starts, and admins when they press
Support Admin, with everything needed to run the event. All names are made up; Discord is a FIXTURE."""
from __future__ import annotations

import re

import pytest
from conftest import actor, at, fake_id, make_cfg, needs_db
from fake_discord import FakeDiscord
from helpers import char, event

from questersboard.config import ConfigError, ConfigHolder
from questersboard.domain import briefing, events, signups, voice
from questersboard.worker import Worker
from test_event_voice import CAT, CH, GUILD, _drain

pytestmark = needs_db
HOST = actor(1, "event_creator", name="Fixture Host")
COHOST = actor(2, name="Fixture Cohost")
PLAYER = actor(80, name="Fixture Player")
BOT_CH = fake_id(96)


def _cfg(**hb):
    return make_cfg(community={"guild_id": GUILD},
                    discord={"event_channel_id": CH, "allowed_command_channel_ids": [BOT_CH]},
                    event_threads={"enabled": True, "private": True},
                    voice={"enabled": True, "category_id": CAT, "staff_role_ids": [], "moderators_can_join": False,
                           "guest_invites": True, "invite_dm": "hosts"},
                    host_briefing={"delay_seconds": 0, **hb})


def _start(seeded, worker_db, monkeypatch, **hb):
    cfg, fake = _cfg(**hb), FakeDiscord()
    monkeypatch.setattr(briefing, "WAIT_SECONDS", 0)
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    monkeypatch.setattr(events, "now", lambda: at(0))
    with seeded.tx() as s:
        ev = event(s, cfg, HOST, 0, 2, title="Fixture Run", late_join=True, cohosts=[COHOST.discord_id])
        signups.join(s, cfg, PLAYER, ev.id, char(s, cfg, PLAYER, "Fixture Grimtusk").id)
        events.start(s, cfg, HOST, ev.id)
        eid = ev.id
    _drain(w, 60)
    return cfg, fake, w, eid


def _to(fake, who):
    return [t for u, t in fake.dms if u == who.discord_id]


def _check_wording(text):
    assert len(text) <= 1900
    assert not re.search(r"\b(AM|PM)\b|webhook|outbox", text, re.I)


def test_config_defaults_and_rules():
    cfg = make_cfg()
    assert cfg["host_briefing"] == {"enabled": True, "support_admin": True, "delay_seconds": 20}
    with pytest.raises(ConfigError, match="host_briefing"):
        make_cfg(host_briefing={"enabled": "yes"})
    with pytest.raises(ConfigError, match="delay_seconds"):
        make_cfg(host_briefing={"delay_seconds": 9999})
    with pytest.raises(ConfigError, match="unknown"):
        make_cfg(host_briefing={"colour": "red"})


def test_host_and_cohost_get_one_briefing_each_when_it_starts(seeded, worker_db, monkeypatch):
    cfg, fake, w, eid = _start(seeded, worker_db, monkeypatch)
    vid = next(iter(fake.voice))
    th = next(iter(fake.threads))
    host, co = _to(fake, HOST), _to(fake, COHOST)
    assert len(host) == 1 and len(co) == 1                 # the guest link is in the briefing, not a second DM
    h, c = host[0], co[0]
    print("\n--- host ---\n" + h + "\n--- co-host ---\n" + c)
    assert h.startswith("**You're hosting Fixture Run. It has started.**")
    assert c.startswith("**You're co-hosting Fixture Run. It has started.**")
    for text in (h, c):
        _check_wording(text)
        assert f"https://discord.com/channels/{GUILD}/{vid}" in text            # voice
        assert f"https://discord.com/channels/{GUILD}/{th}" in text             # event chat
        assert "/events/" in text and "/v/" in text                             # event page, guest link
        assert "Planned end:" in text and " UTC" in text
        assert "End event" in text and "Reopen voice" in text
        assert "`/voice kick @name`" in text and "`/voice ban @name`" in text and "`/voice unban @name`" in text
        assert f"<#{BOT_CH}>" in text
    assert "remove the host, the server owner" in c and "remove the host" not in h
    assert "mark attendance" in h and "add your story" in c
    assert not _to(fake, PLAYER) or all("You're hosting" not in t and "co-hosting" not in t for t in _to(fake, PLAYER))
    _drain(w)                                                                     # no repeats
    assert len(_to(fake, HOST)) == 1 and len(_to(fake, COHOST)) == 1


def test_admin_gets_it_when_they_press_support_admin(seeded, worker_db, monkeypatch):
    cfg, fake, w, eid = _start(seeded, worker_db, monkeypatch)
    adm = actor(3, "admin", "event_moderator", name="Fixture Admin")
    helper = actor(4, "admin", name="Fixture Helper")
    assert not _to(fake, adm)                                                     # nothing until they press it
    with seeded.tx() as s:
        voice.support_join(s, cfg, adm, eid)
        voice.support_join(s, cfg, adm, eid)                                      # twice: one DM
        voice.support_join(s, cfg, helper, eid)
    _drain(w)
    (a,), (b,) = _to(fake, adm), _to(fake, helper)
    print("\n--- Support Admin (can moderate) ---\n" + a + "\n--- Support Admin (can't moderate) ---\n" + b)
    for text in (a, b):
        _check_wording(text)
        assert text.startswith("**You're in Fixture Run as Support Admin.**")
        assert "Leave Support Admin" in text and "/v/" not in text
    assert "`/voice kick @name`" in a and "End event" in a and "remove the host" not in a
    assert "Running long" not in a                                                # hosts get that reminder
    assert "/voice" not in b and "End event" not in b                            # only what they can do


def test_turned_off(seeded, worker_db, monkeypatch):
    cfg, fake, w, eid = _start(seeded, worker_db, monkeypatch, enabled=False)
    assert not any("It has started" in t for _, t in fake.dms)
    assert any("Guest voice link" in t for t in _to(fake, HOST))                  # invite_dm = hosts still works
    with seeded.tx() as s:
        voice.support_join(s, cfg, actor(3, "admin"), eid)
    _drain(w)
    assert not any("as Support Admin.**" in t for _, t in fake.dms)
