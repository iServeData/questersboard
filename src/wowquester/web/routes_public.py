"""Public routes. They read ONLY public_chronicle / public_events (+ calendar token lookup, and
status.json's service_heartbeats / public_event_voice, which hold no member data).

Publishing a chronicle entry updates the projection in the same transaction as the
approval, so these pages, feeds and the site widget update without commits, Hugo
builds or deployments.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import select

from ..config import Config
from ..domain import calendar, status
from ..domain.errors import NotFound
from ..domain.timeutil import now
from ..models import PublicChronicle, PublicEvent

OUTCOME = {"completed": "Completed", "partial": "Partly completed", "not_completed": "Not completed",
           "social": "Social", "unknown": "Outcome not recorded"}
SLUG = re.compile(r"^[a-z0-9-]{1,120}$")


def _cache(cfg: Config) -> dict:
    return {"Cache-Control": f"public, max-age={cfg['feeds'].get('cache_seconds', 300)}"}


def public_story(pc: PublicChronicle, base: str, path: str) -> dict:
    return dict(id=str(pc.public_id), url=f"{base}{path}/{pc.slug}/", title=pc.title, summary=pc.summary,
                content_html=pc.body_html, date_published=pc.published_at.isoformat(),
                date_modified=pc.updated_at.isoformat(), event_date=pc.event_date.isoformat(),
                activity_type=pc.activity_type, edition=pc.edition_name, outcome=OUTCOME.get(pc.outcome, ""),
                image=(base + pc.images[0]["url"]) if pc.images else None, ai_assisted=pc.ai_assisted)


def upcoming(s, cfg: Config, limit: int = 20) -> list[dict]:
    rows = s.scalars(select(PublicEvent).where(status.upcoming_filter(now()))
                     .order_by(PublicEvent.start_at).limit(limit))
    out = []
    for pe in rows:
        out.append(dict(id=str(pe.public_id), title=pe.title, activity_type=pe.activity_type,
                        edition=pe.edition_name, ruleset=(pe.ruleset or "").upper() or None,
                        start=pe.start_at.isoformat(), end=pe.end_at.isoformat(), timezone=pe.timezone,
                        state=pe.state, location=pe.location_label or None, meet_at=pe.muster_label or None,
                        roles=pe.roles, signup_policy=pe.signup_policy, exclusive=pe.exclusive,
                        url=f"{cfg['community']['public_base_url'].rstrip('/')}"
                            f"{cfg['web']['member_path'].rstrip('/')}/events/{pe.public_id}"))
    return out


def router(cfg: Config) -> APIRouter:
    from .routes_member import render
    r = APIRouter()
    cp = cfg["chronicle"]["public_path"].rstrip("/")
    mp = cfg["web"]["member_path"].rstrip("/")

    def web(request: Request):
        return request.app.state.web

    def base(request: Request) -> str:
        return web(request).cfg["community"]["public_base_url"].rstrip("/")

    # ------------------------------------------------------------- chronicle
    @r.get(cp + "/")
    def archive(request: Request, page: int = 1):
        page = max(1, min(page, 500))
        n = 20
        with web(request).db.tx() as s:
            items = list(s.scalars(select(PublicChronicle).order_by(PublicChronicle.event_date.desc())
                                   .offset((page - 1) * n).limit(n + 1)))
        return render(request, "chronicle_archive.html",
                      {"title": "Chronicle", "items": items[:n], "page": page, "more": len(items) > n,
                       "outcome": OUTCOME, "path": cp}, public=True)

    @r.get(cp + "/{slug}/")
    def story(request: Request, slug: str):
        if not SLUG.match(slug):
            raise NotFound("No such story.")
        with web(request).db.tx() as s:
            pc = s.scalar(select(PublicChronicle).where(PublicChronicle.slug == slug))
        if pc is None:
            raise NotFound("That story isn't published (or was removed).")
        return render(request, "chronicle_story.html", {"title": pc.title, "pc": pc, "outcome": OUTCOME,
                                                        "path": cp}, public=True)

    @r.get(cp + "/media/{name}")
    def media(request: Request, name: str):
        m = re.match(r"^([0-9a-f-]{36})\.webp$", name)
        if not m:
            raise NotFound("No such image.")
        url = f"{cp}/media/{m.group(1)}.webp"
        w = web(request)
        with w.db.tx() as s:
            # only images that are part of a published projection are served
            hit = s.scalar(select(PublicChronicle.public_id).where(
                PublicChronicle.images.contains([{"url": url}])).limit(1))
        if hit is None:
            raise NotFound("No such image.")
        f = Path(w.cfg["uploads"]["storage_dir"]) / f"{uuid.UUID(m.group(1))}.webp"
        if not f.is_file():
            raise NotFound("No such image.")
        return FileResponse(f, media_type="image/webp", headers=_cache(w.cfg))

    def _stories(request, limit=None):
        w = web(request)
        with w.db.tx() as s:
            return list(s.scalars(select(PublicChronicle).order_by(PublicChronicle.published_at.desc())
                                  .limit(limit or w.cfg["feeds"].get("items", 20))))

    @r.get(cp + "/feed.json")
    def feed_json(request: Request):
        w, b = web(request), base(request)
        items = [public_story(pc, b, cp) for pc in _stories(request)]
        return JSONResponse({"version": "https://jsonfeed.org/version/1.1",
                             "title": f"{w.cfg['branding']['site_name']} WoW Chronicle",
                             "home_page_url": f"{b}{cp}/", "feed_url": f"{b}{cp}/feed.json",
                             "items": [{k: v for k, v in i.items() if v is not None} for i in items]},
                            headers=_cache(w.cfg), media_type="application/feed+json")

    @r.get(cp + "/latest.json")
    def latest(request: Request, n: int = 3):
        w, b = web(request), base(request)
        items = [public_story(pc, b, cp) for pc in _stories(request, max(1, min(n, 10)))]
        for i in items:
            i.pop("content_html")
        return JSONResponse({"items": items}, headers={**_cache(w.cfg), "Access-Control-Allow-Origin": b})

    @r.get(cp + "/feed.atom")
    def atom(request: Request):
        w, b = web(request), base(request)
        st = _stories(request)
        upd = (st[0].updated_at if st else now()).isoformat()
        x = ['<?xml version="1.0" encoding="utf-8"?>', '<feed xmlns="http://www.w3.org/2005/Atom">',
             f"<title>{escape(w.cfg['branding']['site_name'])} WoW Chronicle</title>",
             f'<link href="{b}{cp}/"/>', f'<link rel="self" href="{b}{cp}/feed.atom"/>',
             f"<id>{b}{cp}/</id>", f"<updated>{upd}</updated>"]
        for pc in st:
            x += ["<entry>", f"<title>{escape(pc.title)}</title>", f'<link href="{b}{cp}/{pc.slug}/"/>',
                  f"<id>urn:uuid:{pc.public_id}</id>", f"<published>{pc.published_at.isoformat()}</published>",
                  f"<updated>{pc.updated_at.isoformat()}</updated>", f"<summary>{escape(pc.summary)}</summary>",
                  f'<content type="html">{escape(pc.body_html)}</content>', "</entry>"]
        x.append("</feed>")
        return Response("\n".join(x), media_type="application/atom+xml", headers=_cache(w.cfg))

    @r.get(cp + "/feed.xml")
    def rss(request: Request):
        w, b = web(request), base(request)
        x = ['<?xml version="1.0" encoding="utf-8"?>', '<rss version="2.0"><channel>',
             f"<title>{escape(w.cfg['branding']['site_name'])} WoW Chronicle</title>", f"<link>{b}{cp}/</link>",
             "<description>Stories from community WoW events</description>"]
        for pc in _stories(request):
            x += ["<item>", f"<title>{escape(pc.title)}</title>", f"<link>{b}{cp}/{pc.slug}/</link>",
                  f'<guid isPermaLink="false">{pc.public_id}</guid>',
                  f"<pubDate>{format_datetime(pc.published_at)}</pubDate>",
                  f"<description>{escape(pc.summary)}</description>", "</item>"]
        x.append("</channel></rss>")
        return Response("\n".join(x), media_type="application/rss+xml", headers=_cache(w.cfg))

    # ------------------------------------------------------------- schedule
    @r.get(mp + "/upcoming.json")
    def upcoming_json(request: Request, n: int = 10):
        w = web(request)
        with w.db.tx() as s:
            items = upcoming(s, w.cfg, max(1, min(n, 50)))
        return JSONResponse({"generated_at": now().isoformat(), "items": items},
                            headers={"Cache-Control": "public, max-age=60",
                                     "Access-Control-Allow-Origin": base(request)})

    @r.get(mp + "/status.json")
    def status_json(request: Request):
        w = web(request)
        with w.db.tx() as s:
            body = status.snapshot(s, w.cfg)
        return JSONResponse(body, headers={"Cache-Control": f"public, max-age={w.cfg['status']['cache_seconds']}",
                                           "Access-Control-Allow-Origin": base(request)})

    @r.get(mp + "/privacy")
    def privacy(request: Request):
        return render(request, "privacy.html", {"title": "Privacy"}, public=True)

    @r.get(mp + "/terms")
    def terms(request: Request):
        return render(request, "terms.html", {"title": "Terms of use"}, public=True)

    @r.get(mp + "/calendar.ics")
    def public_ics(request: Request):
        w = web(request)
        if not w.cfg["calendar"].get("public_enabled", True):
            raise NotFound("The public calendar is turned off.")
        with w.db.tx() as s:
            body = calendar.ics(w.cfg, f"{w.cfg['branding']['site_name']} WoW events", calendar.public_items(s, w.cfg))
        return Response(body, media_type="text/calendar; charset=utf-8", headers={"Cache-Control": "max-age=300"})

    @r.get(mp + "/calendar/{token}.ics")
    def personal_ics(request: Request, token: str):
        w = web(request)
        if not re.match(r"^[A-Za-z0-9_-]{20,80}$", token):
            raise NotFound("Unknown calendar link.")
        with w.db.tx() as s:
            mid = calendar.member_for_token(s, token)
            if mid is None:
                raise NotFound("This calendar link was revoked or never existed.")
            body = calendar.ics(w.cfg, "My WoW events", calendar.personal_items(s, w.cfg, mid))
        return Response(body, media_type="text/calendar; charset=utf-8",
                        headers={"Cache-Control": "private, max-age=300", "Referrer-Policy": "no-referrer"})

    return r


__all__ = ["router", "upcoming", "timedelta"]
