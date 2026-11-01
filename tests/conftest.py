"""Test harness. Runs against a real local PostgreSQL (the constraints under test only exist there).

Environment (see docs/testing.md):
  WQT_PG_HOST / WQT_PG_PORT           default 127.0.0.1 / 5433
  WQT_ADMIN_PW, WQT_APP_PW, WQT_WORKER_PW, WQT_READONLY_PW   passwords of the test roles
Tests are skipped (not failed) when the test cluster isn't reachable.

All Discord IDs below are synthetic and generated at runtime.
"""

from __future__ import annotations

import copy
import os
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from wowquester.config import Config, _hashes, validate
from wowquester.db import Database
from wowquester.domain.actor import Actor

ROOT = Path(__file__).resolve().parent.parent
HOST = os.environ.get("WQT_PG_HOST", "127.0.0.1")
PORT = os.environ.get("WQT_PG_PORT", "5433")


def fake_id(n: int) -> str:
    """Synthetic 18-digit Discord-style ID."""
    return str(10 ** 17 + n)


ROLE = {cap: fake_id(900 + i) for i, cap in enumerate(
    ("event_creator", "event_moderator", "exclusive_designator", "chronicle_editor", "chronicle_reviewer",
     "trusted_author", "admin", "member"))}


def url(role: str) -> str:
    pw = os.environ.get(f"WQT_{role.split('_', 1)[1].upper()}_PW", "")
    return f"postgresql+psycopg://{role}:{pw}@{HOST}:{PORT}/wowquester"  # sanitize: fixture (pw from env)


def _reachable() -> bool:
    try:
        e = create_engine(url("wowquester_admin"))
        with e.connect() as c:
            c.execute(text("select 1"))
        e.dispose()
        return True
    except Exception:
        return False


REACHABLE = _reachable()
needs_db = pytest.mark.skipif(not REACHABLE, reason="test PostgreSQL not reachable")


def make_cfg(tmp_path: Path | None = None, **over) -> Config:
    raw = tomllib.loads((ROOT / "deploy" / "config.example.toml").read_text())
    raw["community"]["guild_id"] = fake_id(1)
    raw["community"]["public_base_url"] = "https://community.example.org"
    for cap in ROLE:
        if cap in raw["roles"]:
            raw["roles"][cap] = [ROLE[cap]]
    raw["uploads"]["storage_dir"] = str(tmp_path or "/tmp/wqt_uploads")
    for sec, vals in over.items():
        raw[sec] = {**raw.get(sec, {}), **vals} if isinstance(vals, dict) else vals
    data = copy.deepcopy(raw)
    return Config(raw=validate(raw), path="test", section_hashes=_hashes(data))


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


def actor(n: int, *caps: str, source: str = "web", name: str | None = None) -> Actor:
    return Actor(discord_id=fake_id(n), source=source, role_ids=frozenset(ROLE[c] for c in caps),
                 display_name=name or f"DiscordUser{n}")


@pytest.fixture(scope="session")
def admin_engine():
    if not REACHABLE:
        pytest.skip("test PostgreSQL not reachable")
    e = create_engine(url("wowquester_admin"), isolation_level="AUTOCOMMIT")
    yield e
    e.dispose()


TABLES_KEEP = {"alembic_version", "schedule_policy"}


@pytest.fixture
def db(admin_engine):
    with admin_engine.connect() as c:
        names = [r[0] for r in c.execute(text(
            "select tablename from pg_tables where schemaname='public'"))]
        c.execute(text("TRUNCATE " + ", ".join(n for n in names if n not in TABLES_KEEP) +
                       " RESTART IDENTITY CASCADE"))
        c.execute(text("UPDATE schedule_policy SET max_simultaneous = 1"))
    from wowquester.db import make_engine
    eng = make_engine(url("wowquester_app"), None, pool_size=10)
    yield Database(eng)
    eng.dispose()


@pytest.fixture
def worker_db(admin_engine):
    from wowquester.db import make_engine
    eng = make_engine(url("wowquester_worker"), None)
    yield Database(eng)
    eng.dispose()


def at(hours: float, base: datetime | None = None) -> datetime:
    b = base or (datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=2))
    return b + timedelta(hours=hours)


@pytest.fixture
def seeded(db, cfg):
    from wowquester.domain import catalog
    with db.tx() as s:
        catalog.seed_edition(s, cfg, "forever")
    return db
