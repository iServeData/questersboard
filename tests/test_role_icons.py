"""Role icons on event posts: the bot's own Discord emojis next to Tank / Healer / Damage, the upload
command (dry run, approval gate, never replaces) and the same icons on the event page.
Discord is a FIXTURE (no live calls); all names are made up."""
from __future__ import annotations

import httpx
import pytest
from conftest import actor, fake_id, make_cfg, needs_db
from fake_discord import FakeDiscord
from helpers import char, event
from test_cli import cfg_file  # noqa: F401  (fixture)
from test_web import app, wcfg  # noqa: F401  (fixtures)

from questersboard import cli
from questersboard.bot import render
from questersboard.config import ConfigError, ConfigHolder
from questersboard.domain import signups
from questersboard.integrations import discord_rest
from questersboard.worker import Worker

ORG = actor(1, "event_creator", name="HostOne")
CH = str(10 ** 17 + 92)
ROLES = [{"key": "tank", "label": "Tank", "quantity": 1}, {"key": "healer", "label": "Healer", "quantity": 1},
         {"key": "guide", "label": "Guide", "quantity": 1}]
EMOJIS = {"qb_tank": "e101", "qb_healer": "e102"}        # FIXTURE emoji ids (damage not uploaded)


def _roles(msg):
    return {f["name"].split(" (")[0]: f for f in msg["embeds"][0]["fields"] if "(" in f["name"]
            and f["name"].split(" (")[0].split()[-1] in ("Tank", "Healer", "Guide")}


def test_config_defaults_and_validation():
    cfg = make_cfg()
    assert cfg["event_posts"]["role_icons"] == {"tank": "qb_tank", "healer": "qb_healer", "damage": "qb_damage"}
    assert cfg["event_posts"]["role_icon_position"] == "heading"
    assert make_cfg(event_posts={"role_icons": {}})["event_posts"]["role_icons"] == {}
    with pytest.raises(ConfigError, match="role_icons"):
        make_cfg(event_posts={"role_icons": {"tank": "not ok!"}})
    with pytest.raises(ConfigError, match="role_icon_position"):
        make_cfg(event_posts={"role_icon_position": "left"})


@needs_db
def test_icon_before_the_heading_or_line_and_plain_fallback(seeded):
    cfg = make_cfg()
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2, roles=ROLES)
        p = actor(61)
        signups.join(s, cfg, p, ev.id, char(s, cfg, p, "Fixture Ironhide").id, role_key="tank", recognition=True)
        f = _roles(render.event_message(s, cfg, ev, EMOJIS))
        assert f["<:qb_tank:e101> Tank"]["value"].startswith("Fixture Ironhide")      # default: in the heading
        assert f["<:qb_healer:e102> Healer"]["value"].startswith("—")
        assert f["Guide"]["value"].startswith("—")                       # custom role: no icon set up
        plain = _roles(render.event_message(s, cfg, ev))                 # emojis not looked up / not uploaded
        assert plain["Tank"]["value"].startswith("Fixture Ironhide") and "<:" not in str(plain)
        line = make_cfg(event_posts={"role_icon_position": "line"})
        ln = _roles(render.event_message(s, line, ev, EMOJIS))
        assert ln["Tank"]["name"] == "Tank (1/1)"
        assert ln["Tank"]["value"].startswith("<:qb_tank:e101> Fixture Ironhide")
        off = make_cfg(event_posts={"role_icons": {}})
        assert "<:" not in str(render.event_message(s, off, ev, EMOJIS))


@needs_db
def test_worker_looks_up_the_bots_emojis_and_survives_failures(seeded, worker_db):
    cfg = make_cfg(discord={"event_channel_id": CH, "application_id": fake_id(2)})
    fake = FakeDiscord()
    fake.app_emojis = {"qb_tank": "e201"}
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        event(s, cfg, ORG, 30, 2, title="Icon run", roles=ROLES)
    for _ in range(10):
        if not w.tick():
            break
    post = next(iter(fake.messages.values()))
    assert any(f["name"] == "<:qb_tank:e201> Tank (0/1)" for f in post["embeds"][0]["fields"])
    assert ("GET", f"/applications/{cfg['discord']['application_id']}/emojis") in fake.calls
    # A failing lookup only means plain text, and it is retried later rather than on every post.
    w2 = Worker(ConfigHolder(cfg), worker_db, FakeDiscord().client())
    w2.rest.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert w2._role_emojis(w2.rest) == {} and w2._emoji_cache[0] > 0


def _patch_rest(monkeypatch, fake):
    real = discord_rest.DiscordRest.__init__

    def init(self, http=None, token=None, base=discord_rest.API):
        import httpx
        real(self, http=httpx.Client(transport=httpx.MockTransport(fake.handler)), token="fixture-token", base=base)
    monkeypatch.setattr(discord_rest.DiscordRest, "__init__", init)


def test_upload_command_is_gated_and_never_replaces(cfg_file, capsys, monkeypatch):  # noqa: F811
    fake = FakeDiscord()
    fake.app_emojis = {"qb_healer": "e9"}                          # FIXTURE: one already there
    _patch_rest(monkeypatch, fake)
    assert cli.main(["--config", cfg_file, "role-emojis"]) == 0
    out = capsys.readouterr()
    assert "healer: qb_healer is already uploaded" in out.out and "tank: would upload tank.png as qb_tank" in out.out
    assert "needs approval" in out.err and not fake.emoji_uploads
    assert cli.main(["--config", cfg_file, "role-emojis", "--yes"]) == 3     # --yes alone is not enough
    assert not fake.emoji_uploads
    monkeypatch.setenv("QUESTERSBOARD_APPROVED_CHANGE", "role-emojis")
    assert cli.main(["--config", cfg_file, "role-emojis", "--yes"]) == 0
    assert sorted(n for _, n in fake.emoji_uploads) == ["qb_damage", "qb_tank"]
    assert fake.emoji_uploads[0][0] == fake_id(2)                   # the configured application
    assert fake.app_emojis["qb_healer"] == "e9"                     # the existing one was left alone
    capsys.readouterr()
    assert cli.main(["--config", cfg_file, "role-emojis", "--yes"]) == 0
    assert "Nothing to upload." in capsys.readouterr().out and len(fake.emoji_uploads) == 2


def test_built_in_icons_fit_discord_limits():
    from pathlib import Path

    from PIL import Image
    art = Path(cli.__file__).parent / "web" / "static" / "roles"
    for key in ("tank", "healer", "damage"):
        png, svg = art / f"{key}.png", art / f"{key}.svg"
        assert png.stat().st_size < 256 * 1024 and svg.is_file()
        with Image.open(png) as im:
            assert im.size == (128, 128) and im.mode == "RGBA"


@needs_db
def test_event_page_shows_the_same_icons(seeded, wcfg, app):  # noqa: F811
    from test_web import login
    with seeded.tx() as s:
        ev = event(s, wcfg, ORG, 30, 2, title="Page icons", roles=ROLES)
        pid = ev.public_id
    html = login(app, 80, "member").get(f"/wow/quester/events/{pid}").text
    assert '<img class="wq-role-ic" src="/wow/quester/static/roles/tank.svg?v=' in html
    assert "roles/healer.svg" in html and "roles/guide.svg" not in html     # custom role: no icon
    assert 'style="' not in html.split("Roster")[1].split("</section>")[0]  # CSP: no inline styles


def test_role_icon_svgs_are_served(app):  # noqa: F811
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/wow/quester/static/roles/tank.svg")
    assert r.status_code == 200 and "<svg" in r.text and "data:" not in r.text
