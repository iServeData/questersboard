"""Web routes via TestClient. Discord OAuth and guild lookups are FIXTURES (no live Discord)."""
from __future__ import annotations

import io
import re

import pytest
from conftest import ROLE, actor, at, fake_id, make_cfg, needs_db
from fastapi.testclient import TestClient
from helpers import char, event
from PIL import Image
from sqlalchemy import text

from wowquester.config import ConfigHolder
from wowquester.domain import chronicle, events, signups
from wowquester.models import ChronicleEntry
from wowquester.web.app import create_app

pytestmark = needs_db


class FakeOAuth:
    """FIXTURE: pretends Discord identified the user named in the code."""
    def authorize_url(self, redirect_uri, state):
        return f"https://discord.example.invalid/authorize?state={state}"

    def identify(self, code, redirect_uri):
        return code, f"User{code[-3:]}"


class FakeRest:
    """FIXTURE: guild membership and roles per Discord ID."""
    def __init__(self):
        self.roles: dict[str, set[str]] = {}
        self.calls = 0

    def member(self, guild_id, user_id):
        self.calls += 1
        if user_id not in self.roles:
            return False, frozenset(), ""
        return True, frozenset(self.roles[user_id]), f"Nick{user_id[-3:]}"


@pytest.fixture
def wcfg(tmp_path):
    return make_cfg(tmp_path, web={"cookie_secure": False})


@pytest.fixture
def app(seeded, wcfg):
    rest = FakeRest()
    a = create_app(ConfigHolder(wcfg), seeded, FakeOAuth(), rest)
    a.state.fake_rest = rest
    return a


def login(app, n, *caps):
    c = TestClient(app, base_url="https://community.example.org", follow_redirects=False)
    app.state.fake_rest.roles[fake_id(n)] = {ROLE[x] for x in caps}
    r = c.get("/wow/quester/login")
    assert r.status_code == 303
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    r = c.get(f"/wow/quester/oauth/callback?code={fake_id(n)}&state={state}")
    assert r.status_code == 303, r.text
    return c


def csrf(c, path="/wow/quester/me"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)


def test_login_state_is_single_use_and_bad_state_rejected(app):
    c = TestClient(app, base_url="https://community.example.org", follow_redirects=False)
    r = c.get("/wow/quester/login")
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    assert c.get(f"/wow/quester/oauth/callback?code={fake_id(5)}&state=wrong").status_code == 403
    c2 = TestClient(app, base_url="https://community.example.org", follow_redirects=False)
    assert c2.get(f"/wow/quester/oauth/callback?code={fake_id(5)}&state={state}").status_code == 403


def test_non_guild_member_cannot_log_in(app):
    c = TestClient(app, base_url="https://community.example.org", follow_redirects=False)
    r = c.get("/wow/quester/login")
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    r = c.get(f"/wow/quester/oauth/callback?code={fake_id(77)}&state={state}")   # not in FakeRest.roles
    assert r.status_code == 403


def test_session_cookie_flags_and_hash_only(app, seeded, tmp_path):
    cfg = make_cfg(tmp_path)          # cookie_secure = true (production default)
    a = create_app(ConfigHolder(cfg), seeded, FakeOAuth(), app.state.fake_rest)
    c = TestClient(a, base_url="https://community.example.org", follow_redirects=False)
    r = c.get("/wow/quester/login")
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "secure" in sc and "samesite=lax" in sc
    raw = r.cookies.get("wq_session") or re.search(r"wq_session=([^;]+)", r.headers["set-cookie"]).group(1)
    with seeded.tx() as s:
        ids = [x[0] for x in s.execute(text("select id_hash from web_sessions"))]
    assert raw not in ids and len(ids[0]) == 64


def test_csrf_required_on_posts(app):
    c = login(app, 10)
    r = c.post("/wow/quester/me", data={"recognition_default": "1"})
    assert r.status_code == 403
    r = c.post("/wow/quester/me", data={"recognition_default": "1", "csrf": csrf(c)},
               headers={"origin": "https://evil.example.net"})
    assert r.status_code == 403
    r = c.post("/wow/quester/me", data={"recognition_default": "1", "csrf": csrf(c)})
    assert r.status_code == 303


def test_character_realm_rule_via_web(app, seeded):
    c = login(app, 11)
    tok = csrf(c)
    base = {"csrf": tok, "name": "Grom", "faction": "horde", "race_key": "orc", "class_key": "warrior"}
    r = c.post("/wow/quester/characters", data={**base, "edition": "forever", "ruleset": "pvp",
                                                "realm_slug": "some-realm"})
    assert r.status_code == 422 and "no realms" in r.text
    r = c.post("/wow/quester/characters", data={**base, "edition": "forever", "ruleset": "pvp"})
    assert r.status_code == 303
    r = c.post("/wow/quester/characters", data={**base, "edition": "retail", "name": "Thrall"})
    assert r.status_code == 422 and "need a realm" in r.text


def test_create_publish_join_and_upcoming_json(app, seeded):
    org = login(app, 20, "event_creator")
    tok = csrf(org)
    start = at(4).astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    r = org.post("/wow/quester/events/new", data={
        "csrf": tok, "edition": "forever", "ruleset": "pvp", "faction": "horde", "title": "Ragefire run",
        "activity_type": "dungeon", "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"),
        "duration_minutes": "90", "timezone": "America/Los_Angeles", "signup_policy": "open",
        "role_key_0": "tank", "role_qty_0": "1", "role_key_1": "healer", "role_qty_1": "1",
        "public_listing": "1", "chronicle_enabled": "1", "publish": "1", "location": "Ragefire Chasm"})
    assert r.status_code == 303, r.text
    url = r.headers["location"]
    pub = TestClient(app, base_url="https://community.example.org")
    j = pub.get("/wow/quester/upcoming.json").json()
    assert j["items"][0]["title"] == "Ragefire run" and j["items"][0]["roles"][0]["filled"] == 0
    assert "creator" not in str(j) and fake_id(20) not in str(j)
    p = login(app, 21)
    t2 = csrf(p)
    p.post("/wow/quester/characters", data={"csrf": t2, "edition": "forever", "ruleset": "pvp", "name": "Brakka",
                                            "faction": "horde", "race_key": "orc", "class_key": "warrior",
                                            "eligible_roles": "tank"})
    page = p.get(url).text
    cid = re.search(r'name="character_id"><option value="(\d+)"', page).group(1)
    r = p.post(url + "/join", data={"csrf": t2, "character_id": cid, "role_key": "tank"})
    assert r.status_code == 303
    assert pub.get("/wow/quester/upcoming.json").json()["items"][0]["roles"][0]["filled"] == 1
    ics = pub.get("/wow/quester/calendar.ics").text
    assert "BEGIN:VEVENT" in ics and "Ragefire run" in ics
    # a non-manager can't start it
    assert p.post(url + "/start", data={"csrf": t2}).status_code == 403
    # per-event consent: default "name me" is pre-checked, and unticking it (hidden 0 only) opts out
    q = login(app, 23)
    t3 = csrf(q)
    q.post("/wow/quester/me", data={"csrf": t3, "recognition_default": "1", "timezone": ""})
    q.post("/wow/quester/characters", data={"csrf": t3, "edition": "forever", "ruleset": "pvp", "name": "Zuljin",
                                            "faction": "horde", "race_key": "orc", "class_key": "warrior",
                                            "eligible_roles": "healer"})
    page = q.get(url).text
    assert re.search(r'name="recognition" value="1" checked', page)
    cid = re.search(r'name="character_id"><option value="(\d+)"', page).group(1)
    assert q.post(url + "/join", data={"csrf": t3, "character_id": cid, "role_key": "healer",
                                       "recognition": "0"}).status_code == 303
    with seeded.tx() as s:
        assert s.execute(text("select recognition from signups su join characters c on c.id = su.character_id "
                              "where c.name = 'Zuljin'")).scalar_one() is False


def test_exclusive_checkbox_needs_role(app, seeded):
    org = login(app, 22, "event_creator")
    form = org.get("/wow/quester/events/new").text
    assert 'name="exclusive"' not in form
    org2 = login(app, 23, "event_creator", "exclusive_designator")
    assert 'name="exclusive"' in org2.get("/wow/quester/events/new").text


def test_removed_role_stops_privileged_action(app, seeded, wcfg, monkeypatch):
    import wowquester.web.app as wa
    org = login(app, 24, "event_creator")
    tok = csrf(org)
    app.state.fake_rest.roles[fake_id(24)] = set()          # role removed in Discord
    monkeypatch.setattr(wa, "ROLE_TTL", -1)
    start = at(6)
    r = org.post("/wow/quester/events/new", data={
        "csrf": tok, "edition": "forever", "ruleset": "pvp", "title": "Late night", "activity_type": "quest",
        "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"), "timezone": "UTC",
        "duration_minutes": "60", "publish": "1"})
    assert r.status_code == 403


def test_personal_calendar_token_and_revocation(app, seeded):
    c = login(app, 30)
    tok = csrf(c)
    c.post("/wow/quester/me/calendar", data={"csrf": tok})
    link = re.search(r'/wow/quester/calendar/([A-Za-z0-9_-]+)\.ics', c.get("/wow/quester/me").text).group(1)
    pub = TestClient(app, base_url="https://community.example.org")
    assert pub.get(f"/wow/quester/calendar/{link}.ics").status_code == 200
    c.post("/wow/quester/me/calendar", data={"csrf": tok, "revoke": "1"})
    assert pub.get(f"/wow/quester/calendar/{link}.ics").status_code == 404


def _png():
    b = io.BytesIO()
    Image.new("RGB", (64, 40), (120, 30, 30)).save(b, "PNG")
    return b.getvalue()


def test_chronicle_public_only_after_approval_and_media_gate(app, seeded, wcfg, monkeypatch):
    org = actor(40, "event_creator")
    with seeded.tx() as s:
        ev = event(s, wcfg, org, 0, 2, title="Wailing Caverns")
        p = actor(41)
        signups.join(s, wcfg, p, ev.id, char(s, wcfg, p, "Zugzug").id, recognition=False)
        monkeypatch.setattr(events, "now", lambda: at(0))
        events.start(s, wcfg, org, ev.id)
        events.end(s, wcfg, org, ev.id)
        e = s.query(ChronicleEntry).filter_by(event_id=ev.id).one()
        chronicle.save(s, wcfg, org, e.id, {"title": "Into the Caverns", "attempted": "Clear WC",
                                             "happened": "Zugzug tanked the whole thing", "outcome": "completed"})
        up = chronicle.add_upload(s, wcfg, org, e.id, _png(), "the crew")
        chronicle.clear_upload(s, wcfg, actor(42, "chronicle_reviewer"), up.id)
        eid = e.id
    # a signed-up player with unknown attendance who opted out may not be named either
    with pytest.raises(Exception, match="hasn't agreed to be named"):
        with seeded.tx() as s:
            chronicle.submit(s, wcfg, org, eid)
    with seeded.tx() as s:
        e = s.get(ChronicleEntry, eid)
        chronicle.save(s, wcfg, org, e.id, {"happened": "Our tank held the whole thing"})
        chronicle.submit(s, wcfg, org, e.id)
        eid, upid, slug = e.id, str(up.public_id), e.slug
    pub = TestClient(app, base_url="https://community.example.org")
    assert pub.get(f"/wow/chronicle/{slug}/").status_code == 404
    assert pub.get(f"/wow/chronicle/media/{upid}.webp").status_code == 404
    rev = login(app, 42, "chronicle_reviewer")
    r = rev.post(f"/wow/quester/chronicle/{eid}/review", data={"csrf": csrf(rev, "/wow/quester/review"),
                                                                "decision": "approve"})
    assert r.status_code == 303
    page = pub.get(f"/wow/chronicle/{slug}/").text
    assert "Into the Caverns" in page and "Zugzug" not in page and fake_id(41) not in page
    assert pub.get(f"/wow/chronicle/media/{upid}.webp").status_code == 200
    feed = pub.get("/wow/chronicle/feed.json").json()
    assert feed["items"][0]["title"] == "Into the Caverns" and "Zugzug" not in str(feed)
    assert "<entry>" in pub.get("/wow/chronicle/feed.atom").text
    assert "<item>" in pub.get("/wow/chronicle/feed.xml").text
    assert pub.get("/wow/chronicle/latest.json").json()["items"][0]["title"] == "Into the Caverns"
    assert "Into the Caverns" in pub.get("/wow/chronicle/").text


def test_security_headers_and_health(app):
    c = TestClient(app, base_url="https://community.example.org")
    r = c.get("/healthz")
    assert r.json() == {"status": "ok"}
    h = c.get("/wow/chronicle/").headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"


def test_rate_limit(app, seeded, wcfg):
    app.state.web.holder.cfg.raw["rate_limits"]["web_requests_per_minute"] = 5
    c = TestClient(app, base_url="https://community.example.org")
    codes = [c.get("/wow/chronicle/").status_code for _ in range(8)]
    assert 429 in codes


# ---------------------------------------------------------------- Blizzard (FIXTURE transport, no live API)
def _bz_app(seeded, tmp_path, monkeypatch, handler):
    import httpx
    from cryptography.fernet import Fernet

    from wowquester.integrations.blizzard import Blizzard
    monkeypatch.setenv("WOWQUESTER_SECRET_BLIZZARD_CLIENT_ID", "FIXTURE-id")
    monkeypatch.setenv("WOWQUESTER_SECRET_BLIZZARD_CLIENT_SECRET", "FIXTURE-secret")
    monkeypatch.setenv("WOWQUESTER_SECRET_FIELD_KEY", Fernet.generate_key().decode())
    cfg = make_cfg(tmp_path, web={"cookie_secure": False},
                   blizzard={"enabled": True, "battlenet_link_enabled": True})
    calls = []

    def h(req):
        calls.append(req)
        if req.url.path == "/token" and b"authorization_code" not in req.content:
            return httpx.Response(200, json={"access_token": "FIXTURE-app", "expires_in": 3600})
        return handler(req)
    bz = Blizzard(cfg, http=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda _s: None)
    rest = FakeRest()
    a = create_app(ConfigHolder(cfg), seeded, FakeOAuth(), rest, blizzard=bz)
    a.state.fake_rest = rest
    return a, cfg, calls


def _retail(seeded, cfg, n, name="Ragna"):
    with seeded.tx() as s:
        c = char(s, cfg, actor(n), name, edition="retail", realm_slug="area-52", ruleset=None)
        return c.id


def test_check_with_blizzard_retail_is_existence_only(seeded, tmp_path, monkeypatch):
    import httpx
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch,
                            lambda r: httpx.Response(200, json={"id": 77, "level": 80}))
    cid = _retail(seeded, cfg, 30)
    c = login(a, 30)
    page = c.get("/wow/quester/characters").text
    assert "Check with Blizzard" in page and "Prove a character is yours" in page
    r = c.post(f"/wow/quester/characters/{cid}/lookup", data={"csrf": csrf(c)})
    assert r.status_code == 303
    with seeded.tx() as s:
        v = s.execute(text("select verification, level from characters where id=:i"), {"i": cid}).one()
    assert v == ("api_matched", 80)                       # matched, NOT ownership_verified
    assert any(q.url.path == "/profile/wow/character/area-52/ragna" for q in calls)
    # someone else can't trigger a lookup on this character
    o = login(a, 31)
    assert c.post(f"/wow/quester/characters/{cid}/lookup", data={"csrf": csrf(c)}).status_code == 303
    assert o.post(f"/wow/quester/characters/{cid}/lookup", data={"csrf": csrf(o)}).status_code == 404


def test_check_with_blizzard_forever_is_refused_without_requests(seeded, tmp_path, monkeypatch):
    import httpx
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, lambda r: httpx.Response(200, json={"id": 1}))
    with seeded.tx() as s:
        cid = char(s, cfg, actor(32), "Grom").id
    c = login(a, 32)
    assert "Check with Blizzard" not in c.get("/wow/quester/characters").text
    r = c.post(f"/wow/quester/characters/{cid}/lookup", data={"csrf": csrf(c)})
    assert r.status_code == 422 and "Forever" in r.text
    assert calls == []                                    # no Retail fallback, no request at all


def test_battlenet_link_state_single_use_and_marks_owned(seeded, tmp_path, monkeypatch):
    import httpx

    def h(req):
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "FIXTURE-user", "expires_in": 86399})
        if req.url.path == "/profile/user/wow":
            assert req.headers["authorization"] == "Bearer FIXTURE-user"
            return httpx.Response(200, json={"wow_accounts": [{"characters": [
                {"id": 9001, "name": "Ragna", "realm": {"slug": "area-52"}}]}]})
        return httpx.Response(404)
    a, cfg, calls = _bz_app(seeded, tmp_path, monkeypatch, h)
    cid = _retail(seeded, cfg, 33)
    other = _retail(seeded, cfg, 34, name="Ragna")       # same name, other member: must stay unverified
    c = login(a, 33)
    r = c.get("/wow/quester/characters/battlenet?edition=retail&region=us")
    assert r.status_code == 303 and "scope=wow.profile" in r.headers["location"]
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    assert c.get("/wow/quester/battlenet/callback?code=x&state=wrong").status_code == 422
    # the failed attempt consumed the state: the real one no longer works either
    assert c.get(f"/wow/quester/battlenet/callback?code=x&state={state}").status_code == 422
    r = c.get("/wow/quester/characters/battlenet?edition=retail&region=us")
    state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
    r = c.get(f"/wow/quester/battlenet/callback?code=FIXTURE-code&state={state}")
    assert r.status_code == 200 and "1 of your characters" in r.text
    with seeded.tx() as s:
        got = dict(s.execute(text("select id, verification from characters where id in (:a,:b)"),
                             {"a": cid, "b": other}).all())
        snap = s.execute(text("select snapshot_enc from battlenet_links")).scalar_one()
    assert got == {cid: "ownership_verified", other: "self_reported"}
    assert b"Ragna" not in bytes(snap) and b"9001" not in bytes(snap)     # encrypted at rest
    assert "Forget my Battle.net link" in c.get("/wow/quester/characters").text
    assert c.post("/wow/quester/characters/battlenet/unlink", data={"csrf": csrf(c)}).status_code == 303
    with seeded.tx() as s:
        assert s.execute(text("select count(*) from battlenet_links")).scalar_one() == 0
        assert s.execute(text("select verification from characters where id=:i"), {"i": cid}).scalar_one() \
            == "self_reported"
    # Forever has no ownership API: refused before any redirect
    assert c.get("/wow/quester/characters/battlenet?edition=forever&region=us").status_code == 422


def test_autocomplete_options_and_free_text(app, seeded):
    c = login(app, 40)
    r = c.get("/wow/quester/api/options?edition=forever&kind=race&q=orc").json()
    assert any(i["key"] == "orc" for i in r["items"]) and r["free_text"] is True
    r = c.get("/wow/quester/api/options?edition=forever&kind=quest&q=x").json()
    assert r["api_status"] == "unsupported" and r["free_text"] is True    # gaps never block planning
    assert c.get("/wow/quester/api/options?edition=forever&kind=password").status_code == 422
    anon = TestClient(app, base_url="https://community.example.org", follow_redirects=False)
    assert anon.get("/wow/quester/api/options?edition=forever&kind=race").status_code in (303, 401)


def test_privacy_and_terms_render_public_from_config(app, wcfg):
    c = TestClient(app, base_url="https://community.example.org", follow_redirects=False)
    for path, heading in (("/wow/quester/privacy", "Privacy"), ("/wow/quester/terms", "Terms of use")):
        r = c.get(path)
        assert r.status_code == 200, path
        assert f"<h1>{heading}</h1>" in r.text
        assert "index,follow" in r.text and "Log in with Discord" not in r.text
        assert wcfg["legal"]["contact"] in r.text
        assert "Example Community" in r.text
    p = c.get("/wow/quester/privacy").text
    assert "<code>identify</code>" in p
    assert f"{wcfg['retention']['audit_days']} days" in p
    # optional integrations are only described when switched on
    assert ("Battle.net link" in p) == bool(wcfg["blizzard"]["enabled"] and wcfg["blizzard"]["battlenet_link_enabled"])
    assert ("AI service" in p) == bool(wcfg["ai"]["enabled"])
    assert 'href="/wow/quester/privacy"' in c.get("/wow/chronicle/").text


def test_privacy_and_terms_have_a_table_of_contents_with_remove_me(app):
    import re as _re
    c = TestClient(app, base_url="https://community.example.org", follow_redirects=False)
    for path in ("/wow/quester/privacy", "/wow/quester/terms"):
        html = c.get(path).text
        toc = html.split('<nav class="wq-toc panel" aria-label="On this page" data-toc>', 1)[1].split("</nav>", 1)[0]
        targets = _re.findall(r'href="#([a-z-]+)"', toc)
        assert len(targets) >= 7, path
        for t in targets:                                    # every entry lands on a real section
            assert f'id="{t}"' in html, (path, t)
    p = c.get("/wow/quester/privacy").text
    assert '<a href="#choices">Your choices</a>' in p and '<a href="#remove-me">Remove me</a>' in p


def test_legal_config_validation(tmp_path):
    from wowquester.config import ConfigError
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, legal={"effective_date": "Nov 4"})
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, legal={"contact": "  "})


def test_every_post_form_carries_the_session_csrf_token(app, seeded, wcfg):
    """Regression: the csrf_field macro must see the page context, so no form posts an empty token."""
    c = login(app, 77, "event_creator", "admin")
    with seeded.tx() as s:
        org = actor(77, "event_creator")
        ev = event(s, wcfg, org, 0, 2, title="Ragefire")
        pid = ev.public_id
    tok = csrf(c)
    for path in ("/wow/quester/me", "/wow/quester/characters", "/wow/quester/events/new", f"/wow/quester/events/{pid}"):
        html = c.get(path).text
        forms = re.findall(r'<form[^>]*method="post"[^>]*>.*?</form>', html, re.S)
        assert forms, path
        for f in forms:
            assert f'name="csrf" value="{tok}"' in f, (path, f[:120])
    r = c.post("/wow/quester/characters", data={"csrf": tok, "edition": "forever", "ruleset": "pvp", "name": "Grom",
                                                 "faction": "horde", "race_key": "orc", "class_key": "warrior"})
    assert r.status_code == 303


def _form_defaults(html: str, action: str) -> dict:
    """Collect what a browser would submit for the form posting to `action` (defaults only)."""
    f = re.search(r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', html, re.S).group(1)
    data: dict = {}
    for m in re.finditer(r"<input([^>]*)>", f):
        a = m.group(1)
        n = re.search(r'name="([^"]+)"', a)
        if not n or " disabled" in a:
            continue
        t = re.search(r'type="([^"]+)"', a)
        v = re.search(r'value="([^"]*)"', a)
        if t and t.group(1) in ("checkbox", "radio"):
            if "checked" in a:
                data[n.group(1)] = v.group(1) if v else "on"
        else:
            data.setdefault(n.group(1), v.group(1) if v else "")
    for m in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', f, re.S):
        o = re.search(r'<option[^>]*value="([^"]*)"[^>]*selected', m.group(2)) or re.search(r'value="([^"]*)"', m.group(2))
        data.setdefault(m.group(1), o.group(1) if o else "")
    return data


def test_host_flow_through_the_browser_forms(app):
    """Regression: a freshly created draft must render for its host (preview_conflicts call)."""
    host = login(app, 91, "event_creator")
    new = host.get("/wow/quester/events/new").text
    data = {**_form_defaults(new, "/wow/quester/events/new"),
            "title": "Test run", "location": "Test", "edition": "forever", "date": "2026-11-10",
            "start_time": "19:00", "timezone": "America/Los_Angeles"}
    r = host.post("/wow/quester/events/new", data=data)
    assert r.status_code == 303, r.text[:300]
    page = r.headers["location"]
    draft = host.get(page)
    assert draft.status_code == 200 and "Test run" in draft.text
    assert host.get(page + "/edit").status_code == 200
    r = host.post(page + "/publish", data={"csrf": csrf(host)})
    assert r.status_code == 303, r.text[:300]
    assert host.get(page).status_code == 200

    member = login(app, 92)
    tok = csrf(member)
    assert member.post("/wow/quester/characters", data={"csrf": tok, "edition": "forever", "ruleset": "pvp",
                                                         "name": "Grom", "faction": "horde", "race_key": "orc",
                                                         "class_key": "warrior"}).status_code == 303
    ev_page = member.get(page)
    assert ev_page.status_code == 200
    join = _form_defaults(ev_page.text, page + "/join")
    r = member.post(page + "/join", data=join)
    assert r.status_code == 303, r.text[:300]
    assert member.get(page).status_code == 200 and host.get(page).status_code == 200


def test_host_joins_own_event_and_wrong_characters_are_explained(app):
    host = login(app, 93, "event_creator")
    tok = csrf(host)
    new = host.get("/wow/quester/events/new").text
    data = {**_form_defaults(new, "/wow/quester/events/new"), "title": "Own run", "location": "Test",
            "edition": "forever", "faction": "horde", "date": "2026-11-12", "start_time": "19:00",
            "timezone": "America/Los_Angeles"}
    page = host.post("/wow/quester/events/new", data=data).headers["location"]
    assert host.post(page + "/publish", data={"csrf": tok}).status_code == 303
    # an Alliance character must not be offered for a Horde event, and the page says why
    host.post("/wow/quester/characters", data={"csrf": tok, "edition": "forever", "ruleset": "pvp", "name": "Elune",
                                                "faction": "alliance", "race_key": "Night Elf", "class_key": "Druid"})
    html = host.get(page).text
    assert "This event is for Horde WoW: Forever characters" in html and "<strong>Elune</strong>: Alliance, not Horde" in html
    assert page + "/join" not in html
    host.post("/wow/quester/characters", data={"csrf": tok, "edition": "forever", "ruleset": "pvp", "name": "Grom",
                                                "faction": "horde", "race_key": "Orc", "class_key": "Warrior"})
    html = host.get(page).text
    assert "Grom" in html and "Elune" not in re.search(r'<select name="character_id">.*?</select>', html, re.S).group(0)
    r = host.post(page + "/join", data=_form_defaults(html, page + "/join"))
    assert r.status_code == 303, r.text[:300]
    assert "Grom" in host.get(page).text


def test_refused_form_offers_a_working_back_link_and_restore_marker(app):
    """Regression: Back used javascript:history.back(), which the CSP blocks, so it did nothing."""
    c = login(app, 94)
    tok = csrf(c)
    r = c.post("/wow/quester/characters", headers={"referer": "https://community.example.org/wow/quester/characters"},
               data={"csrf": tok, "edition": "forever", "ruleset": "pvp", "name": "Tink", "faction": "horde",
                     "race_key": "Gnome", "class_key": "Mage"})
    assert r.status_code == 422
    assert "javascript:" not in r.text
    assert 'href="/wow/quester/characters" data-back' in r.text
    assert 'data-restore-action="/wow/quester/characters"' in r.text
    # a foreign referer is never echoed back as a link
    r = c.post("/wow/quester/characters", headers={"referer": "https://evil.example.net/x"},
               data={"csrf": tok, "edition": "forever", "ruleset": "pvp", "name": "Tink", "faction": "horde",
                     "race_key": "Gnome", "class_key": "Mage"})
    assert "evil.example.net" not in r.text and "data-back" not in r.text


def test_preferred_role_counts_as_eligible(seeded, cfg):
    from helpers import char
    from conftest import actor
    with seeded.tx() as s:
        c = char(s, cfg, actor(10), "Grom", eligible_roles=[], preferred_role="tank")
        assert c.eligible_roles == ["tank"] and c.preferred_role == "tank"


def test_event_form_shows_one_blank_role_row_and_add_more(app, seeded):
    org = login(app, 95, "event_creator")
    html = org.get("/wow/quester/events/new").text
    rows = html.split("data-role-rows", 1)[1].split("</table>", 1)[0]
    assert rows.count("<tr") == 13 and rows.count("data-role-extra") == 11   # header + 12; only row 1 open
    assert 'placeholder="e.g. tank"' in rows and 'placeholder="e.g. 2"' in rows
    assert "data-add-role" in html and "Add more" in html


def test_role_row_without_how_many_is_refused_not_dropped(app, seeded):
    org = login(app, 96, "event_creator")
    tok = csrf(org)
    start = at(5).astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    base = {"csrf": tok, "edition": "forever", "ruleset": "pvp", "faction": "horde", "title": "Deadmines",
            "activity_type": "dungeon", "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"),
            "duration_minutes": "90", "timezone": "America/Los_Angeles", "signup_policy": "open"}
    r = org.post("/wow/quester/events/new", data={**base, "role_key_0": "tank", "role_qty_0": "1",
                                                  "role_key_1": "healer"})
    assert r.status_code == 422 and "Enter how many healer you need" in r.text
    r = org.post("/wow/quester/events/new", data={**base, "role_qty_0": "2"})
    assert r.status_code == 422 and "has a number but no role" in r.text


def test_naming_checkbox_has_help_popup_linking_privacy(app, seeded):
    org = login(app, 98, "event_creator")
    tok = csrf(org)
    start = at(6).astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    r = org.post("/wow/quester/events/new", data={
        "csrf": tok, "edition": "forever", "ruleset": "pvp", "faction": "horde", "title": "Wailing Caverns",
        "activity_type": "dungeon", "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"),
        "duration_minutes": "90", "timezone": "America/Los_Angeles", "signup_policy": "open", "publish": "1"})
    url = r.headers["location"]
    p = login(app, 99)
    t = csrf(p)
    p.post("/wow/quester/characters", data={"csrf": t, "edition": "forever", "ruleset": "pvp", "name": "Rokhan",
                                            "faction": "horde", "race_key": "troll", "class_key": "hunter"})
    page = p.get(url).text
    assert 'popovertarget="tip-naming-join"' in page and 'id="tip-naming-join" popover' in page
    assert "/wow/quester/privacy#public" in page and "javascript:" not in page
    # the button sits inside the label, the popup itself outside it (no block content in a label)
    label = page.split('name="recognition" value="1"', 1)[1].split("</label>", 1)[0]
    assert "popovertarget" in label and "popover " not in label and "<div" not in label
    cid = re.search(r'name="character_id"><option value="(\d+)"', page).group(1)
    p.post(url + "/join", data={"csrf": t, "character_id": cid})
    assert 'popovertarget="tip-naming-change"' in p.get(url).text
    assert 'popovertarget="tip-naming-default"' in p.get("/wow/quester/me").text
    assert 'id="public"' in TestClient(app, base_url="https://community.example.org").get("/wow/quester/privacy").text


def test_copy_and_edit_forms_are_filled_with_the_events_time(app, seeded):
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from wowquester.web.routes_member import copy_suggestion
    LA = ZoneInfo("America/Los_Angeles")

    class E:   # a past event: the copy suggestion moves forward in whole weeks until it's ahead
        timezone = "America/Los_Angeles"
        start_at = _dt(2026, 10, 7, 19, 30, tzinfo=LA)
    assert copy_suggestion(E, now=_dt(2026, 11, 1, 12, 0, tzinfo=LA)) == ("2026-11-04", "19:30")
    E.start_at = _dt(2026, 10, 28, 19, 30, tzinfo=LA)          # across the Nov 1 clock change: wall time kept
    assert copy_suggestion(E, now=_dt(2026, 10, 27, tzinfo=LA)) == ("2026-11-04", "19:30")

    org = login(app, 100, "event_creator")
    tok = csrf(org)
    start = at(6).astimezone(LA)
    r = org.post("/wow/quester/events/new", data={
        "csrf": tok, "edition": "forever", "ruleset": "pvp", "faction": "horde", "title": "Razorfen run",
        "activity_type": "dungeon", "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"),
        "duration_minutes": "90", "timezone": "America/Los_Angeles", "signup_policy": "open", "publish": "1"})
    url = r.headers["location"]
    page = org.get(url).text
    copy_form = page.split("/copy", 1)[1].split("</form>", 1)[0]
    want_date = (start.date() + __import__("datetime").timedelta(days=7)).isoformat()
    assert f'name="date" required value="{want_date}"' in copy_form
    assert f'name="start_time" required value="{start.strftime("%H:%M")}"' in copy_form
    edit = org.get(url + "/edit").text
    assert f'value="{start.strftime("%Y-%m-%d")}"' in edit and f'value="{start.strftime("%H:%M")}"' in edit

    # a player joins; saving the edit form unchanged must not ask them to reconfirm
    p = login(app, 101)
    t = csrf(p)
    p.post("/wow/quester/characters", data={"csrf": t, "edition": "forever", "ruleset": "pvp", "name": "Vol",
                                            "faction": "horde", "race_key": "orc", "class_key": "warrior"})
    cid = re.search(r'name="character_id"><option value="(\d+)"', p.get(url).text).group(1)
    p.post(url + "/join", data={"csrf": t, "character_id": cid})
    base = {"csrf": tok, "title": "Razorfen run (edited)", "date": start.strftime("%Y-%m-%d"),
            "start_time": start.strftime("%H:%M"), "duration_minutes": "90", "timezone": "America/Los_Angeles",
            "signup_policy": "open", "public_listing": "1"}
    assert org.post(url + "/edit", data=base).status_code == 303
    assert "confirmed" in p.get(url).text.lower() and "reconfirm" not in p.get(url).text.lower()
    # changing only the length now takes effect (it used to need the date re-entered)
    org.post(url + "/edit", data={**base, "duration_minutes": "150"})
    assert 'name="duration_minutes" min="15" max="720" step="15" value="150"' in org.get(url + "/edit").text
    # copying with the suggested values makes a new draft at that time
    r = org.post(url + "/copy", data={"csrf": tok, "date": want_date, "start_time": start.strftime("%H:%M"),
                                      "timezone": "America/Los_Angeles"})
    assert r.status_code == 303 and r.headers["location"].endswith("/edit")
    assert f'value="{want_date}"' in org.get(r.headers["location"]).text


def test_copied_draft_explains_publishing_and_edit_can_publish(app, seeded):
    from wowquester.models import Outbox as OutboxJob
    org = login(app, 102, "event_creator")
    tok = csrf(org)
    start = at(6).astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    r = org.post("/wow/quester/events/new", data={
        "csrf": tok, "edition": "forever", "ruleset": "pvp", "faction": "horde", "title": "Blackfathom",
        "activity_type": "dungeon", "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"),
        "duration_minutes": "90", "timezone": "America/Los_Angeles", "signup_policy": "open", "publish": "1"})
    url = r.headers["location"]
    nxt = (start.date() + __import__("datetime").timedelta(days=7)).isoformat()
    r = org.post(url + "/copy", data={"csrf": tok, "date": nxt, "start_time": start.strftime("%H:%M"),
                                      "timezone": "America/Los_Angeles"})
    edit_url = r.headers["location"]
    draft_url = edit_url.rsplit("/edit", 1)[0]
    edit = org.get(edit_url).text
    assert "This is a draft." in edit and 'name="publish" value="1">Save and publish' in edit
    assert "isn't in Discord yet" in org.get(draft_url).text
    with seeded.tx() as s:
        before = s.query(OutboxJob).filter(OutboxJob.kind == "discord.render_event").count()
    r = org.post(edit_url, data={"csrf": tok, "title": "Blackfathom 2", "date": nxt, "start_time": start.strftime("%H:%M"),
                                 "duration_minutes": "90", "timezone": "America/Los_Angeles", "signup_policy": "open",
                                 "public_listing": "1", "publish": "1"})
    assert r.status_code == 303
    page = org.get(draft_url).text
    assert "isn't in Discord yet" not in page and "Scheduled" in page
    with seeded.tx() as s:
        assert s.query(OutboxJob).filter(OutboxJob.kind == "discord.render_event").count() > before


def test_character_edit_form_fills_in_and_saves(app, seeded):
    p = login(app, 103)
    t = csrf(p)
    p.post("/wow/quester/characters", data={"csrf": t, "edition": "forever", "ruleset": "pvp", "name": "Zanra",
                                            "faction": "horde", "race_key": "Skyborne (Windshaper)",
                                            "class_key": "Druid", "level": "60", "spec": "Feral",
                                            "eligible_roles": ["tank", "healer"], "preferred_role": "tank",
                                            "focus_tags": "rp, pvp"})
    page = p.get("/wow/quester/characters").text
    cid = re.search(r'characters\?edit=(\d+)#character-form" title="Edit Zanra"', page).group(1)
    assert ">Edit</a>" in page and "Add a character" in page
    form = p.get(f"/wow/quester/characters?edit={cid}").text
    assert "Edit Zanra" in form and f'name="character_id" value="{cid}"' in form
    assert 'value="Zanra"' in form and 'value="Skyborne (Windshaper)"' in form and 'value="Druid"' in form
    assert 'value="60"' in form and 'value="Feral"' in form and 'value="rp, pvp"' in form
    assert '<option value="pvp" selected>' in form and 'value="tank" checked' in form and 'value="healer" checked' in form
    assert "Save changes" in form and "Cancel" in form
    # save: switch to RP, clear level and spec (blank must clear, not keep)
    r = p.post("/wow/quester/characters", data={"csrf": t, "character_id": cid, "edition": "forever", "ruleset": "rp",
                                                "name": "Zanra Stormhide", "faction": "horde",
                                                "race_key": "Skyborne (Windshaper)", "class_key": "Druid",
                                                "eligible_roles": ["healer"], "focus_tags": "rp"})
    assert r.status_code == 303
    after = p.get("/wow/quester/characters").text
    assert "Zanra Stormhide" in after and "FOREVER · RP" in after.upper() and "level 60" not in after
    assert after.count("title=\"Edit ") == 1          # edited in place, not added
    # someone else's character can't be opened for editing
    assert login(app, 104).get(f"/wow/quester/characters?edit={cid}").status_code == 404


def test_character_edit_offers_leave_and_save_for_every_blocking_event(app, seeded):
    LA = __import__("zoneinfo").ZoneInfo("America/Los_Angeles")
    org = login(app, 105, "event_creator")
    tok = csrf(org)
    urls = []
    for i, title in enumerate(("Deadmines", "Stockade", "Gnomeregan")):
        start = at(10 + 5 * i).astimezone(LA)
        r = org.post("/wow/quester/events/new", data={
            "csrf": tok, "edition": "forever", "ruleset": "pvp", "faction": "horde", "title": title,
            "activity_type": "dungeon", "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"),
            "duration_minutes": "90", "timezone": "America/Los_Angeles", "signup_policy": "open", "publish": "1"})
        urls.append(r.headers["location"])
    p = login(app, 106)
    t = csrf(p)
    base = {"csrf": t, "edition": "forever", "ruleset": "pvp", "name": "Zanra", "faction": "horde",
            "race_key": "Troll", "class_key": "Priest", "eligible_roles": ["healer"]}
    p.post("/wow/quester/characters", data=base)
    cid = re.search(r'characters\?edit=(\d+)#character-form" title="Edit Zanra"', p.get("/wow/quester/characters").text).group(1)
    for u in urls[:2]:
        p.post(u + "/join", data={"csrf": t, "character_id": cid})
    # ruleset change blocked by two events: one button leaves both and saves
    r = p.post("/wow/quester/characters", data={**base, "character_id": cid, "ruleset": "rp"})
    assert r.status_code == 409 and "signed up for 2 events" in r.text
    assert "Deadmines" in r.text and "Stockade" in r.text and "Gnomeregan" not in r.text
    assert "Leave these 2 events and save" in r.text and 'name="leave_events" value="1"' in r.text
    hidden = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', r.text.split("leave_events")[0]))
    assert hidden["ruleset"] == "rp" and hidden["character_id"] == cid
    r = p.post("/wow/quester/characters", data={**base, "character_id": cid, "ruleset": "rp", "leave_events": "1"})
    assert r.status_code == 303
    assert "FOREVER · RP" in p.get("/wow/quester/characters").text.upper()
    for u in urls[:2]:
        assert "You're <strong>" not in p.get(u).text       # left: no longer on the roster
    # edition locked by history: offer "add as new" instead, which keeps the original
    r = p.post("/wow/quester/characters", data={**base, "character_id": cid, "ruleset": "rp", "edition": "retail",
                                                "realm_slug": "area-52"})
    assert r.status_code == 409 and "Add it as a new WoW (Retail) character" in r.text and "leave_events" not in r.text
    new_form = r.text.split('name="as_new"')[0].rsplit("<form", 1)[1]
    assert 'name="character_id"' not in new_form
    r = p.post("/wow/quester/characters", data={**base, "ruleset": "", "edition": "retail", "realm_slug": "area-52",
                                                "as_new": "1", "character_id": cid})
    assert r.status_code == 303
    page = p.get("/wow/quester/characters").text
    assert page.count('title="Edit Zanra"') == 2 and "WoW (Retail)" in page


def test_remove_me_needs_typed_phrase_then_logs_out_with_receipt(app, seeded):
    org = login(app, 107, "event_creator")
    tok = csrf(org)
    start = at(20).astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    r = org.post("/wow/quester/events/new", data={
        "csrf": tok, "edition": "forever", "ruleset": "pvp", "faction": "horde", "title": "Ragefire",
        "activity_type": "dungeon", "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"),
        "duration_minutes": "90", "timezone": "America/Los_Angeles", "signup_policy": "open", "publish": "1"})
    url = r.headers["location"]
    p = login(app, 108)
    t = csrf(p)
    p.post("/wow/quester/characters", data={"csrf": t, "edition": "forever", "ruleset": "pvp", "name": "Korrak",
                                            "faction": "horde", "race_key": "Orc", "class_key": "Warrior",
                                            "eligible_roles": ["tank"]})
    cid = re.search(r'characters\?edit=(\d+)#character-form', p.get("/wow/quester/characters").text).group(1)
    p.post(url + "/join", data={"csrf": t, "character_id": cid})
    assert "/wow/quester/me/remove" in p.get("/wow/quester/me").text
    page = p.get("/wow/quester/me/remove").text
    assert "can't be undone" in page and "Ragefire" in page and "remove me" in page
    for wrong in ("", "remove", "yes"):
        r = p.post("/wow/quester/me/remove", data={"csrf": t, "confirm": wrong})
        assert r.status_code == 400 and "Nothing has been removed" in r.text
    with seeded.tx() as s:
        assert s.execute(text("select count(*) from members where discord_id=:d"), {"d": fake_id(108)}).scalar() == 1
    r = p.post("/wow/quester/me/remove", data={"csrf": t, "confirm": "Remove me"})
    assert r.status_code == 200 and "Your request is complete" in r.text
    assert "Left 1 event: Ragefire" in r.text and "Disconnected your Discord account" in r.text
    assert "Log out" not in r.text
    assert p.get("/wow/quester/me").status_code in (302, 303)        # logged out
    with seeded.tx() as s:
        assert s.execute(text("select count(*) from members where discord_id=:d"), {"d": fake_id(108)}).scalar() == 0
        assert s.execute(text("select count(*) from web_sessions s join members m on m.id=s.member_id "
                              "where m.erased_at is not null")).scalar() == 0
        assert s.execute(text("select count(*) from characters where name='Korrak'")).scalar() == 0
    # logging in again starts a brand-new empty profile
    again = login(app, 108)
    assert "Korrak" not in again.get("/wow/quester/characters").text


def test_remove_me_when_signed_up_to_an_event_you_host(app, seeded):
    """Regression: a host who joined their own event made the confirmation page fail (500)."""
    org = login(app, 109, "event_creator")
    tok = csrf(org)
    start = at(30).astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    url = org.post("/wow/quester/events/new", data={
        "csrf": tok, "edition": "forever", "ruleset": "pvp", "faction": "horde", "title": "Own raid",
        "activity_type": "dungeon", "date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"),
        "duration_minutes": "90", "timezone": "America/Los_Angeles", "signup_policy": "open",
        "publish": "1"}).headers["location"]
    org.post("/wow/quester/characters", data={"csrf": tok, "edition": "forever", "ruleset": "pvp", "name": "Hostchar",
                                              "faction": "horde", "race_key": "Orc", "class_key": "Warrior",
                                              "eligible_roles": ["tank"]})
    cid = re.search(r'characters\?edit=(\d+)#character-form', org.get("/wow/quester/characters").text).group(1)
    org.post(url + "/join", data={"csrf": tok, "character_id": cid})
    page = org.get("/wow/quester/me/remove")
    assert page.status_code == 200
    assert page.text.count(">Own raid</a>") == 1 and "you host" in page.text and "You aren't signed up" in page.text
    r = org.post("/wow/quester/me/remove", data={"csrf": tok, "confirm": "remove me"})
    assert r.status_code == 200 and "Your request is complete" in r.text
    with seeded.tx() as s:
        assert s.execute(text("select state from events where title='Own raid'")).scalar() == "cancelled"
