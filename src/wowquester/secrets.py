"""Secret handling: systemd credentials first, environment variables for local development.

Credential names (files in $CREDENTIALS_DIRECTORY, imported as wowquester.<name>):
  discord-bot-token, discord-client-secret, field-key,
  db-app-password, db-worker-password, db-admin-password (CLI only),
  blizzard-client-id, blizzard-client-secret, ai-api-key
Environment fallback for local development: WOWQUESTER_SECRET_<NAME> (dashes -> underscores).
Values are wrapped in Secret so accidental logging shows a fingerprint, never the value.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

PREFIX = "wowquester."

_REDACT = [
    re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}"),  # Discord bot token shape
    re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(bearer|bot|basic)\s+[^\s'\",}]+"),
    re.compile(r"(?i)(password=)[^&\s]+"),
    re.compile(r"(?i)(client_secret=)[^&\s]+"),
    re.compile(r"(?i)(access_token['\"]?\s*[:=]\s*['\"]?)[^\s'\",}&]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
]


class SecretError(RuntimeError):
    pass


class Secret:
    __slots__ = ("_v",)

    def __init__(self, value: str):
        self._v = value

    def reveal(self) -> str:
        return self._v

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self._v.encode()).hexdigest()[:10]

    def __repr__(self) -> str:
        return f"Secret(fp={self.fingerprint})"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._v)


def redact(text: str) -> str:
    for pat in _REDACT:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        red = redact(msg)
        if red != msg:
            record.msg, record.args = red, None
        return True


def get_secret(name: str, required: bool = True) -> Secret | None:
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        for candidate in (PREFIX + name, name):
            p = Path(cred_dir) / candidate
            if p.is_file():
                v = p.read_text(encoding="utf-8").strip()
                if v:
                    return Secret(v)
    env = os.environ.get("WOWQUESTER_SECRET_" + name.upper().replace("-", "_"))
    if env:
        return Secret(env.strip())
    if required:
        raise SecretError(f"credential {name!r} is not installed")
    return None


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    f = RedactingFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(f)
