"""Environment variables. The app was called WoWQuester before 2026-11; for one release the old
WOWQUESTER_* names are still read when the QUESTERSBOARD_* one isn't set (and old-name credentials)."""

from __future__ import annotations

import os

PREFIX = "QUESTERSBOARD_"
OLD_PREFIX = "WOWQUESTER_"          # pre-rename; remove after the 0.2 release
OLD_CREDENTIAL_PREFIX = "wowquester."


def getenv(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(PREFIX + name)
    if v is None:
        v = os.environ.get(OLD_PREFIX + name)
    return default if v is None else v
