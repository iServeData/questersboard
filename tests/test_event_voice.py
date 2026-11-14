"""FU-1 event voice channels: opened on Start, signed-up players and hosts get access, staff roles can
join without signing up, leavers may stay while connected but can't rejoin (or are disconnected with
remove_on_leave), End/Cancel remove the channel, and
an empty channel is removed after the scheduled end (event keeps running; hosts can reopen).
All names are made up; Discord is a FIXTURE (no live calls)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import ROLE, actor, at, fake_id, make_cfg, needs_db
from fake_discord import FakeDiscord
from helpers import char, event
from sqlalchemy import select, text

from questersboard.bot import render
from questersboard.config import ConfigError, ConfigHolder
from questersboard.domain import events, outbox, signups, voice
from questersboard.domain.errors import DomainError
from questersboard.integrations import discord_audit
from questersboard.models import Event, Outbox, PublicEventVoice
from questersboard.worker import Worker

pytestmark = needs_db
ORG = actor(1, "event_creator", name="HostOne")
CH, CAT, ADMINS, GUILD = fake_id(91), fake_id(92), fake_id(93), fake_id(97)


def _cfg(**voice_over):
    return make_cfg(community={"guild_id": GUILD}, discord={"event_channel_id": CH}, event_threads={"enabled": True},
                    voice={"enabled": True, "category_id": CAT, "staff_role_ids": [ADMINS], **voice_over})


def _drain(w, n=30):
    for _ in range(n):
        if not w.tick():
            break


def _ev(s, eid):
    s.expire_all()
    return s.get(Event, eid)


def _started(seeded, cfg, monkeypatch, w, *players):
    """An event with players signed up (first confirmed, second tentative (holds no spot), third waitlisted), started."""
    monkeypatch.setattr(events, "now", lambda: at(0))
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2, title="Mine run", late_join=True, participant_limit=1 if len(players) > 2 else None)
        for i, p in enumerate(players):
            signups.join(s, cfg, p, ev.id, char(s, cfg, p, ["Grimtusk", "Zulmara", "Vashka", "Corwin"][i]).id,
                         tentative=(i == 1))
        events.start(s, cfg, ORG, ev.id)
        eid = ev.id
    _drain(w)
    return eid


def _bot_alive(seeded, when):
    with seeded.tx() as s:
        s.execute(text("INSERT INTO service_heartbeats (name, state, beat_at, started_at) "
                       "VALUES ('bot', 'connected', :t, :t) ON CONFLICT (name) DO UPDATE SET beat_at = :t, "
                       "state = 'connected'"), {"t": when})


def test_off_by_default_and_config_rules(seeded, worker_db, monkeypatch):
    cfg = make_cfg(discord={"event_channel_id": CH})
    assert cfg["voice"]["enabled"] is False
    fake = FakeDiscord()
    _started(seeded, cfg, monkeypatch, Worker(ConfigHolder(cfg), worker_db, fake.client()))
    assert not fake.voice
    with pytest.raises(ConfigError, match="category_id"):
        make_cfg(voice={"enabled": True})
    with pytest.raises(ConfigError, match="staff_role_ids"):
        make_cfg(voice={"staff_role_ids": ["admins"]})
    with pytest.raises(ConfigError, match="unknown"):
        make_cfg(voice={"enabeld": True})
    assert _cfg().voice_staff_roles() == {ADMINS, ROLE["event_moderator"]}
    assert _cfg(moderators_can_join=False).voice_staff_roles() == {ADMINS}


def test_start_opens_one_private_channel_for_signed_up_people(seeded, worker_db, monkeypatch):
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    a, b, c = actor(80), actor(81), actor(82)
    eid = _started(seeded, cfg, monkeypatch, w, a, b, c)
    assert len(fake.voice) == 1
    vid, v = next(iter(fake.voice.items()))
    assert (v["name"], v["type"], v["parent"], v["limit"]) == ("Mine run", 2, CAT, 0)
    ow = v["overwrites"]
    assert ow[GUILD] == (0, 0, voice.VIEW | voice.CONNECT)                      # @everyone (guild id) hidden
    assert ow[fake.bot_id][1] & voice.MANAGE_CHANNELS and ow[fake.bot_id][1] & voice.CONNECT
    assert fake.bot_role in ow                                                  # copied Manage Permissions kept
    assert fake.other_role not in ow                                            # category's other roles removed
    posts = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/channels")]
    assert len(posts) == 1                                                      # created bare in the category
    for role in (ADMINS, ROLE["event_moderator"]):                            # staff join without signing up
        assert ow[role] == (0, voice.PARTICIPANT, 0)
    people = {k for k, (kind, _, _) in ow.items() if kind == 1} - {fake.bot_id}
    assert people == {fake_id(1), fake_id(80), fake_id(81)}                   # host, confirmed, tentative
    assert voice.PARTICIPANT & voice.STREAM and voice.PARTICIPANT & voice.SEND and voice.PARTICIPANT & voice.HISTORY
    with seeded.tx() as s:
        ev = _ev(s, eid)
        assert ev.discord_voice_channel_id == vid and ev.voice_empty_since is not None
        msg = render.event_message(s, cfg, ev)
        assert msg["components"][1]["components"][0]["label"] == "Join voice"
        assert voice.can_join(s, cfg, actor(99, "event_moderator"), ev)       # moderator, not signed up
        assert not voice.can_join(s, cfg, actor(99), ev)
    th = next(iter(fake.threads.values()))
    assert any(f"<#{vid}>" in p for p in th["posts"])
    _drain(w)                                                                   # repeat syncs: still one channel
    with seeded.tx() as s:
        voice.request_sync(s, cfg, _ev(s, eid))
    _drain(w)
    assert len(fake.voice) == 1


def test_join_after_start_and_leave_disconnects_with_remove_on_leave(seeded, worker_db, monkeypatch):
    cfg, fake = _cfg(remove_on_leave=True), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    a, b, late, staff = actor(80), actor(81), actor(83), actor(84)
    eid = _started(seeded, cfg, monkeypatch, w, a, b)
    vid = next(iter(fake.voice))
    with seeded.tx() as s:
        signups.join(s, cfg, late, eid, char(s, cfg, late, "Elspeth").id)    # RSVP after launch
        signups.join(s, cfg, staff, eid, char(s, cfg, staff, "Brightmere").id)
    _drain(w)
    assert fake_id(83) in fake.voice[vid]["overwrites"]
    fake.voice_states.update({fake_id(80): vid, fake_id(81): "elsewhere", fake_id(84): vid})
    fake.member_roles[fake_id(84)] = [ADMINS]
    with seeded.tx() as s:
        for p in (a, b, staff):
            signups.leave(s, cfg, p, eid)
    _drain(w)
    ow = fake.voice[vid]["overwrites"]
    assert not {fake_id(80), fake_id(81), fake_id(84)} & set(ow)
    assert fake.disconnects == [fake_id(80)]                 # in our channel; the other is elsewhere; staff stay
    assert fake.voice_states[fake_id(81)] == "elsewhere" and fake.voice_states[fake_id(84)] == vid


def test_leave_by_default_only_stops_rejoining(seeded, worker_db, monkeypatch):
    cfg, fake = _cfg(), FakeDiscord()
    assert cfg["voice"]["remove_on_leave"] is False
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    a = actor(80)
    eid = _started(seeded, cfg, monkeypatch, w, a)
    vid = next(iter(fake.voice))
    fake.voice_states[fake_id(80)] = vid
    with seeded.tx() as s:
        signups.leave(s, cfg, a, eid)
    _drain(w)
    kind, allow, deny = fake.voice[vid]["overwrites"][fake_id(80)]
    assert (kind, allow, deny) == (1, voice.LEFT, 0) and not allow & voice.CONNECT and allow & voice.SPEAK
    assert not fake.disconnects and fake.voice_states[fake_id(80)] == vid       # still talking
    with seeded.tx() as s:
        assert _ev(s, eid).discord_voice_members[fake_id(80)] == "left"
        signups.join(s, cfg, a, eid, char(s, cfg, a, "Vashka").id)            # signs up again: can rejoin
    _drain(w)
    assert fake.voice[vid]["overwrites"][fake_id(80)] == (1, voice.PARTICIPANT, 0)


@pytest.mark.parametrize("how", ["end", "cancel"])
def test_end_or_cancel_removes_the_channel(seeded, worker_db, monkeypatch, how):
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    eid = _started(seeded, cfg, monkeypatch, w, actor(80))
    assert fake.voice
    with seeded.tx() as s:
        events.end(s, cfg, ORG, eid) if how == "end" else events.cancel(s, cfg, ORG, eid, "rain")
    _drain(w)
    assert not fake.voice
    with seeded.tx() as s:
        ev = _ev(s, eid)
        assert ev.discord_voice_channel_id is None
        assert len(render.event_message(s, cfg, ev)["components"]) == 1


def test_empty_after_scheduled_end_removes_channel_and_host_can_reopen(seeded, worker_db, monkeypatch):
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    eid = _started(seeded, cfg, monkeypatch, w, actor(80))
    vid = next(iter(fake.voice))
    with seeded.tx() as s:
        assert voice.record_counts(s, {vid: 3}, at(1)) == 1                   # people in voice
        assert _ev(s, eid).voice_empty_since is None
        pv = s.scalars(select(PublicEventVoice)).one()
        assert pv.in_voice == 3                                                # count only, for the status card
        voice.record_counts(s, {vid: 0}, at(1.95))                            # everyone left just before the end
    monkeypatch.setattr(voice, "now", lambda: at(2.05))
    _bot_alive(seeded, at(2.05))
    assert w.scan_voice() == 0                                                # only 3 minutes past the end
    monkeypatch.setattr(voice, "now", lambda: at(2.1))
    _bot_alive(seeded, at(2.1) - timedelta(hours=1))
    assert w.scan_voice() == 0                                                # bot not checking in: never guess
    _bot_alive(seeded, at(2.1))
    assert w.scan_voice() == 1
    _drain(w)
    assert not fake.voice
    with seeded.tx() as s:
        ev = _ev(s, eid)
        assert ev.state == "in_progress" and ev.voice_closed_at is not None   # the event keeps running
        voice.request_sync(s, cfg, ev)
    _drain(w)
    assert not fake.voice                                                     # not reopened by itself
    with seeded.tx() as s, pytest.raises(DomainError):
        voice.reopen(s, cfg, actor(80), eid)                                  # players can't reopen
    with seeded.tx() as s:
        voice.reopen(s, cfg, ORG, eid)
    _drain(w)
    assert len(fake.voice) == 1


def test_someone_in_voice_keeps_the_channel(seeded, worker_db, monkeypatch):
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    eid = _started(seeded, cfg, monkeypatch, w, actor(80))
    vid = next(iter(fake.voice))
    with seeded.tx() as s:
        voice.record_counts(s, {vid: 2}, at(1))
    monkeypatch.setattr(voice, "now", lambda: at(5))
    _bot_alive(seeded, at(5))
    assert w.scan_voice() == 0
    _drain(w)
    assert fake.voice and eid


def test_channel_deleted_in_discord_is_treated_as_closed(seeded, worker_db, monkeypatch):
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    eid = _started(seeded, cfg, monkeypatch, w, actor(80))
    fake.voice.clear()                                                        # a moderator deleted it by hand
    with seeded.tx() as s:
        signups.join(s, cfg, actor(81), eid, char(s, cfg, actor(81), "Corwin").id)
    _drain(w)
    with seeded.tx() as s:
        ev = _ev(s, eid)
        assert ev.discord_voice_channel_id is None and ev.voice_closed_at is not None
        dead = s.scalars(select(Outbox).where(Outbox.dead_at.is_not(None))).all()
        assert not dead
    assert not fake.voice


def test_permissions_report_checks_the_voice_category():
    cfg = _cfg()
    g = cfg.guild_id
    bot_role = fake_id(95)
    roles = [{"id": g, "permissions": "0"}, {"id": bot_role, "name": "QuestersBoard", "permissions": "0"}]
    cat = {"id": CAT, "name": "WoW Voice", "type": 4, "permission_overwrites": []}
    member = {"user": {"id": fake_id(96)}, "roles": [bot_role]}
    rep = discord_audit.build(cfg, g, roles, [cat], member, [], [])
    assert "Manage Channels" in rep.text() and "Move Members" in rep.text()
    cat["permission_overwrites"] = [{"id": bot_role, "type": 0, "allow": str(voice.BOT), "deny": "0"}]
    rep = discord_audit.build(cfg, g, roles, [cat], member, [], [])
    assert "Bot can open and manage event voice channels" in rep.text()


# ------------------------------------------------------------------ Discord buttons (FIXTURE interactions)
class _Resp:  # FIXTURE: records what the bot would send to Discord
    def __init__(self, log):
        self.log, self.done = log, False

    def is_done(self):
        return self.done

    async def send_message(self, **kw):
        self.done = True
        self.log.append((kw.get("content"), kw.get("view")))


def _click(n, roles=()):
    import discord
    from types import SimpleNamespace
    log = []
    user = SimpleNamespace(id=int(fake_id(n)), display_name=f"DiscordUser{n}", name=f"user{n}",
                           roles=[SimpleNamespace(id=int(r)) for r in (ROLE["member"], *roles)])
    return log, SimpleNamespace(user=user, guild_id=int(GUILD), channel_id=int(CH), response=_Resp(log),
                                followup=None, type=discord.InteractionType.component, message=None)


def _labels(view):
    return [getattr(i, "label", None) for i in view.children] if view else []


def test_organizer_and_join_voice_buttons(seeded, monkeypatch):
    import asyncio

    from questersboard.bot import client as bc
    cfg = _cfg()
    bc.Ctx.holder, bc.Ctx.db, bc.Ctx.rest = ConfigHolder(cfg), seeded, None
    host = actor(1, "event_creator")
    with seeded.tx() as s:
        ev = event(s, cfg, host, 0, 2, title="Mine run", late_join=True)
        p = actor(80)
        signups.join(s, cfg, p, ev.id, char(s, cfg, p, "Grimtusk").id)
        eid = ev.id
    log, inter = _click(80)                                        # a player gets the web link only
    asyncio.run(bc.organizer(inter, eid))
    assert "Manage this event on the web" in log[0][0] and log[0][1] is None
    log, inter = _click(1, [ROLE["event_creator"]])                # the host gets Start
    asyncio.run(bc.organizer(inter, eid))
    assert _labels(log[0][1])[:1] == ["Start event"]
    monkeypatch.setattr(events, "now", lambda: at(0))
    log, inter = _click(1, [ROLE["event_creator"]])
    asyncio.run(bc.organizer_action(inter, "start", eid))
    assert log[0][0].startswith("Started.") and "voice channel" in log[0][0]
    log, inter = _click(1, [ROLE["event_creator"]])
    asyncio.run(bc.organizer(inter, eid))
    assert _labels(log[0][1])[:1] == ["End event"]
    log, inter = _click(1, [ROLE["event_creator"]])
    asyncio.run(bc.organizer_action(inter, "end", eid))           # asks first
    assert _labels(log[0][1]) == ["Yes, end it"]
    with seeded.tx() as s:
        assert _ev(s, eid).state == "in_progress"
        s.execute(text("UPDATE events SET discord_voice_channel_id = 'v1' WHERE id = :e"), {"e": eid})
    log, inter = _click(80)
    asyncio.run(bc.join_voice(inter, eid))                          # signed up: gets the channel
    assert "<#v1>" in log[0][0] and _labels(log[0][1]) == ["Open voice channel"]
    log, inter = _click(85)
    asyncio.run(bc.join_voice(inter, eid))                          # not signed up: told to join first
    assert "for people signed up" in log[0][0] and log[0][1] is None
    log, inter = _click(86, [ADMINS])
    asyncio.run(bc.join_voice(inter, eid))                          # staff role: may join to help
    assert "<#v1>" in log[0][0]
    log, inter = _click(1, [ROLE["event_creator"]])
    asyncio.run(bc.organizer_action(inter, "endyes", eid))
    with seeded.tx() as s:
        assert _ev(s, eid).state == "completed"
    log, inter = _click(80)
    asyncio.run(bc.join_voice(inter, eid))
    assert "isn't open" in log[0][0]


def test_channel_is_created_bare_then_locked_and_checked_again(seeded, worker_db, monkeypatch):
    """Discord refuses (403 50013) a new channel that carries Manage Permissions or anything the bot lacks
    server-wide, so the channel copies the category, is locked, and is locked again a few seconds later."""
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    eid = _started(seeded, cfg, monkeypatch, w, actor(80), actor(81))
    vid = next(iter(fake.voice))
    fake.voice[vid]["overwrites"][fake.other_role] = (0, voice.VIEW, 0)        # Discord applied it late
    fake.voice[vid]["overwrites"].pop(fake_id(80))
    with seeded.tx() as s:
        outbox.enqueue(s, "discord.sync_voice", f"{eid}:relock", {"event_id": eid, "relock": True})
    _drain(w)
    ow = fake.voice[vid]["overwrites"]
    assert fake.other_role not in ow and ow[fake_id(80)] == (1, voice.PARTICIPANT, 0)
    assert ow[GUILD] == (0, 0, voice.VIEW | voice.CONNECT)
    assert len(fake.voice) == 1


def test_channel_removed_if_it_cant_be_locked(seeded, worker_db, monkeypatch):
    """No Manage Permissions on the category: the bare channel is deleted again, never left open."""
    cfg, fake = _cfg(), FakeDiscord()
    fake.category_overwrites = [o for o in fake.category_overwrites if o["id"] != fake.bot_role]
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    eid = _started(seeded, cfg, monkeypatch, w, actor(80))
    assert fake.voice == {}
    with seeded.tx() as s:
        assert _ev(s, eid).discord_voice_channel_id is None


def _left_over_channel(fake, name, parent=CAT):
    """FIXTURE: a voice channel an earlier, failed attempt left in Discord."""
    vid = "v" + str(next(fake.ids))
    fake.voice[vid] = {"name": name, "type": 2, "parent": parent, "limit": 0,
                       "overwrites": {o["id"]: (o["type"], int(o["allow"]), int(o["deny"]))
                                      for o in fake.category_overwrites}}
    return vid


def test_rate_limit_while_adding_members_never_makes_a_second_channel(seeded, worker_db, monkeypatch):
    import time
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    fake.fail_on = [("PUT", f"/permissions/{fake_id(80)}", 429), ("PUT", f"/permissions/{fake_id(81)}", 429)]
    eid = _started(seeded, cfg, monkeypatch, w, actor(80), actor(81))
    for _ in range(6):
        time.sleep(0.6)                                  # the fixture's retry_after, plus the 0.5 s margin
        _drain(w)
    assert len(fake.voice) == 1
    assert len([c for c in fake.calls if c[0] == "POST" and c[1].endswith("/channels")]) == 1
    vid, v = next(iter(fake.voice.items()))
    assert {fake_id(1), fake_id(80), fake_id(81)} <= set(v["overwrites"])
    with seeded.tx() as s:
        ev = _ev(s, eid)
        assert ev.discord_voice_channel_id == vid
        assert set(ev.discord_voice_members) == {fake_id(1), fake_id(80), fake_id(81)}


def test_left_over_channel_is_taken_back_and_extra_copies_removed(seeded, worker_db, monkeypatch):
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    first, second = _left_over_channel(fake, "Mine run"), _left_over_channel(fake, "Mine run")
    other = _left_over_channel(fake, "Something else")
    elsewhere = _left_over_channel(fake, "Mine run", parent=fake_id(55))
    eid = _started(seeded, cfg, monkeypatch, w, actor(80))
    assert not [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/channels")]
    assert first in fake.voice and second not in fake.voice
    assert other in fake.voice and elsewhere in fake.voice           # other names and categories left alone
    ow = fake.voice[first]["overwrites"]
    assert ow[GUILD] == (0, 0, voice.VIEW | voice.CONNECT) and fake.other_role not in ow    # locked again
    assert fake_id(80) in ow
    with seeded.tx() as s:
        assert _ev(s, eid).discord_voice_channel_id == first


def test_end_removes_left_over_copies(seeded, worker_db, monkeypatch):
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    eid = _started(seeded, cfg, monkeypatch, w, actor(80))
    copy = _left_over_channel(fake, "Mine run")
    with seeded.tx() as s:
        events.end(s, cfg, ORG, eid)
    _drain(w)
    assert copy not in fake.voice and not fake.voice


def test_participant_permissions_from_config(seeded, worker_db, monkeypatch):
    assert voice.PARTICIPANT & voice.EMBED and voice.PARTICIPANT & voice.APP_COMMANDS      # default set
    with pytest.raises(ConfigError, match="participant_permissions"):
        _cfg(participant_permissions=["view_channel", "connect", "mute_members"])
    with pytest.raises(ConfigError, match="view_channel and connect"):
        _cfg(participant_permissions=["speak"])
    cfg, fake = _cfg(participant_permissions=["view_channel", "connect", "speak", "attach_files"]), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    _started(seeded, cfg, monkeypatch, w, actor(80))
    ow = next(iter(fake.voice.values()))["overwrites"]
    want = voice.VIEW | voice.CONNECT | voice.SPEAK | (1 << 15)
    assert ow[fake_id(80)] == (1, want, 0) and ow[ADMINS] == (0, want, 0)
    assert ow[fake.bot_id][1] & want == want


def test_form_action_allows_the_battlenet_login():
    from questersboard.web.app import form_action
    on = make_cfg(blizzard={"enabled": True, "battlenet_link_enabled": True, "oauth_base": "https://oauth.battle.net"})
    assert form_action(on) == "'self' https://discord.com https://oauth.battle.net https://*.battle.net"
    assert form_action(make_cfg()).startswith("'self' https://discord.com")


def test_copies_are_removed_on_the_next_update_while_running(seeded, worker_db, monkeypatch):
    cfg, fake = _cfg(), FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    eid = _started(seeded, cfg, monkeypatch, w, actor(80))
    vid = next(iter(fake.voice))
    _left_over_channel(fake, "Mine run")
    with seeded.tx() as s:
        signups.join(s, cfg, actor(81), eid, char(s, cfg, actor(81), "Fixturelate").id)
    _drain(w)
    assert set(fake.voice) == {vid} and fake_id(81) in fake.voice[vid]["overwrites"]
