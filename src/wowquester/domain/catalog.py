"""Reference data: races, classes, combos, zones, quests, locations — with provenance."""

from __future__ import annotations

import re
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
    for lname in data.get("locations", []):
        for cat, labels in load_seed(lname).items():
            if not isinstance(labels, list):
                continue
            for lbl in labels:
                key = re.sub(r"[^a-z0-9]+", "_", lbl.lower()).strip("_")[:60]
                rows.append(dict(edition=ed.key, kind="location", key=key, label=lbl, parent_key=cat,
                                 source="seed", source_ref=src, verification="unverified", data={"category": cat}))
    n = 0
    for row in rows:
        # Seed rows pick up label fixes from a newer seed; Blizzard-imported rows are never touched.
        ins = insert(ReferenceOption).values(**row)
        stmt = (ins.on_conflict_do_update(
            constraint="ux_ref_key", set_={"label": ins.excluded.label, "source_ref": ins.excluded.source_ref},
            where=(ReferenceOption.source == "seed") & (ReferenceOption.label != ins.excluded.label))
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


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def resolve(s: Session, edition: str, kind: str, text: str, parent: str | None = None) -> ReferenceOption | None:
    """Match typed text to a catalog row by key or label (case and spacing ignored)."""
    want = _norm(text)
    if not want:
        return None
    rows = options(s, edition, kind, parent, limit=500)
    for o in rows:
        if want in (_norm(o.key), _norm(o.label)):
            return o
    # "Skyborne" alone matches "Skyborne (High Order)"
    hits = [o for o in rows if _norm(o.label).split("_")[0] == want or _norm(o.key) == want.split("_")[0]]
    return hits[0] if len(hits) == 1 else None


def label(s: Session, edition: str, kind: str, key: str) -> str:
    """Display name for a saved race/class key ("maghar_orc" -> "Mag'har Orc"); falls back to the key."""
    hit = s.scalar(select(ReferenceOption.label).where(ReferenceOption.edition == edition,
                                                       ReferenceOption.kind == kind, ReferenceOption.key == key))
    return hit or key.replace("_", " ").title()


def races_for_class(s: Session, edition: str, faction: str, klass: str) -> set[str] | None:
    """Race keys of this faction that can be the class; None when the catalog has no combos."""
    rows = options(s, edition, "race_class", faction, limit=1000)
    if not rows:
        return None
    return {o.key.split(":", 1)[0] for o in rows if o.key.split(":", 1)[1] == klass}


def race_class_map(s: Session, edition: str) -> dict:
    """{faction: [{key, label, classes: [class keys]}]} plus class labels, for the character form."""
    combos: dict[tuple[str, str], list[str]] = {}
    for o in options(s, edition, "race_class", limit=1000):
        rk, ck = o.key.split(":", 1)
        combos.setdefault((o.parent_key, rk), []).append(ck)
    out: dict = {"classes": {o.key: o.label for o in options(s, edition, "class")}, "factions": {}}
    for o in options(s, edition, "race"):
        out["factions"].setdefault(o.parent_key, []).append(
            {"key": o.key, "label": o.label, "classes": sorted(combos.get((o.parent_key, o.key), []))})
    return out


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


def split_race_class(s: Session, edition: str, faction: str, text: str) -> tuple[str, str]:
    """Read one typed "race and class" line: "orc warrior", "Blood Elf Death Knight", "warrior orc", or just
    "orc" / "warrior". Returns (race text, class text); a part that wasn't typed comes back "".
    The catalog decides where the split goes, so multi-word races and classes both work; unknown words
    fall back to "last word is the class"."""
    words = text.split()
    if not words:
        return "", ""
    if len(words) == 1:
        w = words[0]
        if resolve(s, edition, "class", w) is not None and resolve(s, edition, "race", w, faction) is None:
            return "", w
        return w, ""
    def score(race: str, klass: str) -> int:
        return (2 if resolve(s, edition, "class", klass) is not None else 0) + \
               (1 if resolve(s, edition, "race", race, faction) is not None else 0)
    best = (" ".join(words[:-1]), words[-1])
    best_score = score(*best)
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        for cand in ((a, b), (b, a)):
            if (sc := score(*cand)) > best_score:
                best, best_score = cand, sc
    return best
