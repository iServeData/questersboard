"""Local preview server with FIXTURE data. NOT for production.

Runs the real web app against a local test database with:
  * FakeOAuth: "Log in" signs you in as PREVIEW_USER without contacting Discord.
  * FakeRest: that user is a guild member holding every capability role.
  * Seed data: a few events (upcoming, approval-required, draft) and one published chronicle story.
Everything is labelled FIXTURE. All IDs are synthetic.

  QUESTERSBOARD_PREVIEW_DB=postgresql+psycopg://questersboard_app:${WQT_APP_PW}@127.0.0.1:5433/questersboard \
  QUESTERSBOARD_PREVIEW_ADMIN_DB=postgresql+psycopg://questersboard_admin:${WQT_ADMIN_PW}@127.0.0.1:5433/questersboard \
  QUESTERSBOARD_PREVIEW_ASSETS=/path/to/site/assets   # optional: host site's /assets for real styling
  python tools/preview_server.py --port 8740 --reset
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from questersboard.config import Config, ConfigHolder, _hashes, validate  # noqa: E402
from questersboard.db import Database, make_engine  # noqa: E402
from questersboard.domain.actor import Actor  # noqa: E402


def fid(n: int) -> str:
    return str(10 ** 17 + n)


CAPS = ("event_creator", "event_moderator", "exclusive_designator", "chronicle_editor", "chronicle_reviewer",
        "trusted_author", "admin")
ROLE = {c: fid(900 + i) for i, c in enumerate(CAPS)}
PREVIEW_USER = fid(1)


class FakeOAuth:  # FIXTURE
    def authorize_url(self, redirect_uri, state, prompt="none"):
        return f"{redirect_uri}?code={PREVIEW_USER}&state={state}"

    def identify(self, code, redirect_uri):
        return code, "PreviewOrganizer"


class FakeRest:  # FIXTURE
    def member(self, guild_id, user_id):
        if user_id == PREVIEW_USER:
            return True, frozenset(ROLE.values()), "PreviewOrganizer"
        return True, frozenset(), f"Member{user_id[-3:]}"


def cfg_for(uploads: str) -> Config:
    raw = tomllib.loads((ROOT / "deploy" / "config.example.toml").read_text())
    raw["community"]["guild_id"] = fid(1000)
    raw["discord"]["application_id"] = fid(1001)
    for c in CAPS:
        raw["roles"][c] = [ROLE[c]]
    raw["uploads"]["storage_dir"] = uploads
    raw["web"]["cookie_secure"] = False
    raw["web"]["bind"] = "127.0.0.1:8740"
    if not os.environ.get("QUESTERSBOARD_PREVIEW_ASSETS"):
        raw["branding"]["stylesheet"] = ""
    import copy
    data = copy.deepcopy(raw)
    v = validate(raw)
    # Preview only: point links/OAuth redirect at the local server (validation requires https in production).
    v["community"]["public_base_url"] = os.environ.get("QUESTERSBOARD_PREVIEW_BASE", "http://127.0.0.1:8740")
    return Config(raw=v, path="preview", section_hashes=_hashes(data))


def actor(n: int, *caps, name: str | None = None) -> Actor:
    return Actor(discord_id=fid(n), source="web", role_ids=frozenset(ROLE[c] for c in caps),
                 display_name=name or f"Member{n}")


def seed(db: Database, cfg: Config) -> None:
    from questersboard.domain import catalog, chronicle, events, signups, roster
    from questersboard import domain
    import importlib
    org = actor(1, *CAPS, name="PreviewOrganizer")
    rev = actor(2, "chronicle_reviewer", name="ReviewerFixture")
    members = [actor(10 + i, name=f"Player{i}") for i in range(6)]
    mods = [importlib.import_module(f"questersboard.domain.{m}") for m in
            ("events", "signups", "scheduling", "chronicle", "projections", "roster", "recurrence", "outbox")]
    real_now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    def set_now(t):
        for m in mods:
            if hasattr(m, "now"):
                m.now = (lambda tt=t: tt)

    hz = "America/Los_Angeles"
    chars = [("Gorrek", "orc", "warrior", ["tank", "damage"], "tank", True),
             ("Moonwhisper", "tauren", "druid", ["healer", "tank"], "healer", True),
             ("Zultani", "troll", "priest", ["healer"], "healer", False),
             ("Skarn", "undead", "rogue", ["damage"], "damage", True),
             ("Embervale", "undead", "mage", ["damage"], "damage", True),
             ("Vashka", "troll", "hunter", ["damage"], "damage", False)]
    with db.tx() as s:
        for ed in cfg.editions:
            catalog.seed_edition(s, cfg, ed)
        mychar = roster.add_character(s, cfg, org, dict(edition="forever", ruleset="pvp", faction="horde",
                                                         name="Hollowfang", race_key="orc", class_key="hunter",
                                                         eligible_roles=["damage"], preferred_role="damage"))
        cids = []
        for a, (nm, race, klass, roles, pref, _c) in zip(members, chars):
            cids.append(roster.add_character(s, cfg, a, dict(edition="forever", ruleset="pvp", faction="horde",
                                                             name=nm, race_key=race, class_key=klass,
                                                             eligible_roles=roles, preferred_role=pref)).id)

        # past event with a published story
        t0 = real_now - timedelta(days=3, hours=3)
        set_now(t0 - timedelta(days=1))
        past = events.create_draft(s, cfg, org, dict(
            edition="forever", ruleset="pvp", faction="horde", title="Wailing Caverns first clear",
            activity_type="dungeon", start_at=t0, end_at=t0 + timedelta(hours=2), timezone=hz,
            roles=[{"key": "tank", "label": "Tank", "quantity": 1}, {"key": "healer", "label": "Healer", "quantity": 1},
                   {"key": "damage", "label": "Damage", "quantity": 3}],
            description="FIXTURE: first guild dungeon after launch."))
        events.publish(s, cfg, org, past.id)
        for a, cid, (nm, _r, _k, _roles, pref, consent) in zip(members, cids, chars):
            signups.join(s, cfg, a, past.id, cid, role_key=pref, recognition=consent)
        set_now(t0 + timedelta(minutes=5))
        events.start(s, cfg, org, past.id)
        set_now(t0 + timedelta(hours=2, minutes=10))
        events.end(s, cfg, org, past.id)
        ids = dict(s.execute(text("select discord_id, id from members")).all())
        events.record_attendance(s, cfg, org, past.id, {ids[a.discord_id]: "attended" for a in members[:5]}
                                 | {ids[members[5].discord_id]: "absent"})
        e = s.execute(text("select id from chronicle_entries where event_id=:e"), {"e": past.id}).scalar()
        chronicle.save(s, cfg, org, e, {
            "title": "Wailing Caverns first clear", "outcome": "completed",
            "attempted": "Clear Wailing Caverns with a fresh group the week after launch.",
            "happened": "Gorrek held aggro through the long serpent pulls while Moonwhisper and our other "
                        "healer kept everyone standing. We wiped once at Lord Serpentis, regrouped, and cleared "
                        "the rest without losing anyone.",
            "moments": "Skarn found the hidden path above the waterfall. Mutanus went down on the first try."})
        if chronicle.submit(s, cfg, org, e).state == "pending_review":   # organizer is a trusted author here
            chronicle.review(s, cfg, rev, e, "approve")
        mine = chronicle.start_story(s, cfg, members[0], past.id)       # an attendee's own story
        chronicle.save(s, cfg, members[0], mine.id, {
            "happened": "Tanking the serpent pulls was chaos in the best way. I lost aggro once and "
                        "Moonwhisper saved me with a last-second heal.",
            "moments": "Hearing the whole group cheer when Mutanus dropped."})
        chronicle.submit(s, cfg, members[0], mine.id)
        chronicle.review(s, cfg, rev, mine.id, "approve")

        # upcoming events
        set_now(real_now)
        base = (real_now + timedelta(days=1)).astimezone(timezone(timedelta(hours=-8))).replace(hour=19) \
            .astimezone(timezone.utc)
        up1 = events.create_draft(s, cfg, org, dict(
            edition="forever", ruleset="pvp", faction="horde", title="Barrens questing night",
            activity_type="quest", start_at=base, end_at=base + timedelta(hours=2), timezone=hz,
            activity_location={"label": "The Barrens", "zone": "The Barrens"},
            muster_location={"label": "Crossroads inn"},
            roles=[{"key": "tank", "label": "Tank", "quantity": 1}, {"key": "healer", "label": "Healer", "quantity": 1},
                   {"key": "damage", "label": "Damage", "quantity": 3}],
            description="FIXTURE: relaxed levelling for new players. Bring hearthstones."))
        events.publish(s, cfg, org, up1.id)
        signups.join(s, cfg, members[0], up1.id, cids[0], role_key="tank")
        signups.join(s, cfg, members[3], up1.id, cids[3], role_key="damage")
        b2 = base + timedelta(days=2)
        up2 = events.create_draft(s, cfg, org, dict(
            edition="forever", ruleset="pvp", faction="horde", title="Shadowfang Keep",
            activity_type="dungeon", start_at=b2, end_at=b2 + timedelta(hours=2, minutes=30), timezone=hz,
            signup_policy="approval",
            roles=[{"key": "tank", "label": "Tank", "quantity": 1}, {"key": "healer", "label": "Healer", "quantity": 1},
                   {"key": "damage", "label": "Damage", "quantity": 3}],
            description="FIXTURE: approval required, level 20+."))
        events.publish(s, cfg, org, up2.id)
        signups.join(s, cfg, members[1], up2.id, cids[1], role_key="healer")
        signups.join(s, cfg, org, up2.id, mychar.id, role_key="damage")
        b3 = base + timedelta(days=5)
        events.create_draft(s, cfg, org, dict(
            edition="forever", ruleset="pvp", faction="horde", title="Crossroads RP evening",
            activity_type="rp", start_at=b3, end_at=b3 + timedelta(hours=2), timezone=hz,
            description="FIXTURE: draft, not yet published."))
    for m in mods:
        if hasattr(m, "now"):
            from questersboard.domain import timeutil
            m.now = timeutil.now
    seed_boards(db, cfg, org, members, mychar.id, cids, real_now)


def seed_boards(db: Database, cfg: Config, org: Actor, members: list, mychar: int, cids: list, real_now) -> None:
    """FIXTURE posts on the Adventure Board and Profession help, with FIXTURE Blizzard profession data."""
    from questersboard.domain import boards
    from questersboard.models import CharacterProfile
    hz = "America/Los_Angeles"
    day = (real_now + timedelta(days=2)).astimezone(timezone(timedelta(hours=-8))).replace(hour=19) \
        .astimezone(timezone.utc)
    lw = {"name": "Leatherworking", "skill": 165, "recipes": [{"name": "Heavy Armor Kit", "id": 1},
                                                             {"name": "Guardian Belt", "id": 2}]}
    with db.tx() as s:
        for cid, prof in ((cids[0], lw), (mychar, {"name": "Leatherworking", "skill": 120,
                                                    "recipes": [{"name": "Heavy Armor Kit", "id": 1}]})):
            s.execute(text("update characters set verification='ownership_verified' where id=:i"), {"i": cid})
            ch = s.execute(text("select member_id, edition, region from characters where id=:i"), {"i": cid}).one()
            s.add(CharacterProfile(character_id=cid, member_id=ch[0], edition=ch[1], region=ch[2],
                                   groups={"professions": [prof]}, shares={"professions": "members"}))
        # FIXTURE portrait (the preview serves a stand-in picture for it), shared with members
        s.execute(text("update characters set verification='ownership_verified' where id=:i"), {"i": cids[1]})
        ch = s.execute(text("select member_id, edition, region from characters where id=:i"), {"i": cids[1]}).one()
        s.add(CharacterProfile(character_id=cids[1], member_id=ch[0], edition=ch[1], region=ch[2],
                               groups={"portrait": [{"key": "avatar", "url": "https://render.worldofwarcraft.com/"
                                                     "forever-us/character/fixture-avatar.jpg"}]},
                               shares={"portrait": "members"}))
        s.flush()
        a = boards.create(s, cfg, members[1], "adventure", dict(
            kind="need_help", character_id=cids[1], title="Wailing Caverns, first clear", activity_type="dungeon",
            body="FIXTURE: never been inside. Happy to go slow and learn the pulls.", timezone=hz,
            windows=[(day, day + timedelta(hours=2)), (day + timedelta(days=1), day + timedelta(days=1, hours=3))],
            tags=["First-timers welcome", "Explanations wanted"]))
        boards.respond(s, cfg, members[2], str(a.public_id), "need")
        boards.respond(s, cfg, members[3], str(a.public_id), "help")
        boards.create(s, cfg, members[4], "adventure", dict(
            kind="company", character_id=cids[4], title="Questing in the Barrens, levels 14 to 18",
            activity_type="quest", body="FIXTURE: chatty questing, voice optional.", timezone=hz,
            windows=[(day + timedelta(hours=1), day + timedelta(hours=3))], tags=["Social", "Relaxed pace"]))
        boards.create(s, cfg, org, "adventure", dict(
            kind="offer_help", character_id=mychar, title="Happy to run Ragefire Chasm for anyone new",
            activity_type="dungeon", timezone=hz, tags=["Explanations provided"]))
        boards.create(s, cfg, members[2], "profession", dict(
            kind="need_crafted", character_id=cids[2], title="Heavy Armor Kit", body="FIXTURE: two please, I have leather."))
        boards.create(s, cfg, members[0], "profession", dict(kind="can_craft", character_id=cids[0],
                                                              recipe_name="Guardian Belt", body="Bring 4 heavy leather.",
                                                              fee="mats_fee", fee_gold="1", fee_silver="50"))
        q = boards.create(s, cfg, members[5], "profession", dict(
            kind="recipe", character_id=cids[5], profession="Leatherworking", title="Pattern: Barbaric Gloves"))
        boards.respond(s, cfg, members[0], str(q.public_id), "answer", note="FIXTURE: sold by the leatherworking supplier in the Crossroads.")
        boards.create(s, cfg, members[3], "profession", dict(
            kind="farm", character_id=cids[3], profession="Skinning", title="Skinning 150 to 200 in the Barrens"))


def reset(admin_url: str) -> None:
    e = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with e.connect() as c:
        names = [r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))]
        keep = {"alembic_version", "schedule_policy"}
        c.execute(text("TRUNCATE " + ", ".join(n for n in names if n not in keep) + " RESTART IDENTITY CASCADE"))
    e.dispose()


def make_screenshot_png(path: Path) -> None:  # simple FIXTURE image for the upload preview
    Image.new("RGB", (960, 540), (40, 30, 20)).save(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8740)
    ap.add_argument("--reset", action="store_true", help="TRUNCATE the preview database and reseed")
    ap.add_argument("--uploads", default="/tmp/questersboard_preview_uploads")
    a = ap.parse_args()
    url, admin = os.environ["QUESTERSBOARD_PREVIEW_DB"], os.environ.get("QUESTERSBOARD_PREVIEW_ADMIN_DB")
    Path(a.uploads).mkdir(parents=True, exist_ok=True)
    cfg = cfg_for(a.uploads)
    db = Database(make_engine(url, None, 5, "questersboard-preview"))
    if a.reset:
        reset(admin or url)
        seed(db, cfg)
    from questersboard.web.app import create_app
    app = create_app(ConfigHolder(cfg), db, FakeOAuth(), FakeRest())
    assets = os.environ.get("QUESTERSBOARD_PREVIEW_ASSETS")
    if assets:
        from fastapi.staticfiles import StaticFiles
        app.mount("/assets", StaticFiles(directory=assets), name="site_assets")
    site = os.environ.get("QUESTERSBOARD_PREVIEW_SITE")      # optional: serve a static /wow/ page for the widget
    if site:
        from fastapi.responses import FileResponse

        @app.get("/wow/")
        def wow_page():
            return FileResponse(Path(site) / "wow" / "index.html")

        @app.middleware("http")
        async def static_page_csp(request, call_next):   # the real site is served by Nginx, not this app
            resp = await call_next(request)
            if request.url.path == "/wow/":
                del resp.headers["content-security-policy"]
            return resp
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
