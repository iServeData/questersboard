"""Event post redesign: faction colour, difficulty and level, objective, cover art, "Hosted by", the
Chronicle prefill and banner, per-event chat threads (FIXTURE Discord) and refresh-posts.
All names are made up; Discord and Blizzard are FIXTURES (no live calls)."""
from __future__ import annotations

import io
import tomllib
from datetime import date, timedelta

import httpx
import pytest
from conftest import ROOT, actor, at, fake_id, make_cfg, needs_db
from fake_discord import FakeDiscord
from fastapi.testclient import TestClient
from helpers import char, event
from PIL import Image
from sqlalchemy import select, text
from test_web import _form_defaults, app, csrf, login  # noqa: F401  (fixtures)

from questersboard.bot import render
from questersboard.config import ConfigError, ConfigHolder
from questersboard.domain import chronicle, covers, events, signups
from questersboard.domain.errors import Invalid
from questersboard.models import ChronicleEntry, ChronicleRevision, Event, Outbox, PublicChronicle
from questersboard.worker import Worker

pytestmark = needs_db
ORG = actor(1, "event_creator", name="HostOne")
CH = str(10 ** 17 + 91)


def _with_forever_difficulties(tmp_path, **over):
    """These tests exercise the Difficulty field on Forever events, so give Forever a list here (the
    example config has none: Forever has a single difficulty)."""
    eds = [dict(e) for e in tomllib.loads((ROOT / "deploy" / "config.example.toml").read_text())["editions"]]
    for e in eds:
        if e["key"] == "forever":
            e["difficulties"] = ["Normal", "Heroic"]
    return make_cfg(tmp_path, editions=eds, **over)


@pytest.fixture
def cfg(tmp_path):
    return _with_forever_difficulties(tmp_path)


@pytest.fixture
def wcfg(tmp_path):
    return _with_forever_difficulties(tmp_path, web={"cookie_secure": False})


def _img(w=320, h=180, fmt="PNG"):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (150, 40, 30)).save(b, fmt)
    return b.getvalue()


def _drain(w, n=20):
    for _ in range(n):
        if not w.tick():
            break


# ------------------------------------------------------------------ fields and validation
def test_new_fields_saved_and_validated(seeded, cfg):
    with seeded.tx() as s:
        host = char(s, cfg, ORG, "Grimtusk")
        ev = event(s, cfg, ORG, 30, 2, objective="Clear the mine and kill the captain.", difficulty="Normal",
                   level_min=15, level_max=21, host_character_id=host.id)
        assert (ev.objective, ev.difficulty, ev.level_min, ev.level_max) == \
            ("Clear the mine and kill the captain.", "Normal", 15, 21)
        assert ev.host_character_id == host.id
        with pytest.raises(Invalid, match="(?i)difficult"):
            events.update(s, cfg, ORG, ev.id, {"difficulty": "Mythic"})      # not offered by this edition
        with pytest.raises(Invalid, match="(?i)level"):
            events.update(s, cfg, ORG, ev.id, {"level_min": 30, "level_max": 20})
        with pytest.raises(Invalid, match="(?i)level"):
            events.update(s, cfg, ORG, ev.id, {"level_max": 9999})
        other = actor(55)
        theirs = char(s, cfg, other, "Zulmara")
        with pytest.raises(Invalid, match="host"):
            events.update(s, cfg, ORG, ev.id, {"host_character_id": theirs.id})
        with pytest.raises(Invalid):
            events.update(s, cfg, ORG, ev.id, {"cover_image": "../etc/passwd"})
        events.update(s, cfg, ORG, ev.id, {"host_character_id": "", "objective": ""})
        assert ev.host_character_id is None and ev.objective == ""


def test_difficulties_and_colours_are_config(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.edition("retail").difficulties[:3] == ("Normal", "Heroic", "Mythic")
    assert cfg["event_posts"]["faction_colors"]["horde"] == "#C41E3A"
    assert cfg["event_threads"]["enabled"] is False
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, event_posts={"faction_colors": {"horde": "red"}})


# ------------------------------------------------------------------ the Discord post
def test_post_layout_matches_the_mock(seeded, cfg):
    folder = covers.folder(cfg)
    name = covers.save(cfg, _img())
    (folder / name).replace(folder / "default_cover.webp")
    with seeded.tx() as s:
        host = char(s, cfg, ORG, "Grimtusk")
        ev = event(s, cfg, ORG, 30, 2, objective="Kill the captain.", difficulty="Normal", level_min=15,
                   level_max=21, host_character_id=host.id, location="The Deadmines")
        p = actor(60)
        signups.join(s, cfg, p, ev.id, char(s, cfg, p, "Vashka").id, tentative=True, recognition=True)
        msg = render.event_message(s, cfg, ev)
        e = msg["embeds"][0]
        assert e["color"] == 0xC41E3A                                    # horde red while scheduled
        assert e["description"].startswith("**Normal · Level 15–21**")
        assert "**Objective**\nKill the captain." in e["description"]
        names = [f["name"] for f in e["fields"]]
        assert names[:2] == ["Time", "Duration"] and "Tentative (1)" in names
        tent = next(f for f in e["fields"] if f["name"].startswith("Tentative"))
        assert "Vashka" in tent["value"]
        assert e["footer"]["text"].startswith("Hosted by Grimtusk · ")
        assert all(f["value"].endswith("\n\u200b") for f in e["fields"][:-1])      # blank line between headings
        assert not e["fields"][-1]["value"].endswith("\u200b")
        tight = make_cfg(event_posts={"field_spacing": False, "default_cover": "default_cover.webp"})
        assert not any(f["value"].endswith("\u200b")
                       for f in render.event_message(s, tight, ev)["embeds"][0]["fields"])
        assert e["image"]["url"] == "https://community.example.org/wow/quester/covers/default_cover.webp"
        assert e["author"]["name"].endswith("· Horde")
        ev.faction = "alliance"
        assert render.color(cfg, ev) == 0x1F6FD8
        ev.faction = None
        assert render.color(cfg, ev) == 0xC9A227
        ev.state = "cancelled"
        assert render.color(cfg, ev) == render.COLOR["cancelled"]       # finished posts keep the state colour


def test_no_cover_file_means_no_image(seeded, cfg):
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2)
        assert "image" not in render.event_message(s, cfg, ev)["embeds"][0]
        ev.cover_image = "0123abcd-0000-4000-8000-000000000000.webp"        # named but missing on disk
        assert covers.effective(cfg, ev) is None


def test_cover_rejects_non_images(cfg):
    with pytest.raises(Invalid):
        covers.save(cfg, b"not an image at all")
    with pytest.raises(Invalid):
        covers.save(cfg, _img(fmt="GIF"))


# ------------------------------------------------------------------ web: form, cover upload, public cover route
def test_web_form_saves_cover_and_details(app, seeded, wcfg):
    host = login(app, 70, "event_creator")
    new = host.get("/wow/quester/events/new").text
    assert 'enctype="multipart/form-data"' in new and 'name="objective"' in new and 'name="difficulty"' in new
    data = {**_form_defaults(new, "/wow/quester/events/new"), "title": "Mine run", "location": "Mine",
            "edition": "forever", "faction": "horde", "date": (date.today() + timedelta(days=30)).isoformat(), "start_time": "19:00",
            "timezone": "America/Los_Angeles", "objective": "Reach the ship.", "difficulty": "Normal",
            "level_min": "17", "level_max": "22"}
    r = host.post("/wow/quester/events/new", data=data, files={"cover": ("art.png", _img(), "image/png")})
    assert r.status_code == 303, r.text[:400]
    page = host.get(r.headers["location"]).text
    assert "Reach the ship." in page and "Normal · Level 17–22" in page and "wq-cover-horde" in page
    with seeded.tx() as s:
        ev = s.scalars(select(Event).where(Event.title == "Mine run")).one()
        name = ev.cover_image
    assert name and name.endswith(".webp")
    pub = TestClient(app, base_url="https://community.example.org")
    got = pub.get(f"/wow/quester/covers/{name}")
    assert got.status_code == 200 and got.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(got.content)) as im:
        assert im.format == "WEBP"
    assert pub.get("/wow/quester/covers/..%2Fconfig.toml").status_code == 404
    assert pub.get("/wow/quester/covers/nope.webp").status_code == 404


# ------------------------------------------------------------------ Chronicle
def test_chronicle_plan_prefilled_and_banner(seeded, cfg, monkeypatch):
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2, objective="Kill the captain.", difficulty="Normal", level_min=15)
        monkeypatch.setattr(events, "now", lambda: at(0))
        events.start(s, cfg, ORG, ev.id)
        events.end(s, cfg, ORG, ev.id)
        e = s.query(ChronicleEntry).filter_by(event_id=ev.id).one()
        rev = s.scalars(select(ChronicleRevision).where(ChronicleRevision.entry_id == e.id)).first()
        assert rev.attempted == "Kill the captain."
        up = chronicle.add_upload(s, cfg, ORG, e.id, _img(), "the crew")
        chronicle.set_banner(s, cfg, ORG, e.id, up.id)
        assert e.banner_upload_id == up.id
        with pytest.raises(Exception, match="not found"):
            chronicle.set_banner(s, cfg, ORG, e.id, up.id + 999)
        chronicle.clear_upload(s, cfg, actor(42, "chronicle_reviewer"), up.id)
        chronicle.save(s, cfg, ORG, e.id, {"title": "Down the mine", "happened": "We made it.",
                                          "outcome": "completed"})
        chronicle.submit(s, cfg, ORG, e.id)
        chronicle.review(s, cfg, actor(42, "chronicle_reviewer"), e.id, "approve")
        pc = s.scalars(select(PublicChronicle).where(PublicChronicle.public_id == e.public_id)).one()
        assert pc.banner["url"].endswith(f"/media/{up.public_id}.webp")
        assert pc.details == "Normal · Level 15+"


# ------------------------------------------------------------------ chat threads (FIXTURE Discord)
def test_thread_follows_signups_and_closes_after_the_event(seeded, worker_db, monkeypatch):
    cfg = make_cfg(discord={"event_channel_id": CH}, event_threads={"enabled": True})
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    a, b = actor(80), actor(81)
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2, title="Mine run")
        signups.join(s, cfg, a, ev.id, char(s, cfg, a, "Brightmere").id)
        eid = ev.id
    _drain(w)
    assert len(fake.threads) == 1
    tid, th = next(iter(fake.threads.items()))
    assert th["name"] == "Mine run · chat" and th["from_message"] in fake.messages and th["type"] == 11
    assert th["members"] == {fake_id(1), fake_id(80)}                # host and the player
    assert "Mine run" in th["posts"][0]
    with seeded.tx() as s:
        signups.join(s, cfg, b, eid, char(s, cfg, b, "Corwin").id, tentative=True)
        signups.leave(s, cfg, a, eid)
    _drain(w)
    assert len(fake.threads) == 1 and fake.threads[tid]["members"] == {fake_id(1), fake_id(81)}
    with seeded.tx() as s:
        monkeypatch.setattr(events, "now", lambda: at(0))
        events.start(s, cfg, ORG, eid)
        events.end(s, cfg, ORG, eid)
    _drain(w)
    assert fake.threads[tid]["closed"] is False                      # not until archive_after_hours later
    with seeded.tx() as s:
        job = s.scalars(select(Outbox).where(Outbox.kind == "discord.close_thread")).one()
        assert job.run_after >= at(0) + timedelta(hours=23)
        s.execute(text("update outbox set run_after = now() where kind = 'discord.close_thread'"))
    _drain(w)
    assert fake.threads[tid]["closed"] is True


def test_private_thread_and_threads_off_by_default(seeded, worker_db):
    off = make_cfg(discord={"event_channel_id": CH})
    fake = FakeDiscord()
    with seeded.tx() as s:
        event(s, off, ORG, 0, 2)
    _drain(Worker(ConfigHolder(off), worker_db, fake.client()))
    assert fake.messages and not fake.threads
    on = make_cfg(discord={"event_channel_id": CH}, event_threads={"enabled": True, "private": True})
    fake2 = FakeDiscord()
    with seeded.tx() as s:
        event(s, on, ORG, 10, 2)
    _drain(Worker(ConfigHolder(on), worker_db, fake2.client()))
    assert any(t["type"] == 12 and t["from_message"] is None for t in fake2.threads.values())


def test_reminder_also_posted_in_thread_once(seeded, worker_db, monkeypatch):
    cfg = make_cfg(discord={"event_channel_id": CH}, event_threads={"enabled": True})
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 0, 2, title="Mine run", participant_limit=5)
        a = actor(82)
        signups.join(s, cfg, a, ev.id, char(s, cfg, a, "Elspeth").id)
    _drain(w)
    import questersboard.worker as wm
    monkeypatch.setattr(wm, "now", lambda: at(-1))
    w.scan_reminders()
    w.scan_reminders()
    _drain(w)
    th = next(iter(fake.threads.values()))
    rem = [p for p in th["posts"][1:]]
    assert len(rem) == 1 and "Mine run" in rem[0]


# ------------------------------------------------------------------ Blizzard instance details (FIXTURE)
def test_blizzard_instance_details_fixture(monkeypatch):
    from questersboard.integrations import blizzard
    monkeypatch.setenv("QUESTERSBOARD_SECRET_BLIZZARD_CLIENT_ID", "FIXTURE-id")
    monkeypatch.setenv("QUESTERSBOARD_SECRET_BLIZZARD_CLIENT_SECRET", "FIXTURE-secret")

    def h(req):
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "FIXTURE", "expires_in": 3600})
        assert req.url.path.endswith("/data/wow/journal-instance/63")
        return httpx.Response(200, json={            # FIXTURE shaped like the documented response
            "category": {"type": "DUNGEON"}, "minimum_level": 10,
            "modes": [{"mode": {"type": "NORMAL", "name": "Normal"}},
                      {"mode": {"type": "HEROIC", "name": {"en_US": "Heroic"}}},
                      {"mode": {"type": "NORMAL", "name": "Normal"}}]})
    b = blizzard.Blizzard(make_cfg(blizzard={"enabled": True}),
                          http=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda _s: None)
    got = b._instance_details(None, "us", "static-us", 1, {"id": 63})
    assert got == {"category": "dungeon", "modes": ["Normal", "Heroic"], "minimum_level": 10}
    b2 = blizzard.Blizzard(make_cfg(blizzard={"enabled": True, "instance_details": False}),
                           http=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda _s: None)
    assert b2._instance_details(None, "us", "static-us", 1, {"id": 63}) == {}


# ------------------------------------------------------------------ editions with a single difficulty
def test_example_difficulty_lists_per_edition(tmp_path):
    cfg = make_cfg(tmp_path)          # the shipped example config
    assert cfg.edition("forever").difficulties == ()
    assert cfg.edition("classic_era").difficulties == ()
    assert cfg.edition("classic_anniversary").difficulties == ("Normal", "Heroic")
    assert "Challenge Mode" in cfg.edition("mists_classic").difficulties
    assert "Mythic+" in cfg.edition("retail").difficulties
    for bad in (["Normal", "normal"], [""], ["x" * 33], "Normal"):
        eds = [dict(e) for e in tomllib.loads((ROOT / "deploy" / "config.example.toml").read_text())["editions"]]
        eds[0]["difficulties"] = bad
        with pytest.raises(ConfigError, match="difficulties"):
            make_cfg(tmp_path, editions=eds)


def test_single_difficulty_edition_clears_and_rejects(seeded, tmp_path):
    cfg = make_cfg(tmp_path)          # Forever: difficulties = []
    with seeded.tx() as s:
        ev = event(s, cfg, ORG, 30, 2, objective="Clear the mine.")
        ev.difficulty = "Normal"      # saved before this release
        events.update(s, cfg, ORG, ev.id, {"difficulty": ""})
        assert ev.difficulty == ""
        with pytest.raises(Invalid, match="single difficulty"):
            events.update(s, cfg, ORG, ev.id, {"difficulty": "Normal"})


def test_web_form_hides_difficulty_for_single_difficulty_edition(seeded, tmp_path):
    from questersboard.web.app import create_app
    from test_web import FakeOAuth, FakeRest
    c = make_cfg(tmp_path, web={"cookie_secure": False})
    rest = FakeRest()
    a = create_app(ConfigHolder(c), seeded, FakeOAuth(), rest)
    a.state.fake_rest = rest
    page = login(a, 70, "event_creator").get("/wow/quester/events/new").text
    assert '<span data-for-edition="forever" hidden><input type="hidden" name="difficulty" value=""></span>' in page
    assert '<label data-for-edition="retail">Difficulty' in page
