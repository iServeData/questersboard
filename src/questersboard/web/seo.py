"""Search and link-preview metadata for public pages: description, canonical URL and Open Graph /
Twitter card tags. Everything comes from config or the published projection; nothing is site-specific."""

from __future__ import annotations

import re

from fastapi import Request

MAX_DESC = 200


def _clip(text: str, n: int = MAX_DESC) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) <= n:
        return t
    cut = t[: n - 1].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return cut + "…"


def _abs(base: str, url: str) -> str:
    return url if url.startswith("https://") else base + url


def meta(request: Request, *, description: str | None = None, image: dict | None = None,
         kind: str = "website", query: str = "", published=None, modified=None) -> dict:
    """Tags for one public page. `image` is {url, width, height, alt}; None uses the configured default."""
    cfg = request.app.state.web.cfg
    base = cfg["community"]["public_base_url"].rstrip("/")
    ch = cfg["chronicle"]
    if image is None and ch.get("social_image"):
        image = {"url": ch["social_image"], "width": ch.get("social_image_width") or 0,
                 "height": ch.get("social_image_height") or 0, "alt": ch.get("social_image_alt") or ""}
    img = None
    if image and image.get("url"):
        img = {"url": _abs(base, image["url"]), "width": int(image.get("width") or 0),
               "height": int(image.get("height") or 0), "alt": image.get("alt") or ""}
    return {"description": _clip(description or ch["description"]),
            "url": base + request.url.path + query, "site_name": cfg["branding"]["site_name"],
            "type": kind, "image": img,
            "published": published.isoformat() if published else "",
            "modified": modified.isoformat() if modified else ""}


def story_image(pc) -> dict | None:
    """The story's banner, else its first approved screenshot, else None (configured default)."""
    im = pc.banner or (pc.images[0] if pc.images else None)
    if not im or not im.get("url"):
        return None
    return {"url": im["url"], "width": im.get("width") or 0, "height": im.get("height") or 0,
            "alt": im.get("caption") or pc.title}
