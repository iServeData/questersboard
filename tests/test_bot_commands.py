"""/board, /craft who, /character refresh and /questersboard help (FIXTURE interactions, FIXTURE Blizzard
answers; nothing live is exercised)."""
from __future__ import annotations

import asyncio
import copy
import tomllib
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import make_cfg, needs_db
from fake_discord import FakeDiscord
from helpers import char
from sqlalchemy import select, text
from test_boards import HELPER, OTHER, POSTER, RECIPE, _adv, _crafter, _offer

from questersboard.config import ConfigError, ConfigHolder, validate
from questersboard.domain import boards, commands, profiles
from questersboard.domain.errors import Invalid, NotFound
from questersboard.domain.timeutil import now
from questersboard.models import CharacterProfile, Outbox
from questersboard.worker import Worker

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 20)


# ------------------------------------------------------------------ no database

def test_commands_config_defaults_and_limits():
    cm = make_cfg()["commands"]
    assert cm == {"board_list_max": 10, "craft_who_max": 10, "refresh_wait_seconds": 20, "refresh_again_minutes": 5}
    raw = tomllib.loads((ROOT / "deploy" / "config.example.toml").read_text())
    assert raw["commands"] == cm                                   # the example lists every default
    for bad in ({"board_list_max": 0}, {"craft_who_max": 21}, {"refresh_wait_seconds": 61},
                {"refresh_again_minutes": -1}, {"board_list_max": True}, {"bogus": 1}):
        r = copy.deepcopy(raw)
        r["commands"] = {**r["commands"], **bad}
        with pytest.raises(ConfigError, match=r"\[commands\]"):
            validate(r)


def test_parse_times_reads_24_hour_lines():
    got, bad = commands.parse_times("Sat 28 Nov 19:00-22:00\nNov 29 20:00 to 23:30\n2027-01-02 2200-0100", TODAY)
    assert got == [("2026-11-28", "19:00", "22:00"), ("2026-11-29", "20:00", "23:30"), ("2027-01-02", "22:00", "01:00")]
    assert not bad
    got, bad = commands.parse_times("3 Jan 19:00-21:00\n7pm to 9pm\nsoon\n28 Nov 7:00 pm-9:00 pm\n31 Feb 19:00-20:00", TODAY)
    assert got == [("2027-01-03", "19:00", "21:00")]               # no year: the next one
    assert bad == ["7pm to 9pm", "soon", "28 Nov 7:00 pm-9:00 pm", "31 Feb 19:00-20:00"]
    got, bad = commands.parse_times("\n".join(f"{d} Nov 19:00-20:00" for d in (21, 22, 23, 24)), TODAY)
    assert len(got) == boards.MAX_WINDOWS and bad == ["24 Nov 19:00-20:00"]
    assert commands.parse_times("21 Nov 25:00-26:00", TODAY) == ([], ["21 Nov 25:00-26:00"])


def test_post_reply_links_the_filled_in_form():
    cfg = make_cfg()
    out = commands.post_reply(cfg, "company", "Fixture Caverns run", "Bring\nsnacks",
                              "Sat 28 Nov 19:00-22:00\nlater", "America/Los_Angeles", TODAY)
    assert "filled in on the Adventure Board" in out and "Post goal" in out
    assert "Sat 28 Nov 19:00 to 22:00" in out and "UTC" in out        # 24-hour, with the UTC line
    assert "Not understood" in out and '"later"' in out
    assert " AM" not in out and " PM" not in out and "draft" not in out.lower()
    url = out.rsplit("<", 1)[1].rstrip(">")
    parts = urlsplit(url)
    assert url.startswith("https://community.example.org/wow/quester/adventures?")
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {"new": "1", "kind": "company", "title": "Fixture Caverns run", "body": "Bring snacks",
                 "timezone": "America/Los_Angeles", "date_0": "2026-11-28", "from_0": "19:00", "to_0": "22:00"}
    with pytest.raises(Invalid):
        commands.post_link(cfg, "need_crafted", "x", "", [], "UTC")   # profession posts stay on the site


def test_help_lists_every_command_and_links():
    cfg = make_cfg(blizzard={"enabled": True})
    out = commands.help_text(cfg)
    for cmd in ("/board list", "/board my", "/board post", "/craft who", "/character refresh", "/quest join"):
        assert cmd in out, cmd
    assert "https://community.example.org/wow/quester/help" in out and "/privacy>" in out
    assert "webhook" not in out.lower() and "outbox" not in out.lower() and "!" not in out
    off = commands.help_text(make_cfg(adventures={"enabled": False}, professions={"enabled": False}))
    assert "/board" not in off and "/craft who" not in off


# ------------------------------------------------------------------ boards

@needs_db
def test_board_list_and_my(seeded, cfg):
    with seeded.tx() as s:
        assert "Nothing open on the Adventure Board" in commands.board_list(s, cfg, POSTER, "adventure")
        p, _ = _adv(s, cfg, title="Fixture [Caverns] run", kind="company")
        boards.respond(s, cfg, HELPER, str(p.public_id), "need")
        out = commands.board_list(s, cfg, OTHER, "adventure")
        assert "**Adventure Board: 1 open post**" in out and "Looking for company" in out
        assert "Fixture \\[Caverns\\] run" in out and f"/board/{p.public_id}>" in out and "1 is in" in out
        assert "Fixturebrann" in out
        with pytest.raises(Invalid):
            commands.board_list(s, cfg, OTHER, "nope")
        mine = commands.board_my(s, cfg, POSTER)
        assert "**Your posts**" in mine and "You answered" not in mine and "1 is in" in mine
        theirs = commands.board_my(s, cfg, HELPER)
        assert "**You answered**" in theirs and "I'm in" in theirs and "Your posts" not in theirs
        assert "no open posts" in commands.board_my(s, cfg, OTHER)


@needs_db
def test_board_list_is_capped(seeded, tmp_path):
    cfg = make_cfg(tmp_path, commands={"board_list_max": 1})
    with seeded.tx() as s:
        _adv(s, cfg, title="Fixture one")
        _adv(s, cfg, who=OTHER, name="Fixtureother", title="Fixture two")
        out = commands.board_list(s, cfg, HELPER, "adventure")
    assert "2 open posts" in out and "and 1 more" in out and out.count("\u2022") == 1


# ------------------------------------------------------------------ craft who

@needs_db
def test_craft_who_lists_shared_crafters_and_offers(seeded, cfg):
    with seeded.tx() as s:
        c = _crafter(s, cfg)
        _crafter(s, cfg, who=OTHER, name="Fixturehidden", share="private")
        _offer(s, cfg, c, fee="fee", fee_gold="2")
        assert commands.craft_suggest(s, cfg, POSTER, "armor") == [(RECIPE, "Leatherworking")]
        assert commands.craft_suggest(s, cfg, POSTER, "nothing like it") == []
        out, ask = commands.craft_who(s, cfg, POSTER, "fixture armor kit")
        assert "**1 person can craft Fixture Armor Kit**" in out and "**Fixturesmith**" in out
        assert "Leatherworking" in out and "offers it: 2g" in out and "Fixturehidden" not in out
        assert ask == "https://community.example.org/wow/quester/professions?new=need&title=fixture+armor+kit"
        none, _ = commands.craft_who(s, cfg, POSTER, "Fixture Moonblade")
        assert "Nobody who shares their professions has **Fixture Moonblade**" in none
        with pytest.raises(Invalid):
            commands.craft_who(s, cfg, POSTER, "x")
    assert commands.craft_page(cfg, "Fixture Armor Kit").endswith("/players?craft=Fixture+Armor+Kit#craft")


@needs_db
def test_web_forms_open_filled_in_from_the_command_links(seeded, tmp_path):
    from test_boards import _web
    a, cfg, tw = _web(seeded, tmp_path)
    c = tw.login(a, 40)
    with seeded.tx() as s:
        char(s, cfg, POSTER, "Fixtureweb")
    url = commands.post_link(cfg, "company", "Fixture <b>run</b>", "Keep me", [("2026-11-28", "19:00", "22:00")],
                             "America/Los_Angeles")
    page = c.get(urlsplit(url).path + "?" + urlsplit(url).query).text
    for frag in ('value="Fixture &lt;b&gt;run&lt;/b&gt;"', ">Keep me<", 'value="2026-11-28"', 'value="19:00"',
                 'value="22:00"', 'value="company" selected'):
        assert frag in page, frag
    assert "<b>run</b>" not in page
    with seeded.tx() as s:
        assert s.scalar(select(boards.BoardPost.id).limit(1)) is None   # nothing saved by opening the link
    page = c.get("/wow/quester/professions?new=need&title=Fixture+Armor+Kit").text
    assert 'value="Fixture Armor Kit"' in page and 'id="prof-need"' in page


# ------------------------------------------------------------------ character refresh

def _profile(s, cfg, who, name, minutes_ago=60):
    c = _crafter(s, cfg, who=who, name=name)
    s.execute(text("update character_profiles set fetched_at = now() - make_interval(mins => :m) "
                   "where character_id = :i"), {"m": minutes_ago, "i": c.id})
    return c


@needs_db
def test_refresh_request_checks_owner_and_gap(seeded, tmp_path):
    cfg = make_cfg(tmp_path, blizzard={"enabled": True})
    with seeded.tx() as s:
        mine = _profile(s, cfg, POSTER, "Fixturemine")
        recent = _profile(s, cfg, POSTER, "Fixturerecent", minutes_ago=1)
        char(s, cfg, POSTER, "Fixturebare")                                  # nothing from Blizzard
        theirs = _profile(s, cfg, OTHER, "Fixturetheirs")
        assert [n for _i, n in commands.refresh_choices(s, cfg, POSTER)] == \
            ["Fixturemine \u00b7 WoW: Forever", "Fixturerecent \u00b7 WoW: Forever"]
        assert commands.refresh_choices(s, cfg, HELPER) == []
        with pytest.raises(NotFound):
            commands.refresh_request(s, cfg, POSTER, theirs.id)
        assert commands.refresh_request(s, cfg, POSTER, recent.id)[0] == "recent"
        state, name, before = commands.refresh_request(s, cfg, POSTER, mine.id)
        assert (state, name) == ("queued", "Fixturemine")
        job = s.scalars(select(Outbox).where(Outbox.kind == commands.REFRESH_JOB)).one()
        assert job.key == str(mine.id) and job.payload == {"character_id": mine.id, "member_id": mine.member_id}
        assert commands.refresh_state(s, mine.id, before) == "waiting"
    with seeded.tx() as s, pytest.raises(Invalid):
        commands.refresh_request(s, make_cfg(tmp_path), POSTER, mine.id)    # Blizzard off


@needs_db
def test_web_service_refreshes_for_the_owner_or_gives_up(seeded, worker_db, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from questersboard.integrations.blizzard import ApiError
    from questersboard.web import web_jobs
    cfg = make_cfg(tmp_path, blizzard={"enabled": True})
    calls, fail = [], set()

    def fake_refresh(s, cfg_, bz, cid):                     # FIXTURE: stands in for Blizzard's answer
        calls.append(cid)
        if cid in fail:
            raise ApiError("FIXTURE: Blizzard unavailable")
        s.get(CharacterProfile, cid).fetched_at = now()
    monkeypatch.setattr(profiles, "operator_refresh", fake_refresh)
    with seeded.tx() as s:
        ok = _profile(s, cfg, POSTER, "Fixtureok")
        bad = _profile(s, cfg, POSTER, "Fixturebad")
        moved = _profile(s, cfg, POSTER, "Fixturemoved")
        fail.add(bad.id)
        befores = {c.id: commands.refresh_request(s, cfg, POSTER, c.id)[2] for c in (ok, bad, moved)}
        other = _profile(s, cfg, OTHER, "Fixturenewowner")              # it changed hands after the ask
        s.execute(text("update character_profiles set member_id = :m where character_id = :i"),
                  {"m": other.member_id, "i": moved.id})
    w = Worker(ConfigHolder(cfg), worker_db, FakeDiscord().client())
    assert w.tick() == 0                                   # the worker leaves them for the web service
    web = SimpleNamespace(db=seeded, cfg=cfg, bz=object())
    assert web_jobs.run_once(web) == 3 and web_jobs.run_once(web) == 0
    with seeded.tx() as s:
        assert commands.refresh_state(s, ok.id, befores[ok.id]) == "done"
        assert commands.refresh_state(s, bad.id, befores[bad.id]) == "failed"
        assert commands.refresh_state(s, moved.id, befores[moved.id]) == "failed"
        assert "Refreshed **Fixtureok** from Blizzard" in commands.refresh_done_text(s, cfg, ok.id)
        jobs = {j.key: j for j in s.scalars(select(Outbox).where(Outbox.kind == commands.REFRESH_JOB))}
        assert jobs[str(bad.id)].attempts == 1 and jobs[str(bad.id)].dead_at is not None   # never retried
        assert jobs[str(ok.id)].done_at is not None
    assert moved.id not in calls and sorted(calls) == sorted([ok.id, bad.id])


# ------------------------------------------------------------------ the Discord side

@needs_db
def test_slash_commands_reply_ephemeral(seeded, cfg):
    from test_bot_character_form import _inter

    from questersboard.bot import client as bc
    bc.Ctx.holder, bc.Ctx.db, bc.Ctx.rest = ConfigHolder(cfg), seeded, None
    tree = bc.build_tree(_client())
    cmds = {c.qualified_name: c for c in tree.walk_commands()}
    for name in ("board list", "board my", "board post", "craft who", "character refresh", "questersboard help"):
        assert name in cmds, name
    log = []
    asyncio.run(cmds["questersboard help"].callback(_inter(log)))
    assert log[-1][0] == "send" and "QuestersBoard quick guide" in log[-1][1]
    log = []
    asyncio.run(cmds["board my"].callback(_inter(log)))
    assert "no open posts" in log[-1][1]
    log = []
    asyncio.run(cmds["board post"].callback(_inter(log), _Choice("company")))
    assert log[-1][0] == "modal" and isinstance(log[-1][2], bc.BoardPostModal)
    assert log[-1][2].tz == cfg["community"]["default_timezone"]


class _Choice:
    def __init__(self, value):
        self.value, self.name = value, value


def _client():
    import discord
    return discord.Client(intents=discord.Intents.none())

