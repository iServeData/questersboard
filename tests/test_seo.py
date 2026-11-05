"""Search and link-preview tags on public pages, and the Chronicle sitemap. Discord is a FIXTURE."""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET

import pytest
from conftest import actor, at, make_cfg, needs_db
from fastapi.testclient import TestClient
from helpers import char, event
from PIL import Image
from test_web import FakeOAuth, FakeRest, csrf, login

from wowquester.config import ConfigError, ConfigHolder
from wowquester.domain import chronicle, events, signups
from wowquester.models import ChronicleEntry
from wowquester.web.app import create_app

pytestmark = needs_db
BASE = "https://community.example.org"
DEFAULT_IMG = {"social_image": "/assets/images/social/og_example.jpg", "social_image_alt": "Example key art",
               "social_image_width": 1200, "social_image_height": 630}


def _app(seeded, cfg):
    rest = FakeRest()
    a = create_app(ConfigHolder(cfg), seeded, FakeOAuth(), rest)
    a.state.fake_rest = rest
    return a


def _tag(html, attr, name):
    m = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', html)
    return m.group(1) if m else None


def _canonical(html):
    m = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    return m.group(1) if m else None


def _png():
    b = io.BytesIO()
    Image.new("RGB", (80, 50), (30, 90, 30)).save(b, "PNG")
    return b.getvalue()


def _publish(app, s_factory, cfg, monkeypatch, with_shot):
    org = actor(60, "event_creator")
    with s_factory.tx() as s:
        ev = event(s, cfg, org, 0, 2, title="Deadmines")
        p = actor(61)
        signups.join(s, cfg, p, ev.id, char(s, cfg, p, "Grub").id, recognition=False)
        monkeypatch.setattr(events, "now", lambda: at(0))
        events.start(s, cfg, org, ev.id)
        events.end(s, cfg, org, ev.id)
        e = s.query(ChronicleEntry).filter_by(event_id=ev.id).one()
        chronicle.save(s, cfg, org, e.id, {"title": "Into the Mines", "attempted": "Clear the Deadmines",
                                           "happened": "Our tank pulled the whole cave and we lived.",
                                           "outcome": "completed"})
        if with_shot:
            up = chronicle.add_upload(s, cfg, org, e.id, _png(), "the crew at the ship")
            chronicle.clear_upload(s, cfg, actor(62, "chronicle_reviewer"), up.id)
        chronicle.submit(s, cfg, org, e.id)
        eid, slug = e.id, e.slug
    rev = login(app, 62, "chronicle_reviewer")
    r = rev.post(f"/wow/quester/chronicle/{eid}/review",
                 data={"csrf": csrf(rev, "/wow/quester/review"), "decision": "approve"})
    assert r.status_code == 303
    return slug


def test_story_and_archive_preview_tags_and_sitemap(seeded, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, web={"cookie_secure": False}, chronicle=DEFAULT_IMG)
    app = _app(seeded, cfg)
    slug = _publish(app, seeded, cfg, monkeypatch, with_shot=True)
    pub = TestClient(app, base_url=BASE)

    story = pub.get(f"/wow/chronicle/{slug}/").text
    url = f"{BASE}/wow/chronicle/{slug}/"
    assert _canonical(story) == url and _tag(story, "property", "og:url") == url
    assert _tag(story, "property", "og:type") == "article"
    assert _tag(story, "property", "og:title").startswith("Into the Mines")
    desc = _tag(story, "name", "description")
    assert desc and len(desc) <= 200 and "Grub" not in story          # opted-out player still unnamed
    assert _tag(story, "property", "og:description") == desc
    img = _tag(story, "property", "og:image")                         # the approved screenshot, absolute
    assert re.fullmatch(rf"{BASE}/wow/chronicle/media/[0-9a-f-]{{36}}\.webp", img)
    assert pub.get(img.removeprefix(BASE)).status_code == 200
    assert _tag(story, "name", "twitter:card") == "summary_large_image"
    assert _tag(story, "property", "article:published_time")
    assert "index,follow" in story

    arch = pub.get("/wow/chronicle/").text
    assert _canonical(arch) == f"{BASE}/wow/chronicle/"
    assert _tag(arch, "name", "description") == cfg["chronicle"]["description"]
    assert _tag(arch, "property", "og:image") == BASE + DEFAULT_IMG["social_image"]
    assert _tag(arch, "property", "og:image:width") == "1200"
    assert _tag(arch, "property", "og:image:alt") == "Example key art"
    assert _canonical(pub.get("/wow/chronicle/?page=2&utm=x").text) == f"{BASE}/wow/chronicle/?page=2"

    r = pub.get("/wow/chronicle/sitemap.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [u.findtext("s:loc", namespaces=ns) for u in ET.fromstring(r.text).findall("s:url", ns)]
    assert locs == [f"{BASE}/wow/chronicle/", url]
    assert all(u.findtext("s:lastmod", namespaces=ns) for u in ET.fromstring(r.text).findall("s:url", ns))


def test_story_without_screenshot_uses_default_image_and_sitemap_switch(seeded, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, web={"cookie_secure": False}, chronicle={**DEFAULT_IMG, "sitemap": False})
    app = _app(seeded, cfg)
    slug = _publish(app, seeded, cfg, monkeypatch, with_shot=False)
    pub = TestClient(app, base_url=BASE)
    story = pub.get(f"/wow/chronicle/{slug}/").text
    img = _tag(story, "property", "og:image")
    assert img and img.startswith(BASE)                               # event cover art or the configured default
    assert pub.get("/wow/chronicle/sitemap.xml").status_code == 404


def test_no_image_configured_gives_summary_card(seeded, tmp_path):
    cfg = make_cfg(tmp_path, web={"cookie_secure": False})
    pub = TestClient(_app(seeded, cfg), base_url=BASE)
    arch = pub.get("/wow/chronicle/").text
    assert _tag(arch, "property", "og:image") is None and _tag(arch, "name", "twitter:card") == "summary"
    assert pub.get("/wow/chronicle/sitemap.xml").text.count("<url>") == 1   # archive only, no stories yet


def test_privacy_terms_have_own_descriptions_and_error_pages_stay_out_of_search(seeded, tmp_path):
    cfg = make_cfg(tmp_path, web={"cookie_secure": False})
    pub = TestClient(_app(seeded, cfg), base_url=BASE, follow_redirects=False)
    for path in ("/wow/quester/privacy", "/wow/quester/terms"):
        h = pub.get(path).text
        assert _canonical(h) == BASE + path
        assert _tag(h, "name", "description") != cfg["chronicle"]["description"]
    missing = pub.get("/wow/chronicle/no-such-story/")
    assert missing.status_code == 404
    assert "noindex,nofollow" in missing.text and _canonical(missing.text) is None
    home = pub.get("/wow/quester/")                                    # member area stays out of search
    assert home.status_code == 200 and "noindex,nofollow" in home.text and _canonical(home.text) is None


@pytest.mark.parametrize("bad", [{"social_image": "http://x.example/a.jpg"}, {"description": ""},
                                 {"social_image_width": -1}, {"sitemap": "yes"}])
def test_bad_preview_settings_rejected(tmp_path, bad):
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, chronicle=bad)
