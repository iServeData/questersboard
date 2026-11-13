"""Support Admin: an admin steps into an event's chat and voice without signing up. FIXTURE Discord only."""
from __future__ import annotations

import pytest
from conftest import ROLE, actor, fake_id, make_cfg, needs_db
from fake_discord import FakeDiscord
from sqlalchemy import select
from test_voice_guests import _run

from questersboard.bot import client
from questersboard.config import ConfigError, ConfigHolder
from questersboard.domain import voice
from questersboard.domain.errors import PermissionDenied
from questersboard.models import AuditLog, EventVoiceGuest
from questersboard.worker import Worker
from test_event_voice import CAT, CH, GUILD, _drain

pytestmark = needs_db


def _cfg(**sa):
    return make_cfg(community={"guild_id": GUILD}, discord={"event_channel_id": CH},
                    event_threads={"enabled": True, "private": True},
                    voice={"enabled": True, "category_id": CAT, "moderators_can_join": False},
                    support_admin=sa)


def _setup(seeded, worker_db, monkeypatch, **sa):
    cfg, fake = _cfg(**sa), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    eid = _run(seeded, cfg, monkeypatch, w)
    return cfg, fake, w, eid


def test_config_defaults_and_rules():
    cfg = _cfg()
    assert cfg["support_admin"] == {"enabled": True, "announce": True, "role_ids": []}
    assert cfg.support_admin_roles() == frozenset({ROLE["admin"]})
    assert _cfg(role_ids=[fake_id(95)]).support_admin_roles() == frozenset({fake_id(95)})
    assert _cfg(enabled=False).support_admin_roles() == frozenset()
    with pytest.raises(ConfigError, match="support_admin"):
        _cfg(announce="yes")
    with pytest.raises(ConfigError, match="support_admin"):
        _cfg(colour="red")


def test_admin_joins_chat_and_voice_without_signing_up_and_it_is_visible(seeded, worker_db, monkeypatch):
    cfg, fake, w, eid = _setup(seeded, worker_db, monkeypatch)
    adm = actor(190, "admin", name="Fixture Admin")
    th = next(iter(fake.threads.values()))
    vid = next(iter(fake.voice))
    assert adm.discord_id not in th["members"] and adm.discord_id not in fake.voice[vid]["overwrites"]
    with seeded.tx() as s:
        assert not voice.can_join(s, cfg, adm, s.get(voice.Event, eid))
        _, shown, acts = client._organizer(s, cfg, adm, eid)
        assert shown and acts == ["support"]
        voice.support_join(s, cfg, adm, eid)
        voice.support_join(s, cfg, adm, eid)                       # twice: one row, one note
        assert voice.can_join(s, cfg, adm, s.get(voice.Event, eid))
        assert client._organizer(s, cfg, adm, eid)[2] == ["supportleave"]
    _drain(w)
    assert adm.discord_id in th["members"]
    assert fake.voice[vid]["overwrites"][adm.discord_id] == (1, voice.PARTICIPANT, 0)
    notes = [p for p in th["posts"] if "Support Admin" in p]
    assert notes == ["**Fixture Admin** joined as Support Admin (not signed up)."]
    with seeded.tx() as s:
        acts = [a.action for a in s.scalars(select(AuditLog).where(AuditLog.target_id == str(eid)))]
        assert acts.count("event.support_admin_join") == 1
        voice.support_leave(s, cfg, adm, eid)
        assert s.get(EventVoiceGuest, (eid, adm.discord_id)) is None
    _drain(w)
    assert adm.discord_id not in th["members"]
    assert th["posts"][-1] == "**Fixture Admin** left as Support Admin."


def test_others_must_join_with_a_character(seeded, worker_db, monkeypatch):
    cfg, fake, w, eid = _setup(seeded, worker_db, monkeypatch)
    officer, member = actor(191, "event_moderator"), actor(192)
    with seeded.tx() as s:
        assert "support" not in client._organizer(s, cfg, officer, eid)[2]      # host tools only
        assert client._organizer(s, cfg, member, eid)[1:] == (False, [])
        for a in (officer, member):
            with pytest.raises(PermissionDenied, match="Use Join or Tentative"):
                voice.support_join(s, cfg, a, eid)
        # someone already signed up (actor 80) isn't offered it
        assert "support" not in client._organizer(s, cfg, actor(80, "admin"), eid)[2]


def test_switched_off(seeded, worker_db, monkeypatch):
    cfg, fake, w, eid = _setup(seeded, worker_db, monkeypatch, enabled=False)
    with seeded.tx() as s:
        assert client._organizer(s, cfg, actor(193, "admin"), eid)[1:] == (False, [])
        with pytest.raises(PermissionDenied):
            voice.support_join(s, cfg, actor(193, "admin"), eid)


def test_admin_who_hosts_or_is_signed_up_keeps_host_tools_and_signup(seeded, worker_db, monkeypatch):
    from dataclasses import replace
    from test_event_voice import ORG
    from questersboard.domain import signups
    cfg, fake, w, eid = _setup(seeded, worker_db, monkeypatch)
    host_admin = replace(ORG, role_ids=ORG.role_ids | {ROLE["admin"]})
    player_admin = actor(80, "admin")                            # signed up (confirmed) in _run
    with seeded.tx() as s:
        pid, shown, acts = client._organizer(s, cfg, host_admin, eid)
        assert shown and acts == ["end"]                         # host tools, no Support Admin
        assert client._organizer(s, cfg, player_admin, eid)[2] == []
        from questersboard.domain import roster
        for a in (host_admin, player_admin):                     # the Join voice button, as the bot runs it
            assert voice.can_join(s, cfg, roster.ensure_member(s, a), s.get(voice.Event, eid))
    _drain(w)
    th = next(iter(fake.threads.values()))
    assert {ORG.discord_id, player_admin.discord_id} <= th["members"]
    assert not [p for p in th["posts"] if "Support Admin" in p]
    # leaving the signup takes an admin out like anyone else; Support Admin is then offered
    with seeded.tx() as s:
        signups.leave(s, cfg, player_admin, eid)
    _drain(w)
    assert player_admin.discord_id not in th["members"]
    with seeded.tx() as s:
        assert client._organizer(s, cfg, player_admin, eid)[2] == ["support"]
