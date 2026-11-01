"""Transactional outbox: jobs are written in the same transaction as the change that needs them."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from .timeutil import now


def enqueue(s: Session, kind: str, key: str, payload: dict | None = None, run_after: datetime | None = None) -> None:
    """Pending jobs with the same kind+key coalesce: the newest payload wins, the earliest run time wins."""
    s.execute(text("""
        INSERT INTO outbox (kind, key, payload, run_after)
        VALUES (:k, :key, CAST(:p AS jsonb), :ra)
        ON CONFLICT (kind, key) WHERE done_at IS NULL AND dead_at IS NULL
        DO UPDATE SET payload = EXCLUDED.payload, run_after = LEAST(outbox.run_after, EXCLUDED.run_after),
                      attempts = 0, last_error = ''
    """), {"k": kind, "key": key, "p": __import__("json").dumps(payload or {}), "ra": run_after or now()})
