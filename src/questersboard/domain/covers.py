"""Event cover art: the banner on the Discord post, the event page and (by default) the Chronicle story.

Covers live in <[uploads] storage_dir>/covers. Uploaded covers are re-encoded to WebP (which strips
EXIF/GPS and anything appended to the file) and named <uuid>.webp. The community's own default art
is a file the operator copies into the same folder and names in [event_posts] default_cover (or per
activity type in [event_posts.activity_covers]). Cover art isn't personal data, so covers are served
publicly at <member_path>/covers/<name>: Discord has to fetch them to show the image.
"""

from __future__ import annotations

import io
import os
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from ..config import COVER_NAME, Config
from ..models import Event
from .errors import Invalid

MAX_PIXELS = 40_000_000
MAX_SIDE = 1600              # Discord shows embed images at most ~520 px wide; 1600 keeps the web banner sharp
MEDIA = {"webp": "image/webp", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


def folder(cfg: Config) -> Path:
    return Path(cfg["uploads"]["storage_dir"]) / "covers"


def save(cfg: Config, data: bytes) -> str:
    """Validate and re-encode an uploaded image; returns the new cover's file name."""
    up = cfg["uploads"]
    if not data:
        raise Invalid("Choose an image to upload.")
    if len(data) > up.get("max_bytes", 5_000_000):
        raise Invalid("That image is too large.")
    allowed = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
    allowed = {v for k, v in allowed.items() if k in up.get("allowed_types", list(allowed))}
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            if fmt not in allowed:
                raise Invalid("Cover images must be PNG, JPEG or WebP.")
            if im.width * im.height > MAX_PIXELS:
                raise Invalid("That image has too many pixels.")
            im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            im.save(out, "WEBP", quality=85, method=4)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError):
        raise Invalid("That file isn't a readable image.") from None
    name = f"{uuid.uuid4()}.webp"
    d = folder(cfg)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.tmp"
    tmp.write_bytes(out.getvalue())
    os.chmod(tmp, 0o640)
    tmp.replace(d / name)
    return name


def effective(cfg: Config, ev: Event) -> str | None:
    """The cover an event shows: its own, else the activity's default, else the community default."""
    ep = cfg["event_posts"]
    for name in (ev.cover_image, (ep.get("activity_covers") or {}).get(ev.activity_type), ep.get("default_cover")):
        if name and COVER_NAME.match(name) and (folder(cfg) / name).is_file():
            return name
    return None


def path_url(cfg: Config, name: str) -> str:
    return f"{cfg['web']['member_path'].rstrip('/')}/covers/{name}"


def url(cfg: Config, ev: Event, absolute: bool = False) -> str | None:
    name = effective(cfg, ev)
    if name is None:
        return None
    p = path_url(cfg, name)
    return cfg["community"]["public_base_url"].rstrip("/") + p if absolute else p


def file_for(cfg: Config, name: str) -> tuple[Path, str] | None:
    if not COVER_NAME.match(name):
        return None
    f = folder(cfg) / name
    if not f.is_file():
        return None
    return f, MEDIA[name.rsplit(".", 1)[1].lower()]
