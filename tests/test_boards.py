"""Adventure Board and profession help board ([adventures], [professions]). Discord is a FIXTURE
(tests/fake_discord.py) and Blizzard profile data is a FIXTURE row; nothing live is exercised."""
from __future__ import annotations

import re

import pytest
from conftest import actor, at, fake_id, make_cfg, needs_db
from fake_discord import FakeDiscord
from helpers import char
from sqlalchemy import select, text

from questersboard.config import ConfigError, ConfigHolder
from questersboard.domain import boards, erasure, events, profiles, roster
from questersboard.domain.errors import Invalid, InvalidTransition, PermissionDenied
from questersboard.models import BoardPost, BoardResponse, CharacterProfile, Outbox
from questersboard.worker import Worker
from test_cli import cfg_file  # noqa: F401  (fixture: the example config against the test cluster)

pytestmark = needs_db
POSTER = actor(40, name="FixturePoster")
POSTER_EC = actor(40, "event_creator", name="FixturePoster")
HELPER = actor(41, name="FixtureHelper")
OTHER = actor(42, name="FixtureOther")
MOD = actor(43, "event_moderator", name="FixtureModerator")
TZ = "America/Los_Angeles"
RECIPE = "Fixture Armor Kit"


def _adv(s, cfg, who=POSTER, name="Fixturebrann", **kw):
    c = char(s, cfg, who, name)
    data = {"kind": "need_help", "character_id": c.id, "title": "Fixture Caverns, first clear",
            "activity_type": "dungeon", "windows": [(at(24), at(26))], "timezone": TZ, "tags": ["Relaxed pace"], **kw}
    return boards.create(s, cfg, who, "adventure", data), c


def _crafter(s, cfg, who=HELPER, name="Fixturesmith", share="members", recipe=RECIPE):
    """A verified character whose FIXTURE Blizzard profile knows one recipe."""
    c = char(s, cfg, who, name)
    s.execute(text("update characters set verification='ownership_verified' where id=:i"), {"i": c.id})
    s.add(CharacterProfile(character_id=c.id, member_id=c.member_id, edition=c.edition, region=c.region,
                           groups={"professions": [{"name": "Leatherworking", "skill": 165,
                                                    "recipes": [{"name": recipe, "id": 1}]}]},
                           shares={"professions": share}))
    s.flush()
    s.refresh(c)
    return c


DETAILS = {"id": 1, "item_id": 9001, "quality": "uncommon", "rv": 2,
           "icon": "https://render.worldofwarcraft.com/forever-us/icons/56/fixture_kit.jpg",
           "tip": {"binding": "Binds when picked up", "stats": [{"text": "+8 Armor", "green": False}],
                   "sell": {"gold": "0", "silver": "1", "copper": "20"}},
           "reagents": "Fixture Leather (4), Fixture Thread", "qty": 2}


def _detailed(s, cfg, who=HELPER, name="Fixturesmith"):
    """A crafter whose FIXTURE recipe has Blizzard details (tooltip, reagents, icon)."""
    c = _crafter(s, cfg, who, name)
    prof = s.get(CharacterProfile, c.id)
    prof.groups = {"professions": [{"name": "Leatherworking", "skill": 165,
                                    "recipes": [{"name": RECIPE, **DETAILS}]}]}
    s.flush()
    return c


def _drain(w):
    for _ in range(20):
        if not w.tick():
            break


def _jobs(s, kind):
    return list(s.scalars(select(Outbox).where(Outbox.kind == kind)))


# ------------------------------------------------------------------ config

def test_config_defaults_and_errors(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg["adventures"]["enabled"] and cfg["adventures"]["group_size"] == 5
    assert cfg["adventures"]["channel_id"] == "" and cfg["professions"]["post_days"] == 7
    assert "Leatherworking" in cfg["professions"]["names"]
    for over in ({"adventures": {"group_size": 1}}, {"adventures": {"channel_id": "not-an-id"}},
                 {"adventures": {"bogus": 1}}, {"professions": {"names": []}},
                 {"professions": {"enabled": "yes"}}, {"professions": {"max_open_per_member": 0}}):
        with pytest.raises(ConfigError):
            make_cfg(tmp_path, **over)
    ok = make_cfg(tmp_path, adventures={"channel_id": fake_id(77)})
    assert ok["adventures"]["channel_id"] == fake_id(77)


# ------------------------------------------------------------------ adventure posts

def test_create_validates_and_limits(seeded, tmp_path):
    cfg = make_cfg(tmp_path, adventures={"max_open_per_member": 2})
    with seeded.tx() as s:
        p, c = _adv(s, cfg)
        assert p.state == "open" and p.tags == ["Relaxed pace"] and p.group_size == 5
        assert p.expires_at == at(26) and p.discord_channel_id is None
        assert not _jobs(s, "discord.render_board_post")          # no channel: website only
        other = char(s, cfg, OTHER, "Fixtureelse")
        bad = [{"character_id": other.id}, {"kind": "bogus"}, {"title": "x"}, {"activity_type": "bogus"},
               {"windows": [(at(26), at(24))]}, {"windows": [(at(24), at(40))]}, {"windows": [(at(-60), at(-59))]},
               {"windows": [(at(24 * 30), at(24 * 30 + 1))]}, {"group_size": 1}, {"body": "x" * 501},
               {"windows": [(at(h), at(h + 1)) for h in (2, 4, 6, 8)]}]
        for over in bad:
            data = {"kind": "need_help", "character_id": c.id, "title": "A fine goal", "activity_type": "dungeon",
                    "windows": [], **over}
            with pytest.raises(Invalid):
                boards.create(s, cfg, POSTER, "adventure", data)
        boards.create(s, cfg, POSTER, "adventure", {"kind": "company", "character_id": c.id, "title": "Second"})
        with pytest.raises(Invalid, match="open posts"):
            boards.create(s, cfg, POSTER, "adventure", {"kind": "company", "character_id": c.id, "title": "Third"})


def test_disabled_board_refuses(seeded, tmp_path):
    cfg = make_cfg(tmp_path, adventures={"enabled": False})
    with seeded.tx() as s:
        c = char(s, cfg, POSTER, "Fixtureoff")
        with pytest.raises(Exception):
            boards.create(s, cfg, POSTER, "adventure", {"kind": "company", "character_id": c.id, "title": "Hello"})


def test_respond_toggle_counts_and_notify(seeded, worker_db, cfg):
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        p, _ = _adv(s, cfg)
        pid = str(p.public_id)
        with pytest.raises(Invalid):
            boards.respond(s, cfg, POSTER, pid, "help")             # own post
        with pytest.raises(Invalid):
            boards.respond(s, cfg, HELPER, pid, "ask")              # not an option here
        boards.respond(s, cfg, HELPER, pid, "help")
        boards.respond(s, cfg, HELPER, pid, "help")                 # twice is still once
        boards.respond(s, cfg, OTHER, pid, "need")
        v = boards.view(s, cfg, roster.ensure_member(s, OTHER), p)
        assert v.counts == {"help": 1, "need": 1} and v.people == 3 and not v.ready
        assert "need" in v.mine and v.named == {"help": ["FixtureHelper"]}
        assert "need" not in v.named                                # counts only, except to the poster
        mine = boards.view(s, cfg, roster.ensure_member(s, POSTER), p)
        assert mine.own and not mine.actions and "need" in mine.named and mine.can_plan
        boards.respond(s, cfg, OTHER, pid, "need", want=False)
        assert s.get(BoardResponse, (p.id, roster.ensure_member(s, OTHER).member_id, "need")) is None
    _drain(w)
    hits = [c for _, c in fake.dms if "can help with your goal" in c]
    assert len(hits) == 1 and "/board/" in hits[0]                 # "need" presses don't send a message
    for _, c in fake.dms:
        assert "webhook" not in c.lower() and "outbox" not in c.lower()


def test_close_extend_and_moderator_remove(seeded, cfg):
    with seeded.tx() as s:
        p, _ = _adv(s, cfg)
        pid = str(p.public_id)
        with pytest.raises(PermissionDenied):
            boards.close(s, cfg, HELPER, pid, "done")
        with pytest.raises(PermissionDenied):
            boards.close(s, cfg, MOD, pid, "done")                  # moderators can only remove
        with pytest.raises(PermissionDenied):
            boards.extend(s, cfg, HELPER, pid)
        boards.extend(s, cfg, POSTER, pid)
        from questersboard.domain.timeutil import now
        assert p.expires_at > now() + __import__("datetime").timedelta(days=13)
        boards.close(s, cfg, POSTER, pid, "done")
        assert p.state == "done" and boards.view(s, cfg, POSTER, p).closed_text == "Marked done"
        with pytest.raises(InvalidTransition):
            boards.respond(s, cfg, HELPER, pid, "help")
        with pytest.raises(InvalidTransition):
            boards.extend(s, cfg, POSTER, pid)
        q, _ = _adv(s, cfg, name="Fixturesecond")
        boards.close(s, cfg, MOD, str(q.public_id), "removed")
        assert q.state == "removed"


def test_plan_event_then_publish_sends_link(seeded, worker_db, cfg):
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        p, c = _adv(s, cfg)
        pid = str(p.public_id)
        boards.respond(s, cfg, HELPER, pid, "help")
        boards.respond(s, cfg, OTHER, pid, "need")
        with pytest.raises(PermissionDenied):
            boards.plan_event(s, cfg, HELPER, pid, at(24), at(26), TZ)
        ev = boards.plan_event(s, cfg, POSTER, pid, at(24), at(26), TZ)
        assert ev.state == "draft" and ev.participant_limit == 5 and ev.host_character_id == c.id
        assert "Adventure Board" in ev.description
        assert boards.plan_event(s, cfg, POSTER, pid, at(30), at(32), TZ).id == ev.id   # same draft
        events.publish(s, cfg, POSTER_EC, ev.id)
        assert p.state == "closed" and p.closed_reason == "evented"
        eid = ev.id
    fake.dms.clear()
    _drain(w)
    hits = [(u, c) for u, c in fake.dms if "is now an event" in c]
    assert sorted(u for u, _ in hits) == sorted([fake_id(41), fake_id(42)])
    assert all("/events/" in c and "aren't signed up" in c for _, c in hits)
    assert not any(re.search(r"\b(AM|PM)\b", c) for _, c in hits)
    with seeded.tx() as s:                                          # still listed while the event is to come
        assert [v.p.id for v in boards.listing(s, cfg, POSTER, "adventure")] == [p.id]
        assert s.scalar(select(BoardPost.event_id).where(BoardPost.id == p.id)) == eid


def test_listing_filters(seeded, cfg):
    with seeded.tx() as s:
        p, _ = _adv(s, cfg)
        q, _ = _adv(s, cfg, who=OTHER, name="Fixturecomp", kind="company", activity_type="quest", tags=[])
        ids = lambda f: [v.p.id for v in boards.listing(s, cfg, HELPER, "adventure", f)]  # noqa: E731
        assert set(ids({})) == {p.id, q.id}
        assert ids({"kind": "company"}) == [q.id] and ids({"activity": "dungeon"}) == [p.id]
        assert ids({"tag": "relaxed PACE"}) == [p.id] and ids({"edition": "nope"}) == []
        assert ids({"fits": True}) == []                            # the helper has no characters
        char(s, cfg, HELPER, "Fixturefits")
        assert set(ids({"fits": True})) == {p.id, q.id}


def test_expiry_scan(seeded, worker_db, tmp_path):
    cfg = make_cfg(tmp_path, adventures={"channel_id": fake_id(70)})
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        p, _ = _adv(s, cfg)
        pid = p.id
    _drain(w)
    assert len(fake.messages) == 1
    with seeded.tx() as s:
        s.execute(text("update board_posts set expires_at = now() - interval '1 minute' where id=:i"), {"i": pid})
    assert w.scan_boards() == 1 and w.scan_boards() == 0
    _drain(w)
    with seeded.tx() as s:
        assert s.get(BoardPost, pid).closed_reason == "expired"
    card = next(iter(fake.messages.values()))
    assert "Ran out of time" in str(card) and "custom_id" not in str(card)


# ------------------------------------------------------------------ Discord cards

def test_cards_post_edit_delete_in_channel(seeded, worker_db, tmp_path):
    chan = fake_id(71)
    cfg = make_cfg(tmp_path, adventures={"channel_id": chan})
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        p, _ = _adv(s, cfg)
        pid, pub = p.id, str(p.public_id)
    _drain(w)
    assert len(fake.messages) == 1
    mid, card = next(iter(fake.messages.items()))
    flat = str(card)
    assert "Fixture Caverns" in flat and f"wq:bp:help:{pid}" in flat and f"/board/{pub}" in flat
    assert "UTC" in flat and not re.search(r"\b(AM|PM)\b", flat)
    assert "webhook" not in flat.lower() and "outbox" not in flat.lower()
    with seeded.tx() as s:
        assert s.get(BoardPost, pid).discord_message_id == mid
        boards.respond(s, cfg, HELPER, pub, "help")
    _drain(w)
    assert "1 can help" in str(fake.messages[mid]) and "FixtureHelper" not in str(fake.messages[mid])
    with seeded.tx() as s:
        boards.close(s, cfg, POSTER, pub, "removed")
    _drain(w)
    assert ("DELETE", f"/channels/{chan}/messages/{mid}") in fake.calls


def test_no_channel_skips_card(seeded, worker_db, cfg):
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        p, _ = _adv(s, cfg)
        boards.request_render(s, p)                                 # even if asked, nothing is posted
    _drain(w)
    assert fake.messages == {}


def test_board_message_buttons(seeded, cfg):
    from questersboard.bot.render import board_message
    with seeded.tx() as s:
        p, _ = _adv(s, cfg, kind="company")
        msg = board_message(s, cfg, p)
        buttons = [c for row in msg["components"] for c in row["components"]]
        assert [b.get("custom_id") for b in buttons if b.get("custom_id")] == [f"wq:bp:need:{p.id}", f"wq:bt:{p.id}"]
        assert any(b.get("url", "").endswith(f"/board/{p.public_id}") for b in buttons)


# ------------------------------------------------------------------ profession posts

def test_can_craft_needs_blizzard_recipe_and_closes_when_gone(seeded, cfg):
    with seeded.tx() as s:
        c = _crafter(s, cfg)
        with pytest.raises(Invalid, match="Blizzard profile"):
            boards.create(s, cfg, HELPER, "profession", {"kind": "can_craft", "character_id": c.id,
                                                          "recipe_name": "Something else"})
        p = boards.create(s, cfg, HELPER, "profession", {"kind": "can_craft", "character_id": c.id,
                                                          "recipe_name": RECIPE.lower()})
        assert p.title == RECIPE and p.profession == "Leatherworking"
        assert boards.card_extra(s, p) == {"skill": "Leatherworking 165"}
        assert boards.craftable_choices(s, c.member_id)[0]["name"] == RECIPE
        # a refresh that no longer shows the recipe closes the offer
        prof = s.get(CharacterProfile, c.id)
        prof.groups = {"professions": [{"name": "Leatherworking", "skill": 170, "recipes": []}]}
        s.flush()
        assert boards.recheck_offers(s, c.id) == 1
        assert p.state == "closed" and p.closed_reason == "recipe_gone"
        # removing the details closes a new offer too
        prof.groups = {"professions": [{"name": "Leatherworking", "skill": 170, "recipes": [{"name": RECIPE}]}]}
        s.flush()
        q = boards.create(s, cfg, HELPER, "profession", {"kind": "can_craft", "character_id": c.id,
                                                          "recipe_name": RECIPE})
        profiles.remove_profile(s, cfg, roster.ensure_member(s, HELPER), c.id)
        assert q.state == "closed" and q.closed_reason == "recipe_gone"


def test_craft_posts_carry_item_details(seeded, cfg):
    from questersboard.bot.render import board_message
    with seeded.tx() as s:
        c = _detailed(s, cfg)
        p = boards.create(s, cfg, HELPER, "profession", {"kind": "can_craft", "character_id": c.id,
                                                          "recipe_name": RECIPE})
        extra = boards.card_extra(s, p)
        item = extra["item"]
        assert item["reagents"] == DETAILS["reagents"] and item["qty"] == 2 and item["icon"] == DETAILS["icon"]
        assert "+8 Armor" in item["lines"] and "Sell Price: 1s 20c" in item["lines"]
        e = board_message(s, cfg, p, extra)["embeds"][0]
        got = {f["name"]: f["value"] for f in e["fields"]}
        assert got["Reagents"] == DETAILS["reagents"]
        assert "Binds when picked up" in got["Item"] and "Makes 2 at a time" in got["Item"]
        assert e["thumbnail"]["url"] == DETAILS["icon"]
        # a Need crafted post for the same item shows the same game data
        mine = char(s, cfg, POSTER, "Fixturebuyer")
        q = boards.create(s, cfg, POSTER, "profession", {"kind": "need_crafted", "character_id": mine.id,
                                                          "title": RECIPE})
        assert boards.card_extra(s, q)["item"]["reagents"] == DETAILS["reagents"]
        # an icon from anywhere but Blizzard's image host is left off
        prof = s.get(CharacterProfile, c.id)
        prof.groups = {"professions": [{"name": "Leatherworking", "skill": 165, "recipes": [
            {"name": RECIPE, **DETAILS, "icon": "https://elsewhere.example.org/x.jpg"}]}]}
        s.flush()
        assert "thumbnail" not in board_message(s, cfg, p, boards.card_extra(s, p))["embeds"][0]
        # no details stored yet: the card is as before
        prof.groups = {"professions": [{"name": "Leatherworking", "skill": 165, "recipes": [{"name": RECIPE}]}]}
        s.flush()
        assert "item" not in boards.card_extra(s, p)
        # a refresh that still shows the recipe updates the open offer's card
        p.discord_channel_id = fake_id(880)
        s.execute(text("delete from outbox where kind = 'discord.render_board_post'"))
        assert boards.recheck_offers(s, c.id) == 0 and p.state == "open"
        assert [j.payload["post_id"] for j in _jobs(s, "discord.render_board_post")] == [p.id]


def test_removed_character_closes_offers(seeded, cfg):
    with seeded.tx() as s:
        c = _crafter(s, cfg)
        p = boards.create(s, cfg, HELPER, "profession", {"kind": "can_craft", "character_id": c.id,
                                                          "recipe_name": RECIPE})
        roster.remove_character(s, cfg, HELPER, c.id)
        assert p.state == "closed" and p.closed_reason in ("character", "recipe_gone")


def test_need_crafted_can_make_and_could_help(seeded, worker_db, cfg):
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        mine = char(s, cfg, POSTER, "Fixtureneedy")
        bad = {"kind": "need_crafted", "character_id": mine.id, "title": RECIPE, "profession": "Basket weaving"}
        with pytest.raises(Invalid):
            boards.create(s, cfg, POSTER, "profession", bad)
        with pytest.raises(Invalid):
            boards.create(s, cfg, POSTER, "profession", {"kind": "recipe", "character_id": mine.id,
                                                          "title": "Where is it"})   # needs a profession
        p = boards.create(s, cfg, POSTER, "profession", {"kind": "need_crafted", "character_id": mine.id,
                                                          "title": RECIPE})
        pid = str(p.public_id)
        with pytest.raises(Invalid, match="Blizzard profile"):
            boards.respond(s, cfg, OTHER, pid, "can_make")          # no character knows it
        c = _crafter(s, cfg)
        helper = roster.ensure_member(s, HELPER)
        assert [x[0].id for x in boards.could_help(s, cfg, helper)] == [p.id]
        assert boards.card_extra(s, p) == {"crafters": 1}
        v = boards.view(s, cfg, roster.ensure_member(s, POSTER), p)
        assert v.crafters == 1
        r = boards.respond(s, cfg, HELPER, pid, "can_make")
        assert r.character_id == c.id
        only = [v.p.id for v in boards.listing(s, cfg, HELPER, "profession", {"can_help": True})]
        assert only == [p.id]
        assert boards.listing(s, cfg, POSTER, "profession", {"profession": "Mining"}) == []
    _drain(w)
    hits = [m for _, m in fake.dms if "can make your" in m]
    assert len(hits) == 1 and "Leatherworking 165" in hits[0] and "Fixturesmith" in hits[0]


def test_private_profile_not_counted(seeded, cfg):
    with seeded.tx() as s:
        mine = char(s, cfg, POSTER, "Fixtureneedtwo")
        p = boards.create(s, cfg, POSTER, "profession", {"kind": "need_crafted", "character_id": mine.id,
                                                          "title": RECIPE})
        _crafter(s, cfg, share="private")
        assert boards.card_extra(s, p) == {"crafters": 0}
        assert boards.view(s, cfg, roster.ensure_member(s, OTHER), p).crafters == 0


def test_recipe_answer_needs_text(seeded, cfg):
    with seeded.tx() as s:
        mine = char(s, cfg, POSTER, "Fixtureasker")
        p = boards.create(s, cfg, POSTER, "profession", {"kind": "recipe", "character_id": mine.id,
                                                          "title": "Pattern: Fixture Belt", "profession": "leatherworking"})
        assert p.profession == "Leatherworking"
        with pytest.raises(Invalid):
            boards.respond(s, cfg, HELPER, str(p.public_id), "answer", note="")
        boards.respond(s, cfg, HELPER, str(p.public_id), "answer", note="Sold by a fixture vendor.")
        v = boards.view(s, cfg, roster.ensure_member(s, OTHER), p)
        assert v.answers == [("FixtureHelper", "Sold by a fixture vendor.")]


# ------------------------------------------------------------------ erasure

def test_erasure_removes_posts_and_answers(seeded, worker_db, tmp_path):
    chan = fake_id(72)
    cfg = make_cfg(tmp_path, adventures={"channel_id": chan})
    fake = FakeDiscord()
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    with seeded.tx() as s:
        p, _ = _adv(s, cfg)
        q, _ = _adv(s, cfg, who=OTHER, name="Fixturestays")
        boards.respond(s, cfg, POSTER, str(q.public_id), "help")
        pid, qid = p.id, q.id
    _drain(w)
    assert len(fake.messages) == 2
    with seeded.tx() as s:
        mid = roster.ensure_member(s, POSTER).member_id
        r = erasure.erase(s, cfg, mid)
        assert r["board_posts"] == 1
        assert s.get(BoardPost, pid) is None and s.get(BoardPost, qid) is not None
        assert s.scalar(select(BoardResponse).where(BoardResponse.member_id == mid)) is None
    _drain(w)
    assert any(m == "DELETE" and path.startswith(f"/channels/{chan}/messages/") for m, path in fake.calls)


# ------------------------------------------------------------------ web pages

def _web(seeded, tmp_path, **over):
    import test_web as tw
    cfg = make_cfg(tmp_path, web={"cookie_secure": False}, **over)
    rest = tw.FakeRest()
    a = tw.create_app(ConfigHolder(cfg), seeded, tw.FakeOAuth(), rest)
    a.state.fake_rest = rest
    return a, cfg, tw


def test_web_adventure_board_flow(seeded, tmp_path):
    a, cfg, tw = _web(seeded, tmp_path)
    c = tw.login(a, 40)
    r = c.get("/wow/quester/adventures")
    assert r.status_code == 200 and "Add a character first" in r.text
    assert ">Adventure Board<" in r.text and ">Profession help<" in r.text      # submenu
    with seeded.tx() as s:
        cid = char(s, cfg, POSTER, "Fixtureweb").id
    page = c.get("/wow/quester/adventures").text
    assert 'name="from_0"' in page and 'type="time"' not in page and 'placeholder="19:00"' in page
    tok = tw.csrf(c, "/wow/quester/adventures")
    day = at(48).astimezone(__import__("zoneinfo").ZoneInfo(TZ)).date().isoformat()
    # 21:00 to 19:00 reads as past midnight (22 hours): refused with the times, and nothing typed is lost
    bad = c.post("/wow/quester/adventures", data={
        "csrf": tok, "character_id": cid, "kind": "company", "title": "Fixture long day",
        "activity_type": "quest", "body": "Keep me", "date_0": day, "from_0": "21:00", "to_0": "19:00",
        "timezone": TZ, "group_size": "7", "tags": ["Social"]})
    assert bad.status_code == 400
    assert "is 22 hours long" in bad.text and "up to 12 hours" in bad.text and "next day" in bad.text
    for frag in ('value="Fixture long day"', ">Keep me<", f'value="{day}"', 'value="21:00"', 'value="19:00"',
                 'value="7"', 'value="Social" checked', 'value="company" selected', 'value="quest" selected'):
        assert frag in bad.text, frag
    assert 'id="goal-dialog" aria-labelledby="goal-dialog-title" data-board-dialog open' in bad.text
    assert bad.text.count('aria-invalid="true"') == 3          # the date, from and to of that row
    assert 'name="title" required minlength="3" maxlength="120" value="Fixture long day"' in bad.text
    assert 'data-board-dialog>' in c.get("/wow/quester/adventures").text          # closed until asked
    assert 'data-board-dialog open' in c.get("/wow/quester/adventures?new=1").text
    r = c.post("/wow/quester/adventures", data={
        "csrf": tok, "character_id": cid, "kind": "need_help", "title": "Fixture Keep, all bosses",
        "activity_type": "dungeon", "body": "Slow and steady", "date_0": day, "from_0": "19:00", "to_0": "21:30",
        "timezone": TZ, "group_size": "5", "tags": ["Relaxed pace", "Not a real tag"]})
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert "/board/" in loc
    detail = c.get(loc).text
    assert "Fixture Keep, all bosses" in detail and "19:00" in detail and "21:30" in detail and "UTC" in detail
    assert "Turn into an event" in detail and not re.search(r"\b\d{1,2}:\d\d ?(AM|PM)\b", detail)
    with seeded.tx() as s:
        p = s.scalar(select(BoardPost).where(BoardPost.title == "Fixture Keep, all bosses"))
        assert p.tags == ["Relaxed pace"] and len(p.windows) == 1
        pub = str(p.public_id)
    h = tw.login(a, 41)
    tok = tw.csrf(h, "/wow/quester/adventures")
    r = h.post(f"/wow/quester/board/{pub}/respond", data={"csrf": tok, "kind": "help", "want": "1",
                                                          "back": "adventures"})
    assert r.status_code == 303 and r.headers["location"].endswith("/adventures")
    listing = h.get("/wow/quester/adventures").text
    assert "1 can help" in listing and "Fixture Keep" in listing
    assert h.post(f"/wow/quester/board/{pub}/done", data={"csrf": tok}).status_code in (403, 303)
    with seeded.tx() as s:
        assert s.scalar(select(BoardPost.state).where(BoardPost.public_id == pub)) == "open"
    m = tw.login(a, 43, "event_moderator")
    page = m.get(f"/wow/quester/board/{pub}").text
    assert "Moderator tools" in page and "Remove post" in page
    r = c.post(f"/wow/quester/board/{pub}/plan", data={"csrf": tw.csrf(c, loc), "date": day,
                                                       "start_time": "19:00", "minutes": "120", "timezone": TZ})
    assert r.status_code == 303 and "/events/" in r.headers["location"]


def test_web_profession_pages(seeded, tmp_path):
    a, cfg, tw = _web(seeded, tmp_path)
    with seeded.tx() as s:
        c = _crafter(s, cfg)
    h = tw.login(a, 41)
    page = h.get("/wow/quester/professions").text
    assert RECIPE in page and f'value="{c.id}:{RECIPE}"' in page
    assert 'name="craft_character"' in page and f'<optgroup label="{c.name}" data-character="{c.id}">' in page
    # a character and an item from someone else's list don't go together (only possible without the script)
    r = h.post("/wow/quester/professions", data={"csrf": tw.csrf(h, "/wow/quester/professions"),
                                                 "kind": "can_craft", "craft_character": str(c.id + 999),
                                                 "craft": f"{c.id}:{RECIPE}", "body": "Bring mats"})
    assert r.status_code == 400 and "the character you chose can craft" in r.text
    assert 'data-craft-recipe class="is-bad"' in r.text and ">Bring mats<" in r.text
    r = h.post("/wow/quester/professions", data={"csrf": tw.csrf(h, "/wow/quester/professions"),
                                                 "kind": "can_craft", "craft_character": str(c.id),
                                                 "craft": f"{c.id}:{RECIPE}", "body": ""})
    assert r.status_code == 303, r.text
    p = tw.login(a, 40)
    with seeded.tx() as s:
        mine = char(s, cfg, POSTER, "Fixturebuyer").id
    r = p.post("/wow/quester/professions", data={"csrf": tw.csrf(p, "/wow/quester/professions"),
                                                 "kind": "need_crafted", "character_id": mine, "title": RECIPE})
    assert r.status_code == 303
    assert "You could help" in h.get("/wow/quester/professions").text
    bad = p.post("/wow/quester/professions", data={"csrf": tw.csrf(p, "/wow/quester/professions"),
                                                   "kind": "farm", "character_id": mine, "title": "x"})
    assert bad.status_code == 400 and "Pick the profession" in bad.text
    # the Recipe or farming dialog comes back open, filled in, with the profession marked
    assert 'id="prof-ask" aria-labelledby="prof-ask-title" data-board-dialog open' in bad.text
    assert 'id="prof-need" aria-labelledby="prof-need-title" data-board-dialog>' in bad.text
    assert 'value="farm" selected' in bad.text and 'value="x"' in bad.text
    assert 'name="profession" required class="is-bad" aria-invalid="true"' in bad.text
    assert bad.text.count('aria-invalid="true"') == 1
    page = p.get("/wow/quester/professions?new=offer").text
    assert 'id="prof-offer" aria-labelledby="prof-offer-title" data-board-dialog open' in page
    assert 'data-open-dialog="prof-need"' in page and 'aria-invalid' not in page


def test_web_craft_post_shows_item_details(seeded, tmp_path):
    a, cfg, tw = _web(seeded, tmp_path)
    with seeded.tx() as s:
        c = _detailed(s, cfg)
        p = boards.create(s, cfg, HELPER, "profession", {"kind": "can_craft", "character_id": c.id,
                                                          "recipe_name": RECIPE})
        pub = str(p.public_id)
    v = tw.login(a, 40)
    for url in ("/wow/quester/professions", f"/wow/quester/board/{pub}"):
        page = v.get(url).text
        assert "Reagents:</b> Fixture Leather (4), Fixture Thread" in page
        assert 'class="wq-item wq-recipe pb-item wq-q-uncommon" data-tip' in page
        assert "+8 Armor" in page and "Makes 2 at a time" in page and "Leatherworking recipe" in page
        assert f'src="/wow/quester/board/{pub}/icon"' in page and "render.worldofwarcraft.com" not in page
    seen = []
    a.state.web.bz.fetch_icon = lambda url, max_bytes=200_000: seen.append(url) or (b"\x89PNG-fixture", "image/png")
    r = v.get(f"/wow/quester/board/{pub}/icon")
    assert r.status_code == 200 and r.content == b"\x89PNG-fixture" and seen == [DETAILS["icon"]]
    anon = tw.TestClient(a, base_url="https://community.example.org", follow_redirects=False)
    assert anon.get(f"/wow/quester/board/{pub}/icon").status_code in (303, 401, 404)


def test_web_boards_off_and_signed_out(seeded, tmp_path):
    a, cfg, tw = _web(seeded, tmp_path, adventures={"enabled": False})
    c = tw.login(a, 40)
    assert c.get("/wow/quester/adventures").status_code == 404
    assert ">Adventure Board<" not in c.get("/wow/quester/").text
    anon = tw.TestClient(a, base_url="https://community.example.org", follow_redirects=False)
    r = anon.get("/wow/quester/professions")
    assert r.status_code in (200, 303) and "Fixture" not in r.text


def test_moderator_tools_label(seeded, tmp_path):
    from helpers import event
    a, cfg, tw = _web(seeded, tmp_path)
    host = actor(44, "event_creator", name="FixtureHost")
    with seeded.tx() as s:
        ev = event(s, cfg, host, start=24)
        pub = str(ev.public_id)
    page = tw.login(a, 44, "event_creator").get(f"/wow/quester/events/{pub}").text
    assert "Host tools" in page and "Moderator tools" not in page
    page = tw.login(a, 43, "event_moderator").get(f"/wow/quester/events/{pub}").text
    assert "Moderator tools" in page and "Host tools" not in page and "you're an event moderator" in page
    page = tw.login(a, 45).get(f"/wow/quester/events/{pub}").text
    assert "Moderator tools" not in page and "Host tools" not in page


def test_discord_button_press_toggles(seeded, cfg):
    from questersboard.bot.client import BCID, _board_press
    from questersboard.bot.render import bcid
    with seeded.tx() as s:
        p, _ = _adv(s, cfg)
        m = re.fullmatch(BCID, bcid("help", p.id))
        assert m and m["action"] == "help" and int(m["post_id"]) == p.id
        btn = actor(41, source="button", name="FixtureHelper")
        assert "The poster has been told" in _board_press(s, cfg, btn, p.id, "help")
        assert boards.view(s, cfg, roster.ensure_member(s, POSTER), p).counts == {"help": 1}
        assert _board_press(s, cfg, btn, p.id, "help").startswith("Taken back")
        assert boards.view(s, cfg, roster.ensure_member(s, POSTER), p).counts == {}
        boards.close(s, cfg, POSTER, str(p.public_id), "removed")
        assert _board_press(s, cfg, btn, p.id, "help") == "This post was removed."


# ------------------------------------------------------------------ craft fees, poster pictures

def _offer(s, cfg, c, **fee):
    return boards.create(s, cfg, HELPER, "profession", {"kind": "can_craft", "character_id": c.id,
                                                        "recipe_name": RECIPE, **fee})


def test_craft_fee_is_checked_and_shown(seeded, cfg):
    from questersboard.bot.render import board_message
    with seeded.tx() as s:
        c = _crafter(s, cfg)
        for fee, field, msg in (({"fee": "beer"}, "fee", "Pick what you ask for"),
                                ({"fee": "fee"}, "fee_gold", "Put in the fee"),
                                ({"fee": "fee", "fee_gold": "0", "fee_silver": "0"}, "fee_gold", "Put in the fee"),
                                ({"fee": "fee", "fee_silver": "100"}, "fee_silver", "from 0 to 99"),
                                ({"fee": "fee", "fee_copper": "-1"}, "fee_copper", "from 0 to 99"),
                                ({"fee": "mats_fee", "fee_gold": "100000"}, "fee_gold", "from 0 to 99999")):
            with pytest.raises(Invalid, match=msg) as e:
                _offer(s, cfg, c, **fee)
            assert e.value.detail.get("field") == field
        p = _offer(s, cfg, c, fee="mats_fee", fee_gold="5", fee_silver="20", fee_copper="")
        assert (p.fee_kind, p.fee_copper) == ("mats_fee", 52000)
        assert boards.fee_text(p) == "5g 20s, bring your materials"
        got = {f["name"]: f["value"] for f in board_message(s, cfg, p, boards.card_extra(s, p))["embeds"][0]["fields"]}
        assert got["Fee"] == "5g 20s, bring your materials"
        v = boards.view(s, cfg, POSTER, p)
        assert v.fee == {"note": "bring your materials", "coins": {"gold": "5", "silver": "20", "copper": "0"}}
        q = _offer(s, cfg, c, fee="fee", fee_copper="7")
        assert boards.fee_text(q) == "7c, materials included"
        m = _offer(s, cfg, c, fee="mats", fee_gold="9")          # coins left in a hidden box are ignored
        assert (m.fee_kind, m.fee_copper) == ("mats", None) and boards.fee_text(m) == "Bring your materials, no fee"
        old = _offer(s, cfg, c)                                   # a post that doesn't say: no Fee line
        assert old.fee_kind is None and boards.fee_text(old) == "" and boards.view(s, cfg, POSTER, old).fee == {}
        assert "Fee" not in {f["name"] for f in board_message(s, cfg, old, {})["embeds"][0]["fields"]}


def test_web_offer_with_fee(seeded, tmp_path):
    a, cfg, tw = _web(seeded, tmp_path)
    with seeded.tx() as s:
        c = _crafter(s, cfg)
    h = tw.login(a, 41)
    page = h.get("/wow/quester/professions").text
    assert '<select name="fee" required data-fee>' in page and 'name="fee_silver"' in page
    assert 'data-fee-coins hidden>' in page                       # "no fee" is picked to start with
    form = {"kind": "can_craft", "craft_character": str(c.id), "craft": f"{c.id}:{RECIPE}", "body": "",
            "fee": "fee", "fee_gold": "", "fee_silver": "", "fee_copper": ""}
    r = h.post("/wow/quester/professions", data={"csrf": tw.csrf(h, "/wow/quester/professions"), **form})
    assert r.status_code == 400 and "Put in the fee" in r.text
    assert 'value="fee" selected' in r.text and 'data-fee-coins>' in r.text and 'name="fee_gold"' in r.text
    form.update(fee_gold="12", fee_silver="5")
    r = h.post("/wow/quester/professions", data={"csrf": tw.csrf(h, "/wow/quester/professions"), **form})
    assert r.status_code == 303, r.text
    page = tw.login(a, 40).get("/wow/quester/professions").text
    assert ('<p class="pb-fee"><b>Fee:</b> 12<i class="wq-coin wq-coin-g" aria-label="gold"></i> '
            '5<i class="wq-coin wq-coin-s" aria-label="silver"></i>, materials included</p>') in page


def test_post_shows_character_picture_only_to_those_who_can_see_it(seeded, tmp_path):
    a, cfg, tw = _web(seeded, tmp_path)
    with seeded.tx() as s:
        p, c = _adv(s, cfg)
        s.execute(text("update characters set verification='ownership_verified' where id=:i"), {"i": c.id})
        s.add(CharacterProfile(character_id=c.id, member_id=c.member_id, edition=c.edition, region=c.region,
                               groups={"portrait": [{"key": "avatar", "url": "https://render.worldofwarcraft.com/"
                                                     "forever-us/character/fixture-avatar.jpg"}]},
                               shares={"portrait": "private"}))
        s.flush()
        s.expire_all()
        assert boards.view(s, cfg, roster.ensure_member(s, OTHER), p).portrait == ""
        assert boards.view(s, cfg, roster.ensure_member(s, POSTER), p).portrait == f"/characters/{c.id}/portrait/avatar"
        pub, cid = str(p.public_id), c.id
    other = tw.login(a, 42)
    page = other.get("/wow/quester/adventures").text
    assert "data-av-portrait" not in page and "pb-sil" in page
    with seeded.tx() as s:
        s.get(CharacterProfile, cid).shares = {"portrait": "members"}
    for url in ("/wow/quester/adventures", f"/wow/quester/board/{pub}"):
        page = other.get(url).text
        assert f'src="/wow/quester/characters/{cid}/portrait/avatar"' in page and "data-av-portrait" in page
        assert "data-av-fallback hidden" in page and "render.worldofwarcraft.com" not in page
    with seeded.tx() as s:                                     # no picture from Blizzard: the race outline
        s.get(CharacterProfile, cid).groups = {"portrait": []}
    assert "data-av-portrait" not in other.get("/wow/quester/adventures").text


def test_refresh_posts_includes_open_board_posts(seeded, cfg_file, capsys):  # noqa: F811
    from questersboard import cli
    from questersboard.config import load
    cfg = load(cfg_file)
    with seeded.tx() as s:
        p, _c = _adv(s, cfg)
        p.discord_channel_id = p.discord_message_id = fake_id(890)
        closed, _c2 = _adv(s, cfg, name="Fixtureother", title="Fixture closed")
        closed.discord_message_id = fake_id(891)
        closed.state = "closed"
        s.execute(text("delete from outbox where kind = 'discord.render_board_post'"))
        pid = p.id
    assert cli.main(["--config", cfg_file, "refresh-posts"]) == 0
    out = capsys.readouterr().out
    assert f"board post #{pid} Fixture Caverns" in out and "Fixture closed" not in out and "dry run: 1 post(s)" in out
    with seeded.tx() as s:
        assert _jobs(s, "discord.render_board_post") == []
    assert cli.main(["--config", cfg_file, "refresh-posts", "--yes"]) == 0
    with seeded.tx() as s:
        assert [j.payload["post_id"] for j in _jobs(s, "discord.render_board_post")] == [pid]


def test_poster_can_edit_details_and_fee(seeded, cfg):
    with seeded.tx() as s:
        c = _crafter(s, cfg)
        p = _offer(s, cfg, c, body="Bring leather", fee="mats")
        p.discord_channel_id = fake_id(892)
        pub = str(p.public_id)
        s.execute(text("delete from outbox where kind = 'discord.render_board_post'"))
        with pytest.raises(PermissionDenied):
            boards.edit(s, cfg, OTHER, pub, {"body": "x"})
        with pytest.raises(Invalid, match="Put in the fee"):
            boards.edit(s, cfg, HELPER, pub, {"body": "Bring leather", "fee": "fee"})
        boards.edit(s, cfg, HELPER, pub, {"body": "Bring  leather ", "fee": "mats"})     # nothing changed
        assert _jobs(s, "discord.render_board_post") == []
        boards.edit(s, cfg, HELPER, pub, {"body": "Materials on me", "fee": "fee", "fee_gold": "2"})
        assert (p.body, p.fee_kind, p.fee_copper) == ("Materials on me", "fee", 20000)
        assert [j.payload["post_id"] for j in _jobs(s, "discord.render_board_post")] == [p.id]
        a, _ch = _adv(s, cfg)                                   # other kinds: details only, fee ignored
        boards.edit(s, cfg, POSTER, str(a.public_id), {"body": "New plan", "fee": "fee", "fee_gold": "5"})
        assert a.body == "New plan" and a.fee_kind is None
        boards.close(s, cfg, HELPER, pub, "done")
        with pytest.raises(InvalidTransition):
            boards.edit(s, cfg, HELPER, pub, {"body": "late"})


def test_web_edit_post(seeded, tmp_path):
    a, cfg, tw = _web(seeded, tmp_path)
    with seeded.tx() as s:
        c = _crafter(s, cfg)
        pub = str(_offer(s, cfg, c, body="Bring leather", fee="mats_fee", fee_gold="1", fee_silver="50").public_id)
    h = tw.login(a, 41)
    url = f"/wow/quester/board/{pub}"
    page = h.get(url).text
    assert '<details class="pb-edit"><summary class="btn ghost">Edit</summary>' in page
    assert 'value="mats_fee" selected' in page and 'name="fee_gold" inputmode="numeric" pattern="[0-9]*" maxlength="6" placeholder="0" value="1"' in page
    assert 'name="fee_copper" inputmode="numeric" pattern="[0-9]*" maxlength="2" placeholder="0" value=""' in page
    r = h.post(url + "/edit", data={"csrf": tw.csrf(h, url), "body": "Typed this", "fee": "fee", "fee_silver": "100"})
    assert r.status_code == 400 and "from 0 to 99" in r.text and '<details class="pb-edit" open>' in r.text
    assert ">Typed this</textarea>" in r.text and 'value="100" aria-label="Silver (0 to 99)" class="is-bad"' in r.text
    r = h.post(url + "/edit", data={"csrf": tw.csrf(h, url), "body": "Typed this", "fee": "fee", "fee_gold": "3"})
    assert r.status_code == 303
    page = tw.login(a, 40).get(url).text
    assert "Typed this" in page and "materials included" in page and "pb-edit" not in page   # not their post


def test_answers_pick_which_times_work(seeded, cfg):
    from questersboard.bot.client import TCID, _board_times
    from questersboard.bot.render import board_message
    with seeded.tx() as s:
        p, _ = _adv(s, cfg, kind="company", windows=[(at(24), at(26)), (at(48), at(50)), (at(72), at(74))])
        pub = str(p.public_id)
        char(s, cfg, HELPER, "Fixturehelp")
        boards.respond(s, cfg, HELPER, pub, "need", windows=["1", "2"])
        boards.respond(s, cfg, OTHER, pub, "need")                      # no picks: every time
        assert boards.time_counts(s, p) == [1, 2, 2]
        assert boards.best_time(s, p) == boards.windows_of(p)[1]        # most people, earliest of a tie
        for bad, msg in (([], "Tick at least one"), (["7"], "Pick from the times"), (["x"], "Pick from the times")):
            with pytest.raises(Invalid, match=msg):
                boards.respond(s, cfg, HELPER, pub, "need", windows=bad)
        boards.respond(s, cfg, HELPER, pub, "need", windows=["2"])      # change my ticks
        assert boards.time_counts(s, p) == [1, 1, 2]
        v = boards.view(s, cfg, roster.ensure_member(s, HELPER), p)
        assert v.picks and [t["picked"] for t in v.times] == [False, False, True]
        assert [t["best"] for t in v.times] == [False, False, True] and v.labels == {}
        pv = boards.view(s, cfg, roster.ensure_member(s, POSTER), p)
        assert not pv.picks and pv.labels == {"need": "In"} and len(pv.named["need"]) == 2
        msg = board_message(s, cfg, p)
        works = next(f["value"] for f in msg["embeds"][0]["fields"] if f["name"] == "Works for")
        lines = works.split("\n")
        assert len(lines) == 3 and all(x.startswith("• ") for x in lines)
        assert lines[0].endswith("(+1)") and lines[2].endswith("(+2)")
        menu = msg["components"][1]["components"][0]
        assert menu["type"] == 3 and re.fullmatch(TCID, menu["custom_id"]) and menu["max_values"] == 3
        assert [o["value"] for o in menu["options"]] == ["0", "1", "2"] and "UTC)" in menu["options"][0]["description"]
        # the card's menu answers I'm in when there's one button, and saves the picks
        new = actor(44, name="FixtureNew")
        char(s, cfg, new, "Fixturenew")
        assert "You're down as I'm in" in _board_times(s, cfg, new, p.id, ["0"])
        assert boards.time_counts(s, p) == [2, 1, 2]
        # on a goal with two buttons, press one first
        q, _ = _adv(s, cfg, name="Fixtureq")
        with pytest.raises(Invalid, match="Press I need this too or I can help first"):
            _board_times(s, cfg, new, q.id, ["0"])
        # profession posts and posts without times have no menu
        r, _ = _adv(s, cfg, name="Fixturer", windows=[])
        assert len(board_message(s, cfg, r)["components"]) == 1


def test_web_pick_times(seeded, tmp_path):
    a, cfg, tw = _web(seeded, tmp_path)
    with seeded.tx() as s:
        p, _ = _adv(s, cfg, kind="company", windows=[(at(24), at(26)), (at(48), at(50))])
        pub = str(p.public_id)
    h = tw.login(a, 41)
    url = f"/wow/quester/board/{pub}"
    page = h.get(url).text
    assert '<ul class="ab-timelist">' in page and f'form="pick-{pub}" checked' in page
    assert f'type="submit" form="pick-{pub}" name="kind" value="need">I&#39;m in</button>' in page
    r = h.post(url + "/respond", data={"csrf": tw.csrf(h, url), "times": "1", "kind": "need"})
    assert r.status_code == 422 and "Tick at least one time" in r.text
    r = h.post(url + "/respond", data={"csrf": tw.csrf(h, url), "times": "1", "kind": "need", "w": "1"})
    assert r.status_code == 303
    page = h.get(url).text
    assert "Save my times" in page and page.count(" checked") == 1 and "(+1)" in page
    poster = tw.login(a, 40).get(url).text
    assert "(+1)" in poster and "Most people" in poster and 'name="w"' not in poster and "In: " in poster
