"""Which build is running: the commit and its date, for the page footer.

deploy/install.sh writes BUILD ("<short commit> <YYYY-MM-DD>") next to the release's venv. A checkout
without one asks git; anything else shows "local"."""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path

_LINE = re.compile(r"^([0-9a-f]{7,12}) (\d{4}-\d{2}-\d{2})$")


def parse(line: str) -> dict:
    m = _LINE.match((line or "").strip())
    if not m:
        return {"commit": "local", "date": None}
    try:
        d = date.fromisoformat(m.group(2))
    except ValueError:
        d = None
    return {"commit": m.group(1), "date": d}


@lru_cache(maxsize=1)
def info() -> dict:
    stamp = Path(sys.prefix).parent / "BUILD"
    try:
        if stamp.is_file():
            return parse(stamp.read_text(encoding="utf-8")[:64])
    except OSError:
        pass
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(["git", "-C", str(here), "log", "-1", "--format=%h %cs"], capture_output=True,
                             text=True, timeout=3, check=False).stdout
        return parse(out)
    except (OSError, subprocess.SubprocessError):
        return {"commit": "local", "date": None}
