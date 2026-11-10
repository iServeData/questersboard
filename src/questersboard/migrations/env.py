from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from questersboard.models import Base

target_metadata = Base.metadata


def _url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or os.environ.get("QUESTERSBOARD_MIGRATE_URL")
    if not url:
        raise SystemExit("migrations need -x url=... or QUESTERSBOARD_MIGRATE_URL (the questersboard_admin role)")
    return url


def run_migrations_offline() -> None:
    """--sql: print the SQL without connecting (no password needed)."""
    url = context.get_x_argument(as_dictionary=True).get("url") or os.environ.get("QUESTERSBOARD_MIGRATE_URL") \
        or "postgresql+psycopg://"
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection")
    if connectable is None:
        eng = create_engine(_url(), poolclass=pool.NullPool)
        with eng.connect() as conn:
            context.configure(connection=conn, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    else:
        context.configure(connection=connectable, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
