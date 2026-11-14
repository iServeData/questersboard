"""Pages Nginx shows while QuestersBoard isn't running (502/503/504), written as static files."""
import re
from pathlib import Path

from questersboard.config import ConfigHolder
from questersboard.web.app import create_app, offline_path, write_offline_pages
from questersboard.web.error_pages import OFFLINE_CODES

from conftest import fake_id, make_cfg

ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path):
    cfg = make_cfg(tmp_path)
    out = tmp_path / "offline"
    names = write_offline_pages(create_app(ConfigHolder(cfg), None), out)
    return cfg, out, names


def test_offline_pages_are_static_and_self_contained(tmp_path):
    cfg, out, names = _write(tmp_path)
    assert set(OFFLINE_CODES) == {502, 503, 504}
    for want in ("502.html", "503.html", "504.html", "questersboard.css", "questersboard.js", "lock.svg"):
        assert want in names and (out / want).is_file()
    base = offline_path(cfg)
    assert base == "/wow/quester-offline"
    html = (out / "502.html").read_text()
    assert "isn&#39;t answering right now" in html or "isn't answering right now" in html
    assert "signups, characters and" in html and "Try again" in html
    assert f'href="{base}/questersboard.css?v=' in html and f'src="{base}/questersboard.js?v=' in html
    assert "/wow/quester/static/" not in html
    # Nothing that needs the app: no sign-in form, no account menu, no subnav, no app-only links.
    assert "/logout" not in html and "csrf" not in html.lower()
    assert 'class="wq-foot-links"' not in html and "qb-err-links" not in html
    assert "wq-subnav" not in html and "/changelog" not in html
    # CSP 'self' only: no inline styles or scripts, no data: URIs.
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and " style=" not in html and "data:" not in html
    for n in names:
        if n.endswith(".html"):
            assert not re.search(r"\b(webhook|outbox)\b", (out / n).read_text(), re.I)
    # Every url() in the CSS is shipped next to the pages.
    css = (out / "questersboard.css").read_text()
    for ref in re.findall(r"url\(([^)\"']+)\)", css):
        if not ref.startswith(("/", "http")):
            assert (out / ref.split("?")[0]).exists(), ref


def test_each_code_has_its_own_wording(tmp_path):
    _, out, _ = _write(tmp_path)
    texts = {c: (out / f"{c}.html").read_text() for c in OFFLINE_CODES}
    assert "Closed for maintenance" in texts[503] and "took too long" in texts[504]
    assert len(set(texts.values())) == 3


def test_live_error_pages_are_unchanged(tmp_path):
    from questersboard.web.error_pages import page
    assert page(502)["offline"] is False and page(502, offline=True)["notice"] == "Board's closed"
    assert page(404, offline=True)["heading"] == page(404)["heading"]   # only 502/503/504 have offline text


def test_cli_writes_pages_without_a_database(tmp_path, monkeypatch, capsys):
    from questersboard import cli
    t = (ROOT / "deploy" / "config.example.toml").read_text()
    t = t.replace('guild_id = ""', f'guild_id = "{fake_id(1)}"', 1).replace('application_id = ""', f'application_id = "{fake_id(2)}"', 1)
    p = tmp_path / "config.toml"
    p.write_text(t)
    for role in ("ADMIN", "APP", "WORKER"):
        monkeypatch.delenv(f"QUESTERSBOARD_SECRET_DB_{role}_PASSWORD", raising=False)
    out = tmp_path / "o"
    assert cli.main(["--config", str(p), "offline-pages", "--out", str(out)]) == 0
    assert (out / "503.html").is_file() and (out / "503.html").stat().st_mode & 0o777 == 0o644
    assert "served at /wow/quester-offline/" in capsys.readouterr().out


def test_nginx_snippet_points_at_the_offline_pages():
    conf = (ROOT / "deploy" / "nginx" / "questersboard_locations.conf").read_text()
    for code in OFFLINE_CODES:
        assert conf.count(f"error_page {code} /wow/quester-offline/{code}.html;") == 2
    assert "location ^~ /wow/quester-offline/ {" in conf and "alias /opt/questersboard/offline/;" in conf
    assert "proxy_intercept_errors on" not in conf and "add_header" not in conf


def test_install_writes_offline_pages():
    sh = (ROOT / "deploy" / "install.sh").read_text()
    assert "offline-pages" in sh and '"$BASE/offline"' in sh
    assert (ROOT / "deploy" / "enable_offline_pages.sh").stat().st_mode & 0o111
