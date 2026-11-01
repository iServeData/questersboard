"""Engines and sessions. Passwords come from credentials, never from config."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from .secrets import get_secret

_ADVISORY_SCHEDULE_LOCK = 727001  # must match the trigger in migrations


def make_engine(url: str, credential: str | None, pool_size: int = 5, app_name: str = "wowquester") -> Engine:
    u = make_url(url)
    if credential:
        sec = get_secret(credential, required=False)
        if sec:
            u = u.set(password=sec.reveal())
    eng = create_engine(u, pool_size=pool_size, pool_pre_ping=True, future=True,
                        connect_args={"application_name": app_name, "connect_timeout": 10})

    @event.listens_for(eng, "connect")
    def _on_connect(dbapi_conn, _rec):  # pragma: no cover - trivial
        with dbapi_conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute("SET statement_timeout = '15s'")
            cur.execute("SET lock_timeout = '10s'")
        dbapi_conn.commit()

    return eng


class Database:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._factory = sessionmaker(engine, expire_on_commit=False, future=True)

    @contextmanager
    def tx(self) -> Iterator[Session]:
        s = self._factory()
        try:
            with s.begin():
                yield s
        finally:
            s.close()

    def session(self) -> Session:
        return self._factory()


def schedule_lock(s: Session) -> None:
    """Serialize every change to community-wide WoW reservations (same key as the DB trigger)."""
    from sqlalchemy import text
    s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_SCHEDULE_LOCK})
