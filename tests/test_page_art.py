"""[page_art]: one banner + footer picture pair a day, from configuration only.

All picture paths are FIXTURE paths; nothing is fetched.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from conftest import make_cfg, needs_db
from fastapi.testclient import TestClient

from questersboard.config import ConfigError, ConfigHolder
from questersboard.domain import page_art
from questersboard.web.app import create_app

BASE = "https://community.example.org"


def _art(**over) -> dict:
    pa = {
        "enabled": True, "timezone": "America/Los_Angeles", "start_date": "2026-11-01",
        "fx_script": "/assets/js/fx.js",
        "banners": [{"key": k, "image": f"/art/hero_{k}.jpg", "image_mobile": f"/art/hero_{k}_m.jpg"}
                    for k in ("b0", "b1", "b2", "b3")],
        "footers": [{"key": k, "image": f"/art/foot_{k}.jpg", "glow": f"/art/foot_{k}_glow.png",
                     "effect": "fire", "caption": f"Caption {k}"} for k in ("f0", "f1", "f2")],
        "skip_pairs": [["b0", "f2"]],
    }
    pa["banners"][1]["logo"] = "/art/logo.png"
    pa["banners"][1]["rails"] = True
    pa["footers"][2]["effect"] = "arcane"
    pa.update(over)
    return pa


# ------------------------------------------------------------------ rotation (no DB)

def test_four_banners_three_footers_show_every_pairing_once():
    seq = page_art.sequence(4, 3)
    assert len(seq) == 12 and len(set(seq)) == 12
    assert page_art.repeats(seq) == []
    # banner moves forward, footer moves back
    assert [b for b, _ in seq[:5]] == [0, 1, 2, 3, 0]
    assert [f for _, f in seq[:4]] == [0, 2, 1, 0]


def test_skipping_one_pairing_leaves_eleven_days_without_repeats():
    seq = page_art.sequence(4, 3, frozenset({(0, 2)}))
    assert len(seq) == 11 and (0, 2) not in seq
    assert page_art.repeats(seq) == []


def test_equal_lengths_still_show_every_pairing():
    seq = page_art.sequence(3, 3)
    assert len(set(seq)) == 9


def test_pick_follows_the_day_in_the_configured_timezone():
    cfg = make_cfg(page_art=_art())
    seq = page_art.sequence(4, 3, frozenset({(0, 2)}))
    # 2026-11-03 07:30 UTC is still Nov 2 in Los Angeles: day 1
    p = page_art.pick(cfg, datetime(2026, 11, 3, 7, 30, tzinfo=timezone.utc))
    assert (p["banner"]["key"], p["footer"]["key"]) == (f"b{seq[1][0]}", f"f{seq[1][1]}")
    p = page_art.pick(cfg, datetime(2026, 11, 3, 8, 30, tzinfo=timezone.utc))      # midnight passed: day 2
    assert (p["banner"]["key"], p["footer"]["key"]) == (f"b{seq[2][0]}", f"f{seq[2][1]}")
    shown = set()
    for d in range(11):
        p = page_art.pick(cfg, datetime(2026, 11, 1 + d, 20, tzinfo=timezone.utc))
        shown.add((p["banner"]["key"], p["footer"]["key"]))
    assert len(shown) == 11 and ("b0", "f2") not in shown
    p = page_art.pick(cfg, datetime(2026, 11, 12, 20, tzinfo=timezone.utc))         # day 11 = day 0 again
    assert (p["banner"]["key"], p["footer"]["key"]) == ("b0", "f0")
    assert p["fx_script"] == "/assets/js/fx.js"


def test_off_by_default():
    cfg = make_cfg()
    assert cfg["page_art"]["enabled"] is False and page_art.pick(cfg) is None


def test_mobile_and_glow_fall_back():
    cfg = make_cfg(page_art=_art())
    f = cfg["page_art"]["footers"][0]
    assert f["image_mobile"] == f["image"] and f["glow_mobile"] == f["glow"]


@pytest.mark.parametrize("over, msg", [
    ({"surprise": 1}, "unknown setting"),
    ({"timezone": "Mars/Base"}, "timezone"),
    ({"start_date": "11/01/2026"}, "start_date"),
    ({"fx_script": "https://elsewhere.example/fx.js"}, "fx_script"),
    ({"banners": [{"key": "b0", "image": "/art/../etc/passwd"}]}, "path on this site"),
    ({"banners": [{"key": "B 0", "image": "/art/x.jpg"}]}, "key must be unique"),
    ({"banners": [{"key": "b0", "image": "/a.jpg"}, {"key": "b0", "image": "/b.jpg"}]}, "key must be unique"),
    ({"banners": [{"key": "b0", "image": "/a.jpg", "colour": "red"}]}, "may only have"),
    ({"footers": [{"key": "f0", "image": "/a.jpg", "effect": "lasers"}]}, "effect"),
    ({"footers": [{"key": "f0", "image": "/a.jpg", "caption": "x" * 81}]}, "caption"),
    ({"skip_pairs": [["b0", "nope"]]}, "skip_pairs"),
    ({"skip_pairs": ["b0"]}, "skip_pairs"),
    ({"banners": [], "skip_pairs": []}, "at least one banner"),
    ({"banners": [{"key": "b0", "image": "/a.jpg"}], "footers": [{"key": "f0", "image": "/b.jpg"}],
      "skip_pairs": [["b0", "f0"]]}, "no pairing"),
])
def test_bad_settings_are_rejected(over, msg):
    with pytest.raises(ConfigError, match=msg):
        make_cfg(page_art=_art(**over))


def test_sections_match_the_submenu():
    mp, cp = "/wow/quester", "/wow/chronicle"
    assert page_art.section("/wow/quester/", mp, cp) == "Quests"
    assert page_art.section("/wow/quester/events/abc", mp, cp) == "Quests"
    assert page_art.section("/wow/quester/characters", mp, cp) == "Characters"
    assert page_art.section("/wow/chronicle/", mp, cp) == "Chronicle"
    assert page_art.section("/wow/quester/help", mp, cp) == "Help"
    assert page_art.section("/wow/quester/settings", mp, cp) == ""


# ------------------------------------------------------------------ pages (DB)

def _client(seeded, cfg):
    return TestClient(create_app(ConfigHolder(cfg), seeded, None, None), base_url=BASE)


@needs_db
def test_pages_show_todays_banner_and_footer(seeded, tmp_path):
    cfg = make_cfg(tmp_path, page_art=_art())
    r = _client(seeded, cfg).get("/wow/quester/help")
    assert r.status_code == 200
    html = r.text
    p = page_art.pick(cfg)
    hero = html.split('<div class="wq-hero', 1)[1].split("</main>", 1)[0]
    assert f'data-art="{p["banner"]["key"]}"' in hero and p["banner"]["image"] in hero
    assert 'class="crumb"' in hero and "wq-hero-head" in hero and "<h1>How it works</h1>" in hero
    assert html.count("<h1") == 1 and "wq-hero-title" not in hero    # the page's heading, shown once
    assert html.count('class="crumb"') == 1                       # moved into the banner, not doubled
    assert 'class="wq-main wq-main-art"' in html or "wq-main-art" in html
    foot = html.split('<div class="wq-art', 1)[1]
    assert f'data-art="{p["footer"]["key"]}"' in foot and p["footer"]["image"] in foot
    assert 'class="fx-toggle"' in foot or "fx-toggle" in foot
    assert '<script src="/assets/js/fx.js" defer></script>' in html
    assert " style=" not in hero and " style=" not in foot.split("</footer>", 1)[0]   # CSP: no inline styles


@needs_db
def test_no_art_when_off_or_on_error_pages(seeded, tmp_path):
    plain = _client(seeded, make_cfg(tmp_path)).get("/wow/quester/help").text
    assert "wq-hero" not in plain and "wq-art" not in plain and "fx.js" not in plain
    assert 'class="crumb"' in plain
    r = _client(seeded, make_cfg(tmp_path, page_art=_art())).get("/wow/quester/no/such/page")
    assert r.status_code == 404 and "wq-hero" not in r.text


@needs_db
def test_still_pictures_without_an_effects_script(seeded, tmp_path):
    cfg = make_cfg(tmp_path, page_art=_art(fx_script=""))
    html = _client(seeded, cfg).get("/wow/quester/help").text
    assert "wq-art" in html and "fx.js" not in html and "data-fx" not in html


@needs_db
def test_banner_holds_each_pages_own_heading(seeded, tmp_path):
    """With page art on, the page's heading moves into the banner and isn't repeated below."""
    c = _client(seeded, make_cfg(tmp_path, page_art=_art()))
    for path, h1 in (("/wow/quester/", "Quests &amp; events"), ("/wow/chronicle/", "Chronicle"),
                     ("/wow/quester/privacy", "Privacy"), ("/wow/quester/terms", "Terms of use"),
                     ("/wow/quester/changelog", "Changelog")):
        r = c.get(path)
        assert r.status_code == 200, path
        hero, main = r.text.split('<main id="content"', 1)
        assert f"<h1>{h1}</h1>" in hero.split('<div class="wq-hero', 1)[1], path
        assert r.text.count("<h1") == 1 and "<h1" not in main, path
    plain = _client(seeded, make_cfg(tmp_path)).get("/wow/chronicle/").text
    assert "wq-hero" not in plain and plain.count("<h1>Chronicle</h1>") == 1     # art off: heading in the page
