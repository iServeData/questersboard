"""Background jobs the web service runs itself: the "web." outbox kinds.

/character refresh is asked for in Discord, but the bot holds no Blizzard credentials and the worker's
database role can't read or write imported character details. The web service has both already (it
runs every Refresh from Blizzard button), so it picks these jobs up: a small thread, a few at a time,
never retried (the member is told and can press Refresh from Blizzard on the site instead).
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import text

from ..domain import commands

log = logging.getLogger("questersboard.web.jobs")
POLL_SECONDS = 2


def claim(db, n: int = 5) -> list[dict]:
    with db.tx() as s:
        rows = s.execute(text("""
            UPDATE outbox SET locked_until = now() + interval '2 minutes', attempts = attempts + 1
             WHERE id IN (SELECT id FROM outbox
                           WHERE done_at IS NULL AND dead_at IS NULL AND run_after <= now()
                             AND (locked_until IS NULL OR locked_until < now()) AND kind = :k
                           ORDER BY run_after LIMIT :n FOR UPDATE SKIP LOCKED)
            RETURNING id, kind, key, payload, attempts"""), {"k": commands.REFRESH_JOB, "n": n}).mappings().all()
        return [dict(r) for r in rows]


def _finish(db, job: dict, error: str = "") -> None:
    with db.tx() as s:
        if error:
            s.execute(text("UPDATE outbox SET dead_at = now(), last_error = :e, locked_until = NULL WHERE id = :id"),
                      {"id": job["id"], "e": error[:500]})
        else:
            s.execute(text("UPDATE outbox SET done_at = now(), locked_until = NULL WHERE id = :id AND attempts = :a"),
                      {"id": job["id"], "a": job["attempts"]})
            s.execute(text("UPDATE outbox SET locked_until = NULL WHERE id = :id AND done_at IS NULL"),
                      {"id": job["id"]})


def run_once(web) -> int:
    """Run what's waiting; returns how many jobs were picked up."""
    from ..domain.errors import DomainError
    from ..integrations.blizzard import ApiError, Unsupported
    jobs = claim(web.db)
    for job in jobs:
        p = job["payload"] or {}
        try:
            with web.db.tx() as s:
                got = commands.run_refresh(s, web.cfg, web.bz, int(p["character_id"]), int(p["member_id"]))
        except (DomainError, ApiError, Unsupported) as exc:
            _finish(web.db, job, f"not refreshed: {type(exc).__name__}")
            continue
        except Exception as exc:  # noqa: BLE001 - keep the thread alive; recorded on the job
            log.exception("web job %s failed", job["kind"])
            _finish(web.db, job, f"not refreshed: {type(exc).__name__}")
            continue
        _finish(web.db, job, "" if got == "refreshed" else "not refreshed: the character changed")
    return len(jobs)


def start(web, stop: threading.Event | None = None) -> threading.Thread:  # pragma: no cover - service only
    stop = stop or threading.Event()

    def loop():
        while not stop.is_set():
            try:
                if run_once(web):
                    continue
            except Exception:  # noqa: BLE001 - a database hiccup: wait and try again
                log.exception("web jobs pass failed")
            stop.wait(POLL_SECONDS)
    t = threading.Thread(target=loop, name="questersboard-web-jobs", daemon=True)
    t.start()
    return t
