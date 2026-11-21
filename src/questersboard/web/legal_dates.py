"""When each legal page last changed, kept next to a fingerprint of its template.

The page shows the later of this date and [legal] effective_date, so an operator can still date a change
they make in the config. tests/test_legal_dates.py fails when privacy.html or terms.html changes without
a new date and fingerprint here: run `python -m questersboard.web.legal_dates` for the new fingerprints.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

TEMPLATES = Path(__file__).parent / "templates"

# page: (last updated, sha256 of templates/<page>.html)
PAGES = {
    "privacy": ("2026-11-20", "b345152a41be093c90418b7126b88c9c6ecf507e6b176b030aade52b5204729c"),
    "terms": ("2026-11-16", "33c77374d80deae947826690755c1441357cc269f9114489aaf6db6119d58b4f"),
}


def fingerprint(page: str) -> str:
    return hashlib.sha256((TEMPLATES / f"{page}.html").read_bytes()).hexdigest()


def legal_date(page: str, configured: str = "") -> str:
    """The later of the page's code date and the config's effective date (both YYYY-MM-DD)."""
    return max(PAGES[page][0], configured or "")


if __name__ == "__main__":
    for name in PAGES:
        print(f"{name}: {fingerprint(name)}")
