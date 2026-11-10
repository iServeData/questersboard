"""Guest voice links, host /voice kick|ban|unban, and the How it works page.
All names are made up; Discord and Discord sign-in are FIXTURES (no live calls)."""
from __future__ import annotations

import re

import pytest
from conftest import ROLE, actor, at, fake_id, make_cfg, needs_db
from fake_discord import FakeDiscord
from fastapi.testclient import TestClient
from helpers import char, event
from sqlalchemy import func, select
from test_event_voice import CAT, CH, GUILD, ORG, _drain, _ev

from wowquester.config import ConfigError, ConfigHolder
from wowquester.domain import events, signups, voice
from wowquester.domain.errors import DomainError, Invalid, NotFound, PermissionDenied
from wowquester.integrations import discord_audit
from wowquester.models import AuditLog, EventVoiceBan, EventVoiceGuest, EventVoiceInvite, Outbox
from wowquester.web.app import create_app
from wowquester.worker import Worker

pytestmark = needs_db
ADMINS, OFFICERS = fake_id(93), fake_id(94)
GUEST, GUEST2, GUEST3 = fake_id(150), fake_id(151), fake_id(152)


def _cfg(**over):
    return make_cfg(community={"guild_id": GUILD}, discord={"event_channel_id": CH}, event_threads={"enabled": True},
                    voice={"enabled": True, "category_id": CAT, "staff_role_ids": [ADMINS],
                           "protected_role_ids": [OFFICERS], **over})


def _run(seeded, cfg, monkeypatch, w, cohost=None):
    """A started event: HostOne hosts, Grimtusk (actor 80) is signed up, optional co-host."""
    monkeypatch.setattr(events, "now", lambda: at(0))
    with seeded.tx() as s:
        kw = {"cohosts": [cohost.discord_id]} if cohost else {}
        ev = event(s, cfg, ORG, 0, 2, title="Mine run", late_join=True, **kw)
        p = actor(80)
        signups.join(s, cfg, p, ev.id, char(s, cfg, p, "Grimtusk").id)
        events.start(s, cfg, ORG, ev.id)
        eid = ev.id
    _drain(w)
    return eid


def _setup(seeded, worker_db, monkeypatch, cohost=None, **over):
    cfg, fake = _cfg(**over), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    eid = _run(seeded, cfg, monkeypatch, w, cohost)
    return cfg, fake, w, eid, next(iter(fake.voice))


def test_config_rules():
    cfg = make_cfg()
    v = cfg["voice"]
    assert (v["remove_on_leave"], v["guest_invites"], v["max_guests_per_invite"], v["invite_dm"],
            v["guest_link_template"]) == (False, True, 5, "off", "")
    for bad in ({"invite_dm": "always"}, {"max_guests_per_invite": 0}, {"max_guests_per_invite": 26},
                {"guest_link_template": "http://short.example.org/v/{code}"},
                {"guest_link_template": "https://short.example.org/v/"},
                {"protected_role_ids": ["officers"]}):
        with pytest.raises(ConfigError):
            make_cfg(voice=bad)
    c = _cfg()
    assert c.voice_protected_roles() >= {OFFICERS, ADMINS, ROLE["admin"], ROLE["event_moderator"]}


def test_guest_link_one_per_person_and_only_for_people_in_the_event(seeded, worker_db, monkeypatch):
    cfg, fake, w, eid, vid = _setup(seeded, worker_db, monkeypatch)
    with seeded.tx() as s:
        _, code = voice.invite_for(s, cfg, actor(80), eid)
        assert re.fullmatch(f"[{voice.CODE_ALPHABET}]{{{voice.CODE_LEN}}}", code)
        assert voice.invite_for(s, cfg, actor(80), eid)[1] == code                 # same link again
        assert voice.invite_for(s, cfg, ORG, eid)[1] != code                       # the host has their own
        assert voice.invite_for(s, cfg, actor(86, "event_moderator"), eid)[1]     # staff can too
        with pytest.raises(PermissionDenied):
            voice.invite_for(s, cfg, actor(99), eid)                               # not in the event
        assert voice.guest_url(cfg, code) == f"https://community.example.org/wow/quester/v/{code}"
        assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "voice.invite")) == 3
    short = _cfg(guest_link_template="https://short.example.org/v/{code}")
    assert voice.guest_url(short, "abcd2345") == "https://short.example.org/v/abcd2345"
    with seeded.tx() as s, pytest.raises(Invalid):
        voice.invite_for(s, _cfg(guest_invites=False), actor(80), eid)


def test_redeem_gives_access_until_the_event_ends(seeded, worker_db, monkeypatch):
    cfg, fake, w, eid, vid = _setup(seeded, worker_db, monkeypatch, max_guests_per_invite=2)
    with seeded.tx() as s:
        _, code = voice.invite_for(s, cfg, actor(80), eid)
        assert voice.redeem(s, cfg, code.upper(), GUEST).channel_id == vid
        voice.redeem(s, cfg, code, GUEST)                                          # same guest again: fine
        voice.redeem(s, cfg, code, GUEST2)
        with pytest.raises(PermissionDenied, match="as many people"):
            voice.redeem(s, cfg, code, GUEST3)                                     # 2 per link
        with pytest.raises(NotFound):
            voice.check_invite(s, cfg, "nosuchcd")
    _drain(w)
    ow = fake.voice[vid]["overwrites"]
    assert ow[GUEST] == ow[GUEST2] == (1, voice.PARTICIPANT, 0)
    with seeded.tx() as s:
        events.end(s, cfg, ORG, eid)
        for model in (EventVoiceInvite, EventVoiceGuest):
            assert s.scalar(select(func.count()).select_from(model).where(model.event_id == eid)) == 0
        with pytest.raises(NotFound, match="ended"):
            voice.check_invite(s, cfg, code)
    _drain(w)
    assert not fake.voice


def test_host_moderation_and_protections(seeded, worker_db, monkeypatch):
    co = actor(70, name="CoHost")
    cfg, fake, w, eid, vid = _setup(seeded, worker_db, monkeypatch, cohost=co)
    with seeded.tx() as s:
        _, code = voice.invite_for(s, cfg, actor(80), eid)
        voice.redeem(s, cfg, code, GUEST)
    _drain(w)
    fake.voice_states[GUEST] = vid
    T = voice.Target
    with seeded.tx() as s:
        voice.moderate(s, cfg, co, eid, T(GUEST, frozenset()), "ban")             # co-host bans the guest
        with pytest.raises(PermissionDenied, match="removed"):
            voice.redeem(s, cfg, code, GUEST)                                      # can't come back by link
    _drain(w)
    assert fake.voice[vid]["overwrites"][GUEST] == (1, 0, voice.VIEW | voice.CONNECT)
    assert GUEST in fake.disconnects
    with seeded.tx() as s:
        voice.moderate(s, cfg, ORG, eid, T(GUEST, frozenset()), "unban")
        assert s.get(EventVoiceBan, (eid, GUEST)) is None
    _drain(w)
    assert fake.voice[vid]["overwrites"][GUEST] == (1, voice.PARTICIPANT, 0)      # still a guest
    fake.voice_states[fake_id(80)] = vid
    with seeded.tx() as s:
        voice.moderate(s, cfg, ORG, eid, T(fake_id(80), frozenset()), "kick")     # kick: disconnect only
    _drain(w)
    assert fake.disconnects.count(fake_id(80)) == 1
    assert fake.voice[vid]["overwrites"][fake_id(80)] == (1, voice.PARTICIPANT, 0)
    protected = [T(fake_id(60), frozenset(), is_owner=True), T(fake_id(61), frozenset(), is_bot=True),
                 T(fake_id(62), frozenset({OFFICERS})), T(fake_id(63), frozenset({ADMINS})),
                 T(fake_id(64), frozenset({ROLE["admin"]})), T(co.discord_id, frozenset())]
    with seeded.tx() as s:
        for t in protected:
            with pytest.raises(DomainError):
                voice.moderate(s, cfg, co, eid, t, "ban")
        with pytest.raises(PermissionDenied, match="host"):
            voice.moderate(s, cfg, co, eid, T(ORG.discord_id, frozenset()), "kick")   # co-host vs host
        voice.moderate(s, cfg, actor(71, "event_moderator"), eid, T(co.discord_id, frozenset()), "kick")
        with pytest.raises(PermissionDenied):
            voice.moderate(s, cfg, actor(80), eid, T(GUEST, frozenset()), "kick")     # players can't
        acts = set(s.scalars(select(AuditLog.action).where(AuditLog.action.like("voice.%"))))
        assert {"voice.ban", "voice.unban", "voice.kick"} <= acts
        assert voice.event_for_command(s, cfg, ORG, vid, manage=True) == eid
        assert voice.event_for_command(s, cfg, ORG, None, manage=True) == eid
        with pytest.raises(Invalid):
            voice.event_for_command(s, cfg, actor(99), None, manage=True)
        events.end(s, cfg, ORG, eid)
        with pytest.raises(Invalid, match="running"):
            voice.moderate(s, cfg, ORG, eid, T(GUEST, frozenset()), "ban")


@pytest.mark.parametrize("who", ["hosts", "everyone"])
def test_invite_dm_on_start(seeded, worker_db, monkeypatch, who):
    cfg, fake, w, eid, vid = _setup(seeded, worker_db, monkeypatch, invite_dm=who,
                                    guest_link_template="https://short.example.org/v/{code}")
    got = {uid for uid, txt in fake.dms if "Guest voice link for Mine run" in txt}
    assert got == ({ORG.discord_id} if who == "hosts" else {ORG.discord_id, fake_id(80)})
    txt = next(t for u, t in fake.dms if u == ORG.discord_id and "Guest voice link" in t)
    assert "https://short.example.org/v/" in txt and "sign in with Discord" in txt and "until the event ends" in txt


def test_invite_dm_off_by_default(seeded, worker_db, monkeypatch):
    cfg, fake, w, eid, vid = _setup(seeded, worker_db, monkeypatch)
    assert not [t for _, t in fake.dms if "Guest voice link" in t]


def test_housekeeping_drops_guest_rows_of_finished_events(seeded, worker_db, monkeypatch):
    cfg, fake, w, eid, vid = _setup(seeded, worker_db, monkeypatch)
    with seeded.tx() as s:
        _, code = voice.invite_for(s, cfg, actor(80), eid)
        voice.redeem(s, cfg, code, GUEST)
        _ev(s, eid).state = "completed"                     # ended without End (e.g. an old row)
    w.housekeeping()
    with seeded.tx() as s:
        assert s.scalar(select(func.count()).select_from(EventVoiceGuest)) == 0
        assert s.scalar(select(func.count()).select_from(EventVoiceInvite)) == 0


def test_permissions_report_checks_create_invite():
    cfg = _cfg()
    g, bot_role = cfg.guild_id, fake_id(95)
    roles = [{"id": g, "permissions": "0"}, {"id": bot_role, "name": "QuestersBoard", "permissions": "0"}]
    cat = {"id": CAT, "name": "WoW Voice", "type": 4,
           "permission_overwrites": [{"id": bot_role, "type": 0, "allow": str(voice.BOT), "deny": "0"}]}
    member = {"user": {"id": fake_id(96)}, "roles": [bot_role]}
    assert "Create Invite" in discord_audit.build(cfg, g, roles, [cat], member, [], []).text()
    roles[1]["permissions"] = str(discord_audit.CREATE_INVITE)
    assert "Bot can add guests" in discord_audit.build(cfg, g, roles, [cat], member, [], []).text()


# ------------------------------------------------------------------ /voice commands (FIXTURE interactions)
def test_voice_slash_commands(seeded, worker_db, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import discord
    from test_event_voice import _click

    from wowquester.bot import client as bc
    cfg, fake, w, eid, vid = _setup(seeded, worker_db, monkeypatch)
    bc.Ctx.holder, bc.Ctx.db, bc.Ctx.rest = ConfigHolder(cfg), seeded, None
    tree = bc.build_tree(discord.Client(intents=discord.Intents.none()))
    cmd = lambda name: tree.get_command("voice").get_command(name)  # noqa: E731
    log, inter = _click(80)
    inter.channel_id = int(fake_id(55))
    asyncio.run(cmd("invite").callback(inter))
    assert "Guest voice link for Mine run" in log[0][0] and "/wow/quester/v/" in log[0][0]

    def member(n, roles=(), bot=False):
        return SimpleNamespace(id=int(fake_id(n)), display_name=f"DiscordUser{n}", bot=bot,
                               roles=[SimpleNamespace(id=int(r)) for r in roles])
    for target, why in ((member(60), "owner"), (member(61, bot=True), "bot"), (member(62, [OFFICERS]), "officer")):
        log, inter = _click(1, [ROLE["event_creator"]])
        inter.guild = SimpleNamespace(owner_id=int(fake_id(60)))
        asyncio.run(cmd("ban").callback(inter, target))
        assert "can't kick or ban" in log[0][0], why
    log, inter = _click(1, [ROLE["event_creator"]])
    inter.guild = SimpleNamespace(owner_id=int(fake_id(60)))
    asyncio.run(cmd("ban").callback(inter, member(150)))
    assert "out of voice for **Mine run** until the event ends" in log[0][0]
    log, inter = _click(80)
    inter.guild = SimpleNamespace(owner_id=int(fake_id(60)))
    asyncio.run(cmd("kick").callback(inter, member(150)))
    assert "hosting" in log[0][0]                                          # players aren't hosts


# ------------------------------------------------------------------ web (FIXTURE OAuth and REST)
def _app(seeded, cfg):
    from test_web import FakeOAuth, FakeRest
    rest = FakeRest()
    a = create_app(ConfigHolder(cfg), seeded, FakeOAuth(), rest)
    a.state.fake_rest = rest
    return a, rest


def test_guest_web_flow(seeded, worker_db, monkeypatch, tmp_path):
    cfg, fake, w, eid, vid = _setup(seeded, worker_db, monkeypatch)
    cfg.raw["web"]["cookie_secure"] = False
    with seeded.tx() as s:
        _, code = voice.invite_for(s, cfg, actor(80), eid)
    app, rest = _app(seeded, cfg)
    c = TestClient(app, base_url="https://community.example.org", follow_redirects=False)
    r = c.get(f"/wow/quester/v/{code}")
    assert r.status_code == 200 and "Mine run" in r.text and "Continue with Discord" in r.text
    assert 'content="noindex,nofollow"' in r.text and "og:url" not in r.text      # links stay out of search
    assert "You don&#39;t need a QuestersBoard account" in r.text or "You don't need a QuestersBoard account" in r.text
    r = c.get(f"/wow/quester/v/{code}/go")
    assert r.status_code == 303 and "scope=identify guilds.join" in r.headers["location"]
    assert "prompt=consent" in r.headers["location"]
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    r = c.get(f"/wow/quester/oauth/callback?code={GUEST}&state={state}")
    assert r.status_code == 200 and "You're in" in r.text.replace("&#39;", "'")
    assert f"https://discord.com/channels/{GUILD}/{vid}" in r.text
    assert rest.added == [GUEST]                                       # not in the server: added
    with seeded.tx() as s:
        assert s.get(EventVoiceGuest, (eid, GUEST)) is not None
        assert s.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "discord.sync_voice"))
    assert c.get("/wow/quester/me").status_code in (303, 401, 403)      # no member session for guests
    assert c.get("/wow/quester/v/nosuchcd").status_code == 404
    with seeded.tx() as s:
        s.add(EventVoiceBan(event_id=eid, discord_id=GUEST2))
    r = c.get(f"/wow/quester/v/{code}/go")
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    r = c.get(f"/wow/quester/oauth/callback?code={GUEST2}&state={state}")
    assert r.status_code == 403 and GUEST2 not in rest.added            # banned: never added to the server
    with seeded.tx() as s:
        events.end(s, cfg, ORG, eid)
    assert c.get(f"/wow/quester/v/{code}").status_code == 404


def test_help_page_follows_config(seeded):
    app, _ = _app(seeded, _cfg(invite_dm="hosts", max_guests_per_invite=3))
    c = TestClient(app, base_url="https://community.example.org")
    r = c.get("/wow/quester/help")
    assert r.status_code == 200 and 'content="index,follow"' in r.text and "guest links" in r.text
    for bit in ("How it works", "Guest voice links", "/voice invite", "/voice ban", "up to 3 people",
                "Hosts also get their link", "get back in unless you join the event again"):
        assert bit in r.text, bit
    assert 'href="/wow/quester/help"' in r.text                          # footer link
    app, _ = _app(seeded, make_cfg())
    r = TestClient(app, base_url="https://community.example.org").get("/wow/quester/help")
    assert r.status_code == 200 and "Event voice" not in r.text and "Event chat" not in r.text
    priv = TestClient(_app(seeded, _cfg())[0], base_url="https://community.example.org").get("/wow/quester/privacy")
    assert "guest voice link" in priv.text
