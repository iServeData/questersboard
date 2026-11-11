"""Footer (wordmark, notes, credits, build stamp) and the members' changelog page."""
from __future__ import annotations

from datetime import date

import pytest
from conftest import make_cfg
from fastapi.testclient import TestClient

from questersboard import build
from questersboard.config import ConfigError, ConfigHolder
from questersboard.web import changelog
from questersboard.web.app import create_app


def test_build_stamp_parsing():
    assert build.parse("d5e2f1a 2026-11-11\n") == {"commit": "d5e2f1a", "date": date(2026, 11, 11)}
    for bad in ("", "local", "not a commit", "<script> 2026-11-11"):
        assert build.parse(bad) == {"commit": "local", "date": None}
    assert build.parse("d5e2f1a 2026-13-40") == {"commit": "d5e2f1a", "date": None}


def test_shipped_changelog_is_valid_and_sorted():
    rs = changelog.releases()
    assert rs and [r["date"] for r in rs] == sorted((r["date"] for r in rs), reverse=True)
    assert rs[-1]["since"] is None and all(r["since"] == rs[i + 1]["date"] for i, r in enumerate(rs[:-1]))
    assert all(r["changes"] and r["areas"] for r in rs)


@pytest.mark.parametrize("text", [
    '[[release]]\ndate = 2026-11-01\ntitle = "x"\n',
    '[[release]]\ndate = 2026-11-01\n[[release.change]]\narea = "Nope"\nkind = "new"\ntext = "x"\n',
    '[[release]]\ndate = 2026-11-01\n[[release.change]]\narea = "Site"\nkind = "maybe"\ntext = "x"\n',
])
def test_changelog_rejects_bad_entries(text):
    with pytest.raises(ValueError):
        changelog.parse(text)


def test_footer_config_checks():
    c = make_cfg(branding={"wordmark": ["Fixture "], "footer_notes": ["Plain line"],
                           "footer_credits": [{"text": "By ", "link": "Fixture Host", "url": "https://short.example.org/",
                                               "after": "."}]})
    b = c["branding"]
    assert b["wordmark"] == ["", "Fixture ", ""] and b["footer_notes"][0]["text"] == "Plain line"
    for bad in ({"wordmark": []}, {"wordmark": ["a", "b", "c", "d"]},
                {"footer_notes": [{"text": "x", "link": "y"}]},
                {"footer_credits": [{"text": "x", "link": "y", "url": "javascript:alert(1)"}]},
                {"footer_notes": [{"text": "x", "colour": "red"}]},
                {"footer_nav": [{"label": "x", "url": "http://insecure.example.org"}]}):
        with pytest.raises(ConfigError):
            make_cfg(branding=bad)


def test_footer_and_changelog_page(seeded):
    from test_web import FakeOAuth, FakeRest
    app = create_app(ConfigHolder(make_cfg()), seeded, FakeOAuth(), FakeRest())
    c = TestClient(app, base_url="https://community.example.org")
    r = c.get("/wow/quester/help")
    assert r.status_code == 200
    for bit in ('<a class="wordmark" href="/">Example <b>Community</b></a>', "not affiliated with or endorsed",
                'href="https://community.example.org/about/" target="_blank" rel="noopener">Example Hosting',
                'class="build">Commit ', 'href="/wow/quester/changelog">Changelog</a>', 'aria-label="Footer"'):
        assert bit in r.text, bit
    r = c.get("/wow/quester/changelog")
    assert r.status_code == 200 and r.text.count('class="panel wq-release"') == len(changelog.releases())
    first = changelog.releases()[0]
    assert f'id="{first["id"]}" open' in r.text and f'{len(first["changes"])} changes' in r.text
    assert " style=" not in r.text
