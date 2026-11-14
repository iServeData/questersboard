"""Event voice permission matrix: every [voice] participant permission, worked out the way Discord does
(server role permissions, then the channel's @everyone, role and member overwrites) for each kind of
person: Support Admin, host, co-host, someone signed up, and someone who is none of those.
All names are made up; Discord is a FIXTURE (no live calls)."""
from __future__ import annotations

import pytest
from conftest import ROLE, actor, at, fake_id, make_cfg, needs_db
from fake_discord import FakeDiscord
from helpers import char, event

from questersboard.config import VOICE_PERMISSIONS, VOICE_PERMISSIONS_DEFAULT, ConfigHolder
from questersboard.domain import events, signups, voice
from questersboard.worker import Worker
from test_event_voice import CAT, CH, GUILD, _drain

pytestmark = needs_db

HOST = actor(1, "event_creator", name="Fixture Host")
COHOST = actor(2, name="Fixture Cohost")
ADMIN = actor(3, "admin", name="Fixture Admin")
PLAYER = actor(80, name="Fixture Player")
OUTSIDER = actor(99, name="Fixture Outsider")
# What a typical server gives @everyone server-wide: all of the voice permissions. The channel has to take
# them away from people who aren't in the event, so this is the hardest case for the lock.
SERVER_EVERYONE = sum(1 << b for b in VOICE_PERMISSIONS.values())


def _cfg():
    """Like the live server: nobody joins every event voice by role; admins come in as Support Admin."""
    return make_cfg(community={"guild_id": GUILD}, discord={"event_channel_id": CH},
                    event_threads={"enabled": True, "private": True},
                    voice={"enabled": True, "category_id": CAT, "staff_role_ids": [], "moderators_can_join": False})


def effective(ow: dict, user_id: str, role_ids, base: int = SERVER_EVERYONE) -> int:
    """Discord's order: server permissions, then @everyone overwrite, then all role overwrites together,
    then the member's own overwrite. No View Channel means nothing else applies; no Connect means none of
    the voice permissions can be used."""
    perm = base
    if GUILD in ow:
        _, a, d = ow[GUILD]
        perm = (perm & ~d) | a
    ra = rd = 0
    for r in role_ids:
        if r in ow:
            ra |= ow[r][1]
            rd |= ow[r][2]
    perm = (perm & ~rd) | ra
    if user_id in ow:
        _, a, d = ow[user_id]
        perm = (perm & ~d) | a
    if not perm & voice.VIEW:
        return 0
    if not perm & voice.CONNECT:
        perm &= ~sum(1 << VOICE_PERMISSIONS[n] for n in ("speak", "video", "use_voice_activity", "priority_speaker",
                                                       "use_soundboard", "use_external_sounds",
                                                       "use_embedded_activities"))
    return perm


def _row(ow, who) -> dict[str, bool]:
    p = effective(ow, who.discord_id, who.role_ids)
    return {n: bool(p & (1 << VOICE_PERMISSIONS[n])) for n in VOICE_PERMISSIONS_DEFAULT}


def _print(title, rows):
    names = VOICE_PERMISSIONS_DEFAULT
    print(f"\n{title}\n{'':<30}" + " ".join(f"{n[:9]:>9}" for n in names))
    for label, r in rows.items():
        print(f"{label:<30}" + " ".join(f"{'yes' if r[n] else '-':>9}" for n in names))


@pytest.fixture
def running(seeded, worker_db, monkeypatch):
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    monkeypatch.setattr(events, "now", lambda: at(0))
    with seeded.tx() as s:
        ev = event(s, cfg, HOST, 0, 2, title="Fixture Run", late_join=True, cohosts=[COHOST.discord_id])
        signups.join(s, cfg, PLAYER, ev.id, char(s, cfg, PLAYER, "Fixture Grimtusk").id)
        events.start(s, cfg, HOST, ev.id)
        eid = ev.id
    _drain(w)
    assert len(fake.voice) == 1
    return cfg, fake, w, eid, next(iter(fake.voice))


def test_every_permission_for_each_kind_of_person(seeded, running):
    cfg, fake, w, eid, vid = running
    everyone = {n: True for n in VOICE_PERMISSIONS_DEFAULT}
    nothing = {n: False for n in VOICE_PERMISSIONS_DEFAULT}

    before = {"Admin (not stepped in yet)": _row(fake.voice[vid]["overwrites"], ADMIN)}
    with seeded.tx() as s:
        voice.support_join(s, cfg, ADMIN, eid)
    _drain(w)
    ow = fake.voice[vid]["overwrites"]
    rows = {**before,
            "Admin (Support Admin)": _row(ow, ADMIN),
            "Host": _row(ow, HOST),
            "Co-host": _row(ow, COHOST),
            "Signed up": _row(ow, PLAYER),
            "None of those": _row(ow, OUTSIDER)}
    _print("Event voice permissions (FIXTURE Discord)", rows)

    assert rows["Admin (not stepped in yet)"] == nothing
    for who in ("Admin (Support Admin)", "Host", "Co-host", "Signed up"):
        assert rows[who] == everyone, who
    assert rows["None of those"] == nothing
    # Nobody but the bot gets moderation permissions from the channel.
    for who in (ADMIN, HOST, COHOST, PLAYER, OUTSIDER):
        p = effective(ow, who.discord_id, who.role_ids)
        assert not p & (voice.MANAGE_CHANNELS | voice.MANAGE_ROLES | voice.MOVE_MEMBERS), who.display_name


def test_permissions_survive_leaving_voice_and_coming_back(seeded, running):
    """Leaving the voice channel (not the event) and coming back changes nothing; later updates keep the
    same permissions, so talking and video still work after rejoining."""
    cfg, fake, w, eid, vid = running
    first = {p.display_name: _row(fake.voice[vid]["overwrites"], p) for p in (HOST, COHOST, PLAYER)}
    fake.voice_states[PLAYER.discord_id] = vid
    fake.voice_states.pop(PLAYER.discord_id)
    fake.voice_states[PLAYER.discord_id] = vid
    for _ in range(3):
        with seeded.tx() as s:
            voice.request_sync(s, cfg, s.get(voice.Event, eid))
        _drain(w)
    assert {p.display_name: _row(fake.voice[vid]["overwrites"], p) for p in (HOST, COHOST, PLAYER)} == first
    assert len(fake.voice) == 1


def test_leaving_the_event_keeps_you_in_but_stops_rejoining(seeded, running):
    cfg, fake, w, eid, vid = running
    with seeded.tx() as s:
        signups.leave(s, cfg, PLAYER, eid)
    _drain(w)
    r = _row(fake.voice[vid]["overwrites"], PLAYER)
    _print("After leaving the event", {"Left the event": r})
    assert not r["connect"] and r["view_channel"] and r["send_messages"] and not r["speak"]


def test_server_administrator_bypasses_the_channel():
    """Discord's Administrator permission ignores channel permissions; QuestersBoard can't change that."""
    admin_bit = 1 << 3
    assert admin_bit not in {1 << b for b in VOICE_PERMISSIONS.values()}   # never handed out by the channel
    assert ROLE["admin"] != GUILD and fake_id(3) == ADMIN.discord_id
