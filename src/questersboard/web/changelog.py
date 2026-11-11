"""The members' changelog: web/changelog.toml, checked and sorted newest first."""
from __future__ import annotations

import tomllib
from datetime import date
from functools import lru_cache
from pathlib import Path

AREAS = ("Characters", "Guild", "Events", "Voice", "Chronicle", "Site")
KINDS = {"new": "New", "improved": "Improved", "fixed": "Fixed"}
FILE = Path(__file__).resolve().parent / "changelog.toml"


def parse(text: str) -> list[dict]:
    raw = tomllib.loads(text).get("release", [])
    out = []
    for r in raw:
        d = r.get("date")
        d = d if isinstance(d, date) else date.fromisoformat(str(d))
        changes = []
        for c in r.get("change", []):
            if c.get("area") not in AREAS or c.get("kind") not in KINDS or not str(c.get("text", "")).strip():
                raise ValueError(f"changelog {d}: each change needs an area {AREAS}, a kind {tuple(KINDS)} and text")
            changes.append({"area": c["area"], "kind": c["kind"], "kind_label": KINDS[c["kind"]],
                            "text": str(c["text"]).strip()})
        if not changes:
            raise ValueError(f"changelog {d}: a release needs at least one change")
        out.append({"date": d, "title": str(r.get("title", "")).strip(), "changes": changes,
                    "areas": [a for a in AREAS if any(c["area"] == a for c in changes)]})
    out.sort(key=lambda r: r["date"], reverse=True)
    for i, r in enumerate(out):
        r["since"] = out[i + 1]["date"] if i + 1 < len(out) else None
        r["id"] = "r" + r["date"].isoformat()
    if len({r["id"] for r in out}) != len(out):
        raise ValueError("changelog: one release per date")
    return out


@lru_cache(maxsize=1)
def releases() -> tuple:
    return tuple(parse(FILE.read_text(encoding="utf-8")))
