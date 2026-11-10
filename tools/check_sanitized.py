#!/usr/bin/env python3
"""Refuse to commit deployment-specific or secret material.

Checks every tracked/staged text file for:
  * Discord snowflake IDs (17-20 digit numbers)      -> IDs belong in /etc/questersboard/config.toml
  * Discord bot tokens, Bearer tokens, private keys, AWS-style keys
  * Passwords inside URLs
  * Optional deny-list words from $QUESTERSBOARD_DENYLIST (a file OUTSIDE the repo,
    one word per line), e.g. community names, domains, host names.

Run: python tools/check_sanitized.py   (also run by the test suite)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = {
    "discord snowflake id": re.compile(r"(?<![\w.])\d{17,20}(?![\w.])"),
    "discord bot token": re.compile(r"[MNO][A-Za-z\d_-]{23,25}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer token": re.compile(r"Bearer [A-Za-z0-9._~+/-]{20,}"),
    "aws key": re.compile(r"AKIA[0-9A-Z]{16}"),
    # a literal password in a URL; ${ENV_VAR} placeholders are fine
    "password in url": re.compile(r"[a-z+]+://[^\s/:@]+:(?!\$\{?[A-Z_]+\}?@)[^\s/@]{3,}@"),
}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2", ".lock"}
# Lines explicitly marked as test fixtures may contain fake IDs.
ALLOW_MARK = "sanitize: fixture"


def files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, check=True,
                             capture_output=True, text=True).stdout.split()
        return [ROOT / f for f in out]
    except Exception:
        return [p for p in ROOT.rglob("*") if p.is_file() and ".venv" not in p.parts and ".git" not in p.parts]


def main() -> int:
    deny = []
    dl = os.environ.get("QUESTERSBOARD_DENYLIST") or os.environ.get("WOWQUESTER_DENYLIST")
    if dl and Path(dl).is_file():
        deny = [w.strip().lower() for w in Path(dl).read_text().splitlines() if w.strip() and not w.startswith("#")]
    problems = []
    for p in files():
        if p.suffix.lower() in SKIP_SUFFIX or not p.is_file() or p.name == "check_sanitized.py":
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARK in line:
                continue
            for label, rx in PATTERNS.items():
                if rx.search(line):
                    problems.append(f"{p.relative_to(ROOT)}:{n}: {label}")
            low = line.lower()
            for w in deny:
                if w in low:
                    problems.append(f"{p.relative_to(ROOT)}:{n}: deny-listed word")
    for pr in problems:
        print(pr)
    print(f"check_sanitized: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
