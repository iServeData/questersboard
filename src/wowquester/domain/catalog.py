"""Reference data: races, classes, combos, zones, quests, locations — with provenance."""

from __future__ import annotations

import tomllib
from importlib import resources

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import Config
from ..models import ReferenceOption


def load_seed(name: str) -> dict:
    return tomllib.loads(resources.files("wowquester.catalog").joinpath(f"{name}.toml").read_text())


def seed_edition(s: Session, cfg: Config, edition_key: str) -> int:
    """Insert packaged seed rows for an edition (never overwrites Blizzard-imported rows)."""
    ed = cfg.edition(edition_key)
    if not ed.catalog:
        return 0
    data = load_seed(ed.catalog)
    src = data.get("source_url")
    rows = []
    for c in data["classes"]:
        rows.append(dict(edition=ed.key, kind="class", key=c, label=data["class_labels"][c], parent_key="",
                         source="seed", source_ref=src, verification="unverified", data={}))
    for faction, races in data["races"].items():
        for rk, r in races.items():
            rows.append(dict(edition=ed.key, kind="race", key=rk, label=r["label"], parent_key=faction,
                             source="seed", source_ref=src, verification="unverified", data={}))
            for c in r["classes"]:
                rows.append(dict(edition=ed.key, kind="race_class", key=f"{rk}:{c}", label=f"{r['label']} {c}",
                                 parent_key=faction, source="seed", source_ref=src, verification="unverified",
                                 data={}))
    n = 0
    for row in rows:
        stmt = (insert(ReferenceOption).values(**row).on_conflict_do_nothing(constraint="ux_ref_key")
                .returning(ReferenceOption.id))
        # count RETURNING rows: rowcount is -1 for this statement with SQLAlchemy 2.1 + psycopg
        n += len(s.execute(stmt).all())
    return n


def options(s: Session, edition: str, kind: str, parent: str | None = None, q: str | None = None,
            limit: int = 200) -> list[ReferenceOption]:
    stmt = select(ReferenceOption).where(ReferenceOption.edition == edition, ReferenceOption.kind == kind,
                                         ReferenceOption.retired_at.is_(None))
    if parent is not None:
        stmt = stmt.where(ReferenceOption.parent_key == parent)
    if q:
        stmt = stmt.where(ReferenceOption.label.ilike(f"%{q.replace('%', '')}%"))
    return list(s.scalars(stmt.order_by(ReferenceOption.label).limit(limit)))


def race_class_allowed(s: Session, edition: str, faction: str, race: str, klass: str) -> bool | None:
    """True/False when the catalog knows combos for this edition; None when it has none (free entry)."""
    any_combo = s.scalar(select(ReferenceOption.id).where(ReferenceOption.edition == edition,
                                                          ReferenceOption.kind == "race_class").limit(1))
    if any_combo is None:
        return None
    hit = s.scalar(select(ReferenceOption.id).where(
        ReferenceOption.edition == edition, ReferenceOption.kind == "race_class",
        ReferenceOption.key == f"{race}:{klass}", ReferenceOption.parent_key == faction,
        ReferenceOption.retired_at.is_(None)))
    return hit is not None
