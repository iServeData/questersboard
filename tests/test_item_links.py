"""[item_links]: item cards for [Item Name] in chosen channels, posted through a webhook.
Discord and Blizzard are FIXTURES (in-memory stand-ins); item and channel names are made up."""
from __future__ import annotations

import httpx
import pytest
from conftest import fake_id, make_cfg, needs_db
from fake_discord import FakeDiscord
from sqlalchemy import text

from questersboard.config import ConfigError, ConfigHolder
from questersboard.domain import item_links
from questersboard.integrations.blizzard import Blizzard
from questersboard.secrets import redact

WATCH, OTHER = fake_id(70), fake_id(71)
ON = {"enabled": True, "watch_channels": [WATCH]}

# FIXTURE item data, shaped like Blizzard's item search and item answers
ITEMS = {
    101: {"name": "Fixture Blade of Testing", "quality": "EPIC"},
    102: {"name": "Fixture Blade of Testing", "quality": "RARE"},       # same name, higher id
    103: {"name": "Fixture Blade of Testing Mk II", "quality": "COMMON"},
    104: {"name": "Fixture Cloak", "quality": "UNCOMMON"},
    105: {"name": "Fixture Old Shield", "quality": "RARE", "only": "classic1x"},   # missing from the main edition
    106: {"name": "Pattern: Fixture Cloak", "quality": "UNCOMMON", "recipe": True},
}


def _served(iid, ns):
    only = ITEMS[iid].get("only")
    return (only in ns) if only else "classic1x" not in ns


def _item_body(iid):
    it = ITEMS[iid]
    if it.get("recipe"):
        return {"id": iid, "name": it["name"], "quality": {"type": it["quality"]},
                "preview_item": {"name": it["name"], "inventory_type": {"type": "NON_EQUIP", "name": "Non-equippable"},
                                 "requirements": {"skill": {"display_string": "Requires Tailoring (100)"}},
                                 "spells": [{"description": "Use: Teaches you how to sew a Fixture Cloak."}],
                                 "recipe": {"item": {"name": "Fixture Cloak", "id": 104},
                                            "reagents": [{"reagent": {"name": "Fixture Cloth"}, "quantity": 3},
                                                         {"reagent": {"name": "Fixture Thread"}, "quantity": 1}]}}}
    return {"id": iid, "name": it["name"], "quality": {"type": it["quality"], "name": it["quality"].title()},
            "preview_item": {
                "name": it["name"], "binding": {"type": "ON_EQUIP", "name": "Binds when equipped"},
                "inventory_type": {"name": "One-Hand"}, "item_subclass": {"name": "Sword"},
                "weapon": {"damage": {"display_string": "41 - 77 Damage"},
                           "attack_speed": {"display_string": "Speed 2.60"},
                           "dps": {"display_string": "(22.7 damage per second)"}},
                "stats": [{"display": {"display_string": "+7 Stamina"}}],
                "durability": {"display_string": "Durability 90 / 90"},
                "requirements": {"level": {"display_string": "Requires Level 40"}},
                "sell_price": {"display_strings": {"gold": "1", "silver": "2", "copper": "3"}}}}


def _fake_bz(cfg, searches):
    def h(req):
        p = req.url.path
        if p == "/token":
            return httpx.Response(200, json={"access_token": "FIXTURE", "expires_in": 3600})
        if p == "/data/wow/search/item":
            name = req.url.params["name.en_US"]
            searches.append((name, req.url.params["_page"]))
            ns = req.url.params["namespace"]
            hits = [{"data": {"id": i, "name": {"en_US": v["name"]}}} for i, v in sorted(ITEMS.items(), reverse=True)
                    if name.casefold() in v["name"].casefold() and _served(i, ns)]
            return httpx.Response(200, json={"page": 1, "pageCount": 1, "results": hits})
        if p.startswith("/data/wow/item/"):
            iid = int(p.rsplit("/", 1)[1])
            if not _served(iid, req.url.params["namespace"]):
                return httpx.Response(404)
            return httpx.Response(200, json=_item_body(iid))
        if p.startswith("/data/wow/media/item/"):
            iid = p.rsplit("/", 1)[1]
            return httpx.Response(200, json={"assets": [
                {"key": "icon", "value": f"https://render.worldofwarcraft.com/us/icons/56/fixture_{iid}.jpg"}]})
        return httpx.Response(404)
    return Blizzard(cfg, http=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda _s: None)


@pytest.fixture(autouse=True)
def _bz_creds(monkeypatch):
    monkeypatch.setenv("QUESTERSBOARD_SECRET_BLIZZARD_CLIENT_ID", "FIXTURE-id")
    monkeypatch.setenv("QUESTERSBOARD_SECRET_BLIZZARD_CLIENT_SECRET", "FIXTURE-secret")


def _cfg(**il):
    return make_cfg(blizzard={"enabled": True}, item_links={**ON, **il})


# ------------------------------------------------------------------ parsing and filters
def test_parse_names_finds_brackets_once_in_order():
    t = ("need [Fixture Cloak] and [fixture cloak] and [Fixture Blade of Testing] but not [a](https://x.example) "
         "or [...] or [\u2026 sure] or [42] or [x] or [line\nbreak]")
    assert item_links.parse_names(t, 5) == ["Fixture Cloak", "Fixture Blade of Testing"]
    assert item_links.parse_names("[A1 b] [C2 d] [E3 f]", 2) == ["A1 b", "C2 d"]
    assert item_links.parse_names("[  Fixture   Cloak ]", 5) == ["Fixture Cloak"]
    assert item_links.parse_names("", 5) == []


def test_should_read_only_watched_channels_and_never_our_own_cards():
    il = _cfg()["item_links"]
    base = dict(self_id="me", author_id="u", is_bot=False, webhook_id=None, author_name="Fixture Player")
    assert item_links.should_read(il, WATCH, **base)
    assert not item_links.should_read(il, OTHER, **base)
    assert not item_links.should_read(il, WATCH, **{**base, "author_id": "me"})
    assert not item_links.should_read(il, WATCH, **{**base, "webhook_id": "h1", "author_name": il["webhook_name"]})
    # relayed game chat may arrive as a webhook or a bot; both are read unless ignore_bots
    assert item_links.should_read(il, WATCH, **{**base, "webhook_id": "h2", "author_name": "Fixture Relay"})
    assert item_links.should_read(il, WATCH, **{**base, "is_bot": True})
    il2 = _cfg(ignore_bots=True)["item_links"]
    assert not item_links.should_read(il2, WATCH, **{**base, "is_bot": True})
    assert not item_links.should_read(make_cfg()["item_links"], WATCH, **base)       # off by default


def test_cooldown_per_channel_and_name():
    t = [0.0]
    cd = item_links.Cooldown(clock=lambda: t[0])
    assert cd.allow(WATCH, ["Fixture Cloak", "Fixture Blade of Testing"], 10) == ["Fixture Cloak", "Fixture Blade of Testing"]
    assert cd.allow(WATCH, ["fixture cloak"], 10) == []
    assert cd.allow(OTHER, ["Fixture Cloak"], 10) == ["Fixture Cloak"]
    t[0] = 601
    assert cd.allow(WATCH, ["Fixture Cloak"], 10) == ["Fixture Cloak"]
    assert cd.allow(WATCH, ["Fixture Ring"], 0) == ["Fixture Ring"] and cd.allow(WATCH, ["Fixture Ring"], 0)


# ------------------------------------------------------------------ config
def test_config_defaults_off_and_validates():
    il = make_cfg()["item_links"]
    assert il["enabled"] is False and il["watch_channels"] == [] and il["webhook_name"] == "QuestersBoard Items"
    assert il["edition"] in make_cfg().editions and il["max_per_message"] == 5
    for bad, msg in (({"enabled": True, "watch_channels": []}, "watch_channels"),
                     ({"webhook_name": "My Discord Bot"}, "webhook_name"),
                     ({"max_per_message": 11}, "max_per_message"),
                     ({"avatar_url": "http://example.org/a.png"}, "avatar_url"),
                     ({"edition": "nope"}, "edition"),
                     ({"surprise": 1}, "unknown"),
                     ({"watch_channels": ["not-a-channel"]}, "watch_channels"),
                     ({"not_found_text": "Nothing here"}, "not_found_text")):
        with pytest.raises(ConfigError, match=msg):
            make_cfg(blizzard={"enabled": True}, item_links=bad)
    with pytest.raises(ConfigError, match="blizzard"):
        make_cfg(item_links=ON)


# ------------------------------------------------------------------ lookup and card
def test_lookup_takes_exact_name_lowest_id_and_builds_a_card():
    cfg = _cfg()
    searches = []
    bz = _fake_bz(cfg, searches)
    ed = cfg.edition(cfg["item_links"]["edition"])
    region = cfg["item_links"]["region"]
    trace = []
    got = item_links.lookup(None, bz, ed, region, 0, "fixture blade of testing", 3, trace)
    assert got["id"] == 101 and got["quality"] == "epic" and trace[0]["exact"] == [101, 102]
    assert got["icon"].endswith("fixture_101.jpg")
    e = item_links.card({**got, "footer": "Fixture footer"})
    assert e["title"] == "Fixture Blade of Testing" and e["color"] == 0xA335EE and "url" not in e
    lines = e["description"].split("\n")
    assert lines[:3] == ["Binds when equipped", "One-Hand · Sword", "41 - 77 Damage · Speed 2.60"]
    assert "+7 Stamina" in lines and "Requires Level 40" in lines and lines[-1] == "Sell Price: 1g 2s 3c"
    assert e["thumbnail"]["url"].endswith("fixture_101.jpg") and e["footer"]["text"] == "Fixture footer"
    assert item_links.lookup(None, bz, ed, region, 0, "Fixture Nothing", 3) is None


def test_message_says_not_found_and_escapes_formatting():
    m = item_links.message([], ["Fixture *bold* `x`"], "https://discord.com/channels/1/2/3", True)
    assert m["content"].startswith("Items from https://discord.com/channels/1/2/3\n")
    assert "No item called **Fixture \\*bold\\* \\`x\\`** found in Blizzard's public game data." in m["content"]
    assert m["allowed_mentions"] == {"parse": []}
    assert item_links.message([], ["Fixture Nothing"], "", False) is None
    only = item_links.message([{"name": "Fixture Cloak", "quality": "common"}], [], "", True)
    assert "content" not in only and only["embeds"][0]["title"] == "Fixture Cloak"
    m = item_links.message([], ["Fixture Nothing"], "", True, "Fixture text {name}.")
    assert m["content"] == "Fixture text Fixture Nothing."


def test_webhook_tokens_are_redacted_from_logs():
    line = "POST https://discord.com/api/v10/webhooks/123/abcdefghijklmnopqrstuvwxyz0123456789?wait=true"
    assert "abcdefghijklmnop" not in redact(line) and "/webhooks/123/[REDACTED]" in redact(line)


# ------------------------------------------------------------------ worker (database)
def _worker(cfg, worker_db, fake):
    from questersboard.worker import Worker
    w = Worker(ConfigHolder(cfg), worker_db, fake.client())
    w.bz = _fake_bz(cfg, [])
    return w


def _drain(w):
    while w.tick():
        pass


@needs_db
def test_worker_posts_cards_through_one_reused_webhook_then_removes_them(db, worker_db):
    cfg = _cfg()
    fake = FakeDiscord()
    w = _worker(cfg, worker_db, fake)
    m1, m2 = fake_id(501), fake_id(502)
    with db.tx() as s:
        item_links.enqueue(s, WATCH, m1, ["Fixture Cloak", "Fixture Nothing"])
        item_links.enqueue(s, WATCH, m2, ["Fixture Blade of Testing"])
    _drain(w)
    assert len(fake.webhooks) == 1                                # created once, then found again
    hook = next(iter(fake.webhooks.values()))
    assert hook["channel"] == WATCH and hook["name"] == "QuestersBoard Items"
    first = fake.hook_posts[0]["body"]
    assert first["username"] == "QuestersBoard Items" and first["allowed_mentions"] == {"parse": []}
    assert first["embeds"][0]["title"] == "Fixture Cloak" and first["embeds"][0]["color"] == 0x1EFF00
    assert first["content"] == "No item called **Fixture Nothing** found in Blizzard's public game data."   # no link back
    assert first["embeds"][0]["footer"]["text"] == "Source: Blizzard · WoW: Forever API"
    assert not any(m == "POST" and p.startswith(f"/channels/{WATCH}/messages") for m, p in fake.calls)
    with db.tx() as s:
        pl = s.execute(text("SELECT payload FROM outbox WHERE kind='item_links.post' AND key=:k"),
                       {"k": f"{WATCH}:{m1}"}).scalar()
        assert pl["card_id"] == fake.hook_posts[0]["id"] and set(pl) >= {"names", "message_id", "channel_id"}
        assert not {"content", "author", "author_id"} & set(pl)
        assert item_links.enqueue_delete(s, WATCH, m1)
        assert not item_links.enqueue_delete(s, WATCH, fake_id(599))        # never had cards
    _drain(w)
    assert fake.hook_deletes == [(next(iter(fake.webhooks)), fake.hook_posts[0]["id"])]


@needs_db
def test_worker_recreates_a_deleted_webhook_and_skips_deleted_pending_messages(db, worker_db):
    cfg = _cfg()
    fake = FakeDiscord()
    w = _worker(cfg, worker_db, fake)
    with db.tx() as s:
        item_links.enqueue(s, WATCH, fake_id(511), ["Fixture Cloak"])
    _drain(w)
    fake.webhooks.clear()                                         # someone deleted it in Discord
    with db.tx() as s:
        item_links.enqueue(s, WATCH, fake_id(512), ["Fixture Cloak"])
    _drain(w)
    assert len(fake.hook_posts) == 2 and len(fake.webhooks) == 1
    with db.tx() as s:                                            # deleted before the worker got to it
        item_links.enqueue(s, WATCH, fake_id(513), ["Fixture Cloak"])
        item_links.enqueue_delete(s, WATCH, fake_id(513))
    w.run_job(next(j for j in w.claim() if j["kind"] == "item_links.delete"))
    _drain(w)
    assert len(fake.hook_posts) == 2


@needs_db
def test_worker_without_manage_webhooks_stops_and_records_why(db, worker_db):
    cfg = _cfg()
    fake = FakeDiscord()
    fake.no_manage_webhooks.add(WATCH)
    w = _worker(cfg, worker_db, fake)
    with db.tx() as s:
        item_links.enqueue(s, WATCH, fake_id(521), ["Fixture Cloak"])
    _drain(w)
    with db.tx() as s:
        dead, err = s.execute(text("SELECT dead_at IS NOT NULL, last_error FROM outbox WHERE kind='item_links.post'")).one()
    assert dead and "50013" in err and not fake.hook_posts


@needs_db
def test_worker_ignores_jobs_when_turned_off(db, worker_db):
    fake = FakeDiscord()
    w = _worker(make_cfg(blizzard={"enabled": True}), worker_db, fake)
    with db.tx() as s:
        item_links.enqueue(s, WATCH, fake_id(531), ["Fixture Cloak"])
    _drain(w)
    assert not fake.hook_posts and not fake.webhooks


# ------------------------------------------------------------------ bot and privacy
def test_bot_asks_for_message_content_only_when_turned_on():
    from questersboard.bot import client as bc
    old = getattr(bc.Ctx, "holder", None)
    try:
        bc.Ctx.holder = ConfigHolder(make_cfg())
        b = bc.Bot()
        assert not b.intents.message_content and not b.intents.guild_messages and not b.item_links
        bc.Ctx.holder = ConfigHolder(_cfg())
        b = bc.Bot()
        assert b.intents.message_content and b.intents.guild_messages and b.item_links
    finally:
        if old is not None:
            bc.Ctx.holder = old


@needs_db
def test_privacy_page_mentions_item_cards_only_when_on(seeded, tmp_path):
    from fastapi.testclient import TestClient
    from test_web import FakeOAuth, FakeRest

    from questersboard.web.app import create_app
    for il, expect in (({}, False), (ON, True)):
        cfg = make_cfg(tmp_path, web={"cookie_secure": False}, blizzard={"enabled": True}, item_links=il)
        a = create_app(ConfigHolder(cfg), seeded, FakeOAuth(), FakeRest())
        p = TestClient(a, base_url="https://community.example.org").get("/wow/quester/privacy").text
        assert ("Item cards." in p) is expect and ("[square brackets]" in p) is expect
        assert "webhook" not in p.lower()


def test_probe_item_prints_the_search_and_card(monkeypatch, capsys):
    from questersboard import cli
    cfg = _cfg()
    monkeypatch.setattr(cli, "_cfg", lambda _a: cfg)
    monkeypatch.setattr("questersboard.integrations.blizzard.Blizzard", lambda c: _fake_bz(c, []))
    ed = cfg["item_links"]["edition"]
    assert cli.main(["blizzard", "probe-item", ed, "--name", "Fixture Cloak"]) == 0
    out = capsys.readouterr().out
    assert "exact matches [104]" in out and "chosen item 104 (uncommon) from forever" in out
    assert '"title": "Fixture Cloak"' in out and "classic_era" not in out          # found first time: no fallback
    assert cli.main(["blizzard", "probe-item", ed, "--name", "Fixture Old Shield"]) == 0
    out = capsys.readouterr().out
    assert "chosen item 105 (rare) from classic_era" in out and "not in WoW: Forever yet" in out
    assert cli.main(["blizzard", "probe-item", ed, "--name", "Fixture Nothing"]) == 1
    assert cli.main(["blizzard", "probe-item", ed]) == 2


@needs_db
def test_cards_in_another_channel_link_back_to_the_message(db, worker_db):
    post = fake_id(72)
    cfg = _cfg(post_channel=post)
    fake = FakeDiscord()
    w = _worker(cfg, worker_db, fake)
    with db.tx() as s:
        item_links.enqueue(s, WATCH, fake_id(541), ["Fixture Cloak"])
    _drain(w)
    assert next(iter(fake.webhooks.values()))["channel"] == post
    assert fake.hook_posts[0]["body"]["content"] == f"Items from https://discord.com/channels/{fake_id(1)}/{WATCH}/{fake_id(541)}"


def test_falls_back_to_classic_era_and_says_so_in_the_footer():
    cfg = _cfg()
    assert cfg["item_links"]["fallbacks"] == ["classic_era"]
    bz = _fake_bz(cfg, [])
    trace = []
    got = item_links.find(None, bz, cfg, 0, "Fixture Old Shield", trace=trace)
    assert got["id"] == 105 and [t["edition"] for t in trace] == ["forever", "classic_era"]
    assert got["footer"] == "Source: Blizzard · WoW Classic Era API (not in WoW: Forever yet; may differ)"
    assert item_links.find(None, bz, cfg, 0, "Fixture Cloak")["footer"] == "Source: Blizzard · WoW: Forever API"
    assert item_links.find(None, bz, cfg, 0, "Fixture Nothing") is None
    assert item_links.find(None, bz, _cfg(fallbacks=[]), 0, "Fixture Old Shield") is None
    own = _cfg(source_labels={"classic_era": "Fixture Era"}, footer_fallback_text="From {source}, not {main}")
    assert item_links.find(None, bz, own, 0, "Fixture Old Shield")["footer"] == "From Fixture Era, not WoW: Forever"
    for bad in ({"fallbacks": ["forever"]}, {"fallbacks": ["nope"]}, {"footer_text": "no placeholder"}):
        with pytest.raises(ConfigError, match="fallbacks|footer_text"):
            _cfg(**bad)


def test_recipe_cards_show_what_they_make_and_need_without_non_equippable():
    cfg = _cfg()
    got = item_links.find(None, _fake_bz(cfg, []), cfg, 0, "Pattern: Fixture Cloak")
    lines = item_links.card(got)["description"].split("\n")
    assert lines == ["Requires Tailoring (100)", "Use: Teaches you how to sew a Fixture Cloak.",
                     "Creates: Fixture Cloak", "Reagents: Fixture Cloth (3), Fixture Thread"]


@needs_db
def test_refresh_edits_the_card_already_posted_and_clears_the_not_found_line(db, worker_db):
    fake = FakeDiscord()
    old = _worker(_cfg(fallbacks=[]), worker_db, fake)        # before the fallback existed
    m = fake_id(561)
    with db.tx() as s:
        item_links.enqueue(s, WATCH, m, ["Fixture Cloak", "Fixture Old Shield"])
    _drain(old)
    assert fake.hook_posts[0]["body"]["content"].startswith("No item called **Fixture Old Shield**")
    new = _worker(_cfg(), worker_db, fake)
    with db.tx() as s:
        s.execute(text("INSERT INTO outbox (kind, key, payload) VALUES ('item_links.refresh', :k, CAST(:p AS jsonb))"),
                  {"k": f"{WATCH}:{m}", "p": f'{{"key": "{WATCH}:{m}"}}'})
    _drain(new)
    assert len(fake.hook_posts) == 1 and fake.hook_edits == [(fake.hook_posts[0]["hook"], fake.hook_posts[0]["id"])]
    body = fake.hook_posts[0]["body"]
    assert body["content"] == "" and [e["title"] for e in body["embeds"]] == ["Fixture Cloak", "Fixture Old Shield"]
    assert body["embeds"][1]["footer"]["text"].endswith("(not in WoW: Forever yet; may differ)")
    with db.tx() as s:                                          # a card someone removed stays removed
        fake.hook_posts.clear()
        s.execute(text("INSERT INTO outbox (kind, key, payload) VALUES ('item_links.refresh', :k, CAST(:p AS jsonb))"),
                  {"k": f"{WATCH}:{m}", "p": f'{{"key": "{WATCH}:{m}"}}'})
    _drain(new)
    with db.tx() as s:
        assert s.execute(text("SELECT count(*) FROM outbox WHERE kind='item_links.refresh' AND dead_at IS NOT NULL")).scalar() == 0


def test_recipe_reagent_text_from_blizzard_gets_a_label_once():
    bare = {"recipe": {"reagents_display_string": "Fixture Cloth (5), Fixture Thread"}}
    assert item_links._recipe(bare)["reagents"] == "Reagents: Fixture Cloth (5), Fixture Thread"
    done = {"recipe": {"reagents_display_string": "Reagents: Fixture Cloth (5)"}}
    assert item_links._recipe(done)["reagents"] == "Reagents: Fixture Cloth (5)"
