"""One account menu, the get-started page, My characters list + detail, and sharing that saves as you click.
Blizzard answers are FIXTURES (see test_character_details); no live Battle.net login is made."""
from __future__ import annotations

import re

from conftest import actor, fake_id, needs_db
from helpers import char
from sqlalchemy import text
from test_character_details import _handler
from test_web import _bz_app, csrf, login


def _verify(seeded, cid):
    with seeded.tx() as s:
        s.execute(text("update characters set verification='ownership_verified', api_refreshed_at=now() "
                       "where id=:i"), {"i": cid})


@needs_db
def test_first_visit_shows_get_started_with_regions(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    c = login(a, 150)
    page = c.get("/wow/quester/characters").text
    assert "Bring in your characters" in page and "Continue with Battle.net" in page
    assert 'name="region" value="us" checked' in page and "Americas &amp; Oceania" in page and "Europe" in page
    assert "Not now, add a character by hand" in page and "never keeps your Battle.net login" in page
    # "Not now" opens the add dialog instead
    assert 'data-mode="add" open>' in c.get("/wow/quester/characters?add=1").text
    assert "Continue with Battle.net" in c.get("/wow/quester/characters/start").text
    # the region choice goes straight to one Battle.net login
    r = c.get("/wow/quester/characters/battlenet?region=eu")
    assert r.status_code == 303 and "scope=wow.profile" in r.headers["location"]
    assert c.get("/wow/quester/characters/battlenet?region=zz").status_code == 422


@needs_db
def test_account_menu_on_every_page(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(151), "Zulmara Ashveil").id
    c = login(a, 151)
    for path in ("/wow/quester/", "/wow/quester/characters", "/wow/quester/players", "/wow/quester/me"):
        page = c.get(path).text
        assert 'class="wq-acct"' in page, path
        assert "Discord · Nick151" in page and "Not connected" in page and "Connect Battle.net" in page
        assert "My characters <span class=\"note\">1</span>" in page and "Log out on all devices" in page
    _verify(seeded, cid)
    page = c.get("/wow/quester/").text
    assert "Connected · Americas &amp; Oceania" in page and "1 character verified" in page
    assert "Sync now" in page and "+ Europe" in page and "wq-dot-bn" in page
    # logged out: no menu, the Discord login instead
    from fastapi.testclient import TestClient
    anon = TestClient(a, base_url="https://community.example.org", follow_redirects=False)
    assert 'class="wq-acct"' not in anon.get("/wow/quester/help").text
    # public pages show the same signed-in header (read-only session), marked private; anonymous stays cacheable
    r = c.get("/wow/quester/help")
    assert 'class="wq-acct"' in r.text and r.headers["cache-control"] == "private, no-store"
    assert "set-cookie" not in r.headers and r.headers["vary"] == "Cookie"
    r = anon.get("/wow/quester/help")
    assert "cache-control" not in r.headers and r.headers["vary"] == "Cookie"


@needs_db
def test_list_detail_and_sharing_saves_or_explains(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(152), "Zulmara Ashveil").id
        other = char(s, cfg, actor(152), "Fixture Second").id
    c = login(a, 152)
    assert c.post(f"/wow/quester/characters/{cid}/details",
                  data={"csrf": csrf(c), "groups": ["gear", "talents", "guild"]}).status_code == 303
    page = c.get(f"/wow/quester/characters?c={cid}").text
    assert 'class="wq-cm wq-cm-chosen"' in page                 # phones show the detail alone
    assert 'aria-current="true" id="char-' + str(cid) + '"' in page and "Not verified" in page
    assert "Who can see what" in page and "Guild and All members open once Zulmara Ashveil is verified" in page
    assert page.count("data-locked") >= 2 and "Verify with Battle.net" in page and "Open full page" in page
    assert "Remove character" in page and "Edit roles and notes" in page and "&larr; All characters" in page
    plain = c.get("/wow/quester/characters").text
    assert 'class="wq-cm"' in plain and "wq-cm-chosen" not in plain   # phones start on the list
    # Not verified: asking for Guild is refused with a reason, nothing saved
    calls.clear()
    r = c.post(f"/wow/quester/characters/{cid}/sharing", data={"csrf": csrf(c), "share_gear": "guild",
                                                                "share_talents": "private", "share_guild": "private"})
    assert r.status_code == 303 and f"refused={cid}" in r.headers["location"]
    page = c.get(r.headers["location"]).text
    assert "isn't verified yet.</strong> Your choice wasn't saved" in page
    assert re.search(r'data-share-refused>\s', page)              # shown (not hidden)
    with seeded.tx() as s:
        assert s.execute(text("select shares from character_profiles where character_id=:i"), {"i": cid}
                         ).scalar_one() == {"gear": "private", "talents": "private", "guild": "private"}
    # Verified: one click saves, no Blizzard calls; "All sections" sets every one
    _verify(seeded, cid)
    r = c.post(f"/wow/quester/characters/{cid}/sharing", data={"csrf": csrf(c), "share_gear": "guild",
                                                                "share_talents": "members", "share_guild": "private"})
    assert f"shared={cid}" in r.headers["location"]
    assert "Sharing saved for Zulmara Ashveil." in c.get(r.headers["location"]).text
    r = c.post(f"/wow/quester/characters/{cid}/sharing", data={"csrf": csrf(c), "share_all": "members"})
    with seeded.tx() as s:
        sh = s.execute(text("select shares from character_profiles where character_id=:i"), {"i": cid}).scalar_one()
    assert sh == {"gear": "members", "talents": "members", "guild": "members"} and not calls
    page = c.get(f"/wow/quester/characters?c={cid}").text
    assert 'name="share_all" value="members" checked' in page and "All members" in page
    # someone else's character, or one with nothing imported, can't be changed
    o = login(a, 153)
    assert o.post(f"/wow/quester/characters/{cid}/sharing", data={"csrf": csrf(o), "share_all": "private"}
                  ).status_code == 404
    assert c.post(f"/wow/quester/characters/{other}/sharing", data={"csrf": csrf(c), "share_all": "private"}
                  ).status_code == 404


@needs_db
def test_disconnect_one_region_keeps_sharing_and_logout_everywhere(seeded, tmp_path, monkeypatch):
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(154), "Zulmara Ashveil").id
        eu = char(s, cfg, actor(154), "Fixture Eurochar", edition="retail", region="eu",
                  realm_slug="fixture-realm", ruleset=None).id
    c = login(a, 154)
    c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["gear"]})
    _verify(seeded, cid)
    _verify(seeded, eu)
    c.post(f"/wow/quester/characters/{cid}/sharing", data={"csrf": csrf(c), "share_gear": "members"})
    r = c.post("/wow/quester/characters/battlenet/unlink", data={"csrf": csrf(c), "region": "us"})
    assert r.status_code == 303 and "disconnected=1" in r.headers["location"]
    assert "Battle.net disconnected" in c.get(r.headers["location"]).text
    with seeded.tx() as s:
        got = dict(s.execute(text("select id, verification from characters where id in (:a,:b)"),
                             {"a": cid, "b": eu}).all())
        sh = s.execute(text("select shares from character_profiles where character_id=:i"), {"i": cid}).scalar_one()
    assert got == {cid: "self_reported", eu: "ownership_verified"}       # only that region
    assert sh == {"gear": "members"}                                     # kept for when it's verified again
    # Log out on all devices ends every session of this member, not just this one
    other_device = login(a, 154)
    assert c.post("/wow/quester/logout", data={"csrf": csrf(c), "everywhere": "1"}).status_code == 303
    assert other_device.get("/wow/quester/characters").status_code == 303            # back to login
    with seeded.tx() as s:
        assert s.execute(text("select count(*) from web_sessions w join members m on m.id=w.member_id "
                              "where m.discord_id=:d"), {"d": fake_id(154)}).scalar_one() == 0
