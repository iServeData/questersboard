"""Submenu under the main menu: section dropdowns on member and public pages, config checks."""
from __future__ import annotations

import re

import pytest
from conftest import make_cfg, needs_db
from fastapi.testclient import TestClient
from test_web import app, login, wcfg  # noqa: F401  (fixtures)

from questersboard.config import ConfigError


def _subnav(html: str) -> str:
    m = re.search(r'<nav class="subnav wq-subnav".*?</nav>', html, re.S)
    assert m, "submenu missing"
    return m.group(0)


def _menu(html: str) -> str:
    return re.search(r'<ul id="menu">.*?</ul>', html, re.S).group(0)


def _section(sub: str, key: str) -> str:
    return re.search(rf'<li><button[^>]*aria-controls="wq-dd-{key}">.*?</div></li>', sub, re.S).group(0)


def test_subnav_defaults_follow_game_page():
    b = make_cfg(branding={"game_page_url": "/wow/"})["branding"]
    assert b["subnav_lead_label"] == "WoW" and b["subnav_lead"] == [{"label": "Schedule", "url": "/wow/#schedule"}]
    assert b["subnav_label"] == "QuestersBoard" and b["subnav_label_url"] == "/wow/#questersboard"
    b = make_cfg(branding={"game_page_url": "", "subnav_lead": []})["branding"]
    assert b["subnav_lead_label"] == "" and b["subnav_lead"] == [] and b["subnav_label_url"] == ""


def test_subnav_config_checks():
    for bad in ({"subnav_label_url": "javascript:alert(1)"}, {"subnav_lead": [{"label": "x", "url": "http://a"}]},
                {"subnav_lead": [{"label": "", "url": "/x"}]}, {"subnav_label": "x" * 41},
                {"subnav_lead": [{"label": "x", "url": "/x"}] * 7}):
        with pytest.raises(ConfigError):
            make_cfg(branding=bad)


@needs_db
def test_member_page_submenu(app):  # noqa: F811
    c = login(app, 301)
    html = c.get("/wow/quester/characters").text
    sub, menu = _subnav(html), _menu(html)
    assert ">Quests<" not in menu and ">Chronicle<" not in menu   # moved into the submenu
    for label in ("Quests", "Characters", "Chronicle", "Help"):
        assert f">{label}" in sub
    chars = _section(sub, "characters")
    assert 'class="wq-dd-btn is-cur"' in chars and '(current section)' in chars
    assert 'href="/wow/quester/characters" aria-current="page">Mine<' in chars
    assert ">Plan an event<" in _section(sub, "quests")
    assert "Review queue" not in sub
    assert "Shared characters</a>" not in html.split("</header>", 1)[1]   # redundant page button gone
    assert _section(sub, "quests").count("is-cur") == 0


@needs_db
def test_reviewer_sees_review_queue(app):  # noqa: F811
    c = login(app, 302, "chronicle_reviewer")
    sub = _subnav(c.get("/wow/quester/").text)
    assert 'href="/wow/quester/review">Review queue<' in _section(sub, "chronicle")
    assert 'is-cur' in _section(sub, "quests")
    assert 'href="/wow/quester/" aria-current="page">Events<' in sub


@needs_db
@pytest.mark.parametrize("path,label", [("/wow/quester/privacy", "Privacy"), ("/wow/quester/help", "How it works")])
def test_public_pages_have_same_submenu(app, path, label):  # noqa: F811
    c = TestClient(app, base_url="https://community.example.org")
    r = c.get(path)
    assert r.status_code == 200 and "set-cookie" not in r.headers
    sub = _subnav(r.text)
    help_ = _section(sub, "help")
    assert "is-cur" in help_ and f'aria-current="page">{label}<' in help_
    assert "/guilds/" not in sub and "Review queue" not in sub
    assert ">Plan an event<" in sub and ">Mine<" in sub


@needs_db
def test_guild_roster_in_characters_menu(seeded, tmp_path, monkeypatch):
    from conftest import actor
    from helpers import char
    from test_account_ux import _verify
    from test_character_details import _handler
    from test_web import _bz_app, csrf
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(311), "Zulmara Ashveil").id
    c = login(a, 311)
    c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["guild"]})
    _verify(seeded, cid)
    c.post(f"/wow/quester/characters/{cid}/sharing", data={"csrf": csrf(c), "share_guild": "guild"})
    chars = _section(_subnav(c.get("/wow/quester/").text), "characters")
    names = re.findall(r">([^<>]+)</a>", chars)
    assert names[:2] == ["Mine", "Players"] and names[-1] == "Who can craft" and len(names) == 4
    assert "/wow/quester/guilds/" in chars


@needs_db
def test_same_menus_on_every_page_when_signed_in(seeded, tmp_path, monkeypatch):
    """Public pages (help, privacy, terms, changelog, chronicle) show the signed-in header and the same
    submenu as member pages; only the current-section/page markers differ."""
    from conftest import actor
    from helpers import char
    from test_account_ux import _verify
    from test_character_details import _handler
    from test_web import _bz_app, csrf
    a, cfg, _ = _bz_app(seeded, tmp_path, monkeypatch, _handler)
    with seeded.tx() as s:
        cid = char(s, cfg, actor(312), "Zulmara Ashveil").id
    c = login(a, 312)
    c.post(f"/wow/quester/characters/{cid}/details", data={"csrf": csrf(c), "groups": ["guild"]})
    _verify(seeded, cid)
    c.post(f"/wow/quester/characters/{cid}/sharing", data={"csrf": csrf(c), "share_guild": "guild"})

    def plain(html):
        sub = _subnav(html)
        sub = re.sub(r' aria-current="page"| is-cur|<span class="sr-only"> \(current section\)</span>', "", sub)
        return sub, 'class="wq-acct"' in _menu(html)
    pages = ["/wow/quester/", "/wow/quester/events/new", "/wow/quester/characters", "/wow/quester/players",
             "/wow/quester/help", "/wow/quester/privacy", "/wow/quester/terms", "/wow/quester/changelog",
             "/wow/chronicle/"]
    seen = {p: plain(c.get(p).text) for p in pages}
    first = seen[pages[0]]
    assert first[1] and "/wow/quester/guilds/" in first[0]
    for p, v in seen.items():
        assert v == first, p
