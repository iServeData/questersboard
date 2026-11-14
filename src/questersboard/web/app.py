"""questersboard-web: member pages (Discord login) and public chronicle/schedule pages.

Security model
* Login: Discord OAuth2 authorization-code flow, scope `identify` only, single-use `state`
  (guest voice links alone also ask for `guilds.join`, to add the guest to the server; no session is made)
  bound to the pre-login session. No Discord access token is stored.
* Sessions: random 256-bit id in a Secure/HttpOnly/SameSite=Lax cookie; only its SHA-256
  is stored (web_sessions). Rotated at login. Server-side expiry.
* CSRF: per-session token required on every POST (form field or X-CSRF-Token header),
  plus an Origin/Referer same-origin check.
* Roles: fetched from the guild via the bot token at login and re-fetched before any
  privileged action when older than ROLE_TTL (so a removed role stops working quickly).
* Rate limits: per-IP and per-member buckets in PostgreSQL (no Redis).
* Public routes read only the public_* projection tables.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets as pysecrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select, text

from ..config import Config, ConfigHolder
from ..db import Database
from ..domain.actor import Actor
from ..domain.errors import DomainError, PermissionDenied
from ..config import RULESET_LABELS
from ..domain.timeutil import fmt_local, now, tz_choices, tz_label
from ..models import Member
from ..models import Session as WebSession

log = logging.getLogger("questersboard.web")
HERE = Path(__file__).resolve().parent
COOKIE = "wq_session"
ROLE_TTL = 300          # seconds before a privileged action re-checks Discord roles
DISCORD_API = "https://discord.com/api/v10"


def form_action(cfg) -> str:
    """Where a form may lead, including the redirects after it. Chromium browsers (Chrome, Edge, Opera)
    apply this to the redirect too, so the Battle.net login reached from the Continue with Battle.net form
    has to be listed: its sign-in host and the other hosts of the same service that its login passes through."""
    out = ["'self'", "https://discord.com"]
    bz = cfg["blizzard"]
    if bz.get("enabled") and bz.get("battlenet_link_enabled"):
        host = urlsplit(str(bz.get("oauth_base") or "")).hostname or ""
        if host:
            out.append(f"https://{host}")
            parent = host.split(".", 1)[1] if host.count(".") >= 2 else ""
            if parent:
                out.append(f"https://*.{parent}")
    return " ".join(out)


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


class DiscordOAuth:
    """Discord OAuth2 code exchange. Injected so tests use a FIXTURE transport."""

    def __init__(self, client_id: str, client_secret: str | None, http: httpx.Client | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self.http = http or httpx.Client(timeout=10)

    def authorize_url(self, redirect_uri: str, state: str, prompt: str = "none", scope: str = "identify") -> str:
        # prompt=none skips Discord's Authorize screen, but only for accounts that have authorized the app
        # before; Discord answers everyone else with error=access_denied, and the callback then asks again
        # with prompt=consent (the normal Authorize screen).
        return "https://discord.com/oauth2/authorize?" + urlencode(dict(
            client_id=self.client_id, response_type="code", scope=scope, state=state,
            redirect_uri=redirect_uri, prompt=prompt))

    def guest_identify(self, code: str, redirect_uri: str) -> tuple[str, str, str, str]:
        """Guest voice links (scope identify guilds.join): who they are, plus the token used once to add them
        to the server. The token is never stored."""
        return self.identify(code, redirect_uri, with_token=True)

    def identify(self, code: str, redirect_uri: str, with_token: bool = False):
        if not self.client_secret:
            raise PermissionDenied("Discord login is not configured on this server yet.")
        r = self.http.post(DISCORD_API + "/oauth2/token", data=dict(
            grant_type="authorization_code", code=code, redirect_uri=redirect_uri),
            auth=(self.client_id, self.client_secret))
        if r.status_code != 200:
            raise PermissionDenied("Discord login failed. Please try again.")
        tok = r.json()["access_token"]
        u = self.http.get(DISCORD_API + "/users/@me", headers={"Authorization": f"Bearer {tok}"})
        if u.status_code != 200:
            raise PermissionDenied("Discord login failed. Please try again.")
        d = u.json()
        out = (str(d["id"]), d.get("global_name") or d.get("username") or "", d.get("username") or "")
        return out + (tok,) if with_token else out


class Web:
    def __init__(self, holder: ConfigHolder, db: Database, oauth: DiscordOAuth | None, rest, blizzard=None):
        self.holder, self.db, self.oauth, self.rest = holder, db, oauth, rest
        self._bz = blizzard

    @property
    def bz(self):
        """Blizzard adapter (built lazily; lookups still refuse when [blizzard] enabled = false)."""
        if self._bz is None:
            from ..integrations.blizzard import Blizzard
            self._bz = Blizzard(self.cfg)
        return self._bz

    @property
    def cfg(self) -> Config:
        return self.holder.cfg

    # ---------------------------------------------------------------- sessions
    def load_session(self, request: Request) -> WebSession | None:
        raw = request.cookies.get(COOKIE)
        if not raw or len(raw) > 100:
            return None
        with self.db.tx() as s:
            row = s.get(WebSession, _h(raw))
            if row is None or row.expires_at < now():
                return None
            s.expunge(row)
            return row

    def new_session(self, response: Response, member_id: int | None, data: dict) -> WebSession:
        raw = pysecrets.token_urlsafe(32)
        hours = self.cfg["web"].get("session_hours", 72) if member_id else 1
        row = WebSession(id_hash=_h(raw), member_id=member_id, csrf=pysecrets.token_urlsafe(24), data=data,
                         expires_at=now() + timedelta(hours=hours))
        with self.db.tx() as s:
            s.add(row)
        response.set_cookie(COOKIE, raw, max_age=hours * 3600, httponly=True, samesite="lax",
                            secure=self.cfg["web"].get("cookie_secure", True), path="/")
        return row

    def forget_session(self, request: Request) -> None:
        """Delete the request's session row but leave cookies alone (a new session replaces it)."""
        raw = request.cookies.get(COOKIE)
        if raw:
            with self.db.tx() as s:
                s.execute(delete(WebSession).where(WebSession.id_hash == _h(raw)))

    def drop_session(self, request: Request, response: Response) -> None:
        raw = request.cookies.get(COOKIE)
        if raw:
            with self.db.tx() as s:
                s.execute(delete(WebSession).where(WebSession.id_hash == _h(raw)))
        response.delete_cookie(COOKIE, path="/")

    def update_session_data(self, sess: WebSession, **data) -> None:
        with self.db.tx() as s:
            row = s.get(WebSession, sess.id_hash)
            if row:
                row.data = {**row.data, **data}
                sess.data = row.data

    # ---------------------------------------------------------------- actors
    def actor(self, sess: WebSession | None, fresh: bool = False) -> Actor | None:
        if sess is None or sess.member_id is None:
            return None
        d = sess.data
        if fresh and time.time() - d.get("roles_at", 0) > ROLE_TTL and self.rest is not None:
            info = self.rest.member(self.cfg.guild_id, d["discord_id"])
            in_guild, roles, name = info
            joined = getattr(info, "joined_at", None)
            self.update_session_data(sess, roles=sorted(roles), in_guild=in_guild, roles_at=time.time(),
                                     name=name or d.get("name", ""),
                                     joined_at=joined.isoformat() if joined else d.get("joined_at"))
            d = sess.data
        return Actor(discord_id=d["discord_id"], source="web", role_ids=frozenset(d.get("roles", [])),
                     in_guild=d.get("in_guild", False), member_id=sess.member_id, display_name=d.get("name", ""),
                     username=d.get("username", ""), joined_at=_parse_when(d.get("joined_at")))

    def member_search(self, query: str) -> list[dict]:
        """Discord guild member search, used when a typed co-host name isn't a known QuestersBoard member."""
        if self.rest is None or not hasattr(self.rest, "search_members"):
            return []
        try:
            return self.rest.search_members(self.cfg.guild_id, query)
        except Exception:      # a lookup failure just means "not found"; the host can paste an ID instead
            log.warning("member search failed")
            return []

    # ---------------------------------------------------------------- rate limits
    def rate_ok(self, key: str, per_minute: int) -> bool:
        window = now().replace(second=0, microsecond=0)
        with self.db.tx() as s:
            n = s.execute(text(
                "INSERT INTO rate_buckets (key, window_start, count) VALUES (:k, :w, 1) "
                "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_buckets.count + 1 RETURNING count"),
                {"k": key[:120], "w": window}).scalar()
        return n <= per_minute

    def csrf_ok(self, request: Request, sess: WebSession | None, form_token: str | None) -> bool:
        if sess is None:
            return False
        tok = form_token or request.headers.get("x-csrf-token", "")
        if not tok or not hmac.compare_digest(tok, sess.csrf):
            return False
        base = urlparse(self.cfg["community"]["public_base_url"])
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin:
            o = urlparse(origin)
            if (o.scheme, o.netloc) != (base.scheme, base.netloc) and \
                    (o.netloc != request.url.netloc):       # local preview on 127.0.0.1
                return False
        return True

    def cleanup_sessions(self) -> int:
        days = self.cfg["retention"].get("session_days_after_expiry", 1)
        with self.db.tx() as s:
            return s.execute(text("DELETE FROM web_sessions WHERE expires_at < now() - make_interval(days => :d)"),
                             {"d": days}).rowcount or 0


def _parse_when(v) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def client_ip(request: Request) -> str:
    # Nginx sets X-Real-IP; the app only listens on a unix socket / localhost so it can't be spoofed externally.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def create_app(holder: ConfigHolder, db: Database, oauth: DiscordOAuth | None = None, rest=None,
               blizzard=None) -> FastAPI:
    from . import routes_member, routes_public

    cfg = holder.cfg
    app = FastAPI(title="QuestersBoard", docs_url=None, redoc_url=None, openapi_url=None)
    web = Web(holder, db, oauth, rest, blizzard)
    app.state.web = web
    tpl = Jinja2Templates(directory=str(HERE / "templates"))
    # Cache-busting: ?v= changes whenever a static file changes, so phones don't keep an old script.
    sv = hashlib.sha256(b"".join(f.read_bytes() for f in sorted((HERE / "static").rglob("*")) if f.is_file())).hexdigest()[:10]
    tpl.env.globals.update(fmt_local=fmt_local, static_v=sv, tz_choices=tz_choices, tz_label=tz_label,
                           ruleset_label=lambda r: RULESET_LABELS.get(r or "", (r or "").replace("_", " ").title()))
    from ..domain import profiles as _profiles
    sil = frozenset(f.stem for f in (HERE / "static" / "silhouettes").glob("*.svg"))
    role_art = frozenset(f.stem for f in (HERE / "static" / "roles").glob("*.svg"))
    # The event page shows the same role icons as the Discord post, for roles that have one set up.
    tpl.env.globals["role_icon"] = lambda key: bool(
        key in role_art and key in holder.cfg["event_posts"].get("role_icons", {}))
    from .. import build as _build
    from . import account as _account
    tpl.env.globals.update(build=_build.info(), ago=_account.ago)
    tpl.env.globals.update(portrait_state=_profiles.portrait_state,
                           silhouette=lambda race: _profiles.silhouette_key(race, sil))
    tpl.env.autoescape = True
    app.state.tpl = tpl
    member = cfg["web"]["member_path"].rstrip("/")
    app.mount(member + "/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # Pictures and page files (gear and talent icons, portraits, screenshots, CSS/JS) come many to a page:
    # a character's talent trees alone can be over 100 icons. They count against their own per-address
    # limit ([rate_limits] icon_requests_per_minute), so looking at a few characters never runs into the
    # page limit (web_requests_per_minute).
    chron = cfg["chronicle"]["public_path"].rstrip("/")
    asset_re = re.compile("^(?:" + re.escape(member) + r"/(?:static/|uploads/|characters/\d+/(?:icon|portrait)/)"
                          "|" + re.escape(chron) + r"/media/)")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        rl = holder.cfg["rate_limits"]
        path = request.url.path
        if asset_re.match(path):
            bucket, per_min = "ipa:", rl.get("icon_requests_per_minute", 600)
        else:
            bucket, per_min = "ip:", rl.get("web_requests_per_minute", 120)
        if path != "/healthz" and not web.rate_ok(bucket + client_ip(request), per_min):
            return PlainTextResponse("Too many requests. Please slow down.", status_code=429,
                                     headers={"Retry-After": "60"})
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                                f"form-action {form_action(holder.cfg)}; frame-ancestors 'none'; base-uri 'none'")
        return resp

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)
        ctx = {"title": "Can't do that", "message": exc.message}
        if request.method == "POST":
            # Offer a real link back to the form page (same site only); the page script restores
            # what the player typed there, so nothing has to be entered twice.
            ref = urlsplit(request.headers.get("referer", ""))
            base = urlsplit(holder.cfg["community"]["public_base_url"])
            if ref.netloc and ref.netloc in (base.netloc, request.url.netloc) and ref.path.startswith("/") \
                    and not ref.path.startswith("//"):
                ctx["back_url"] = ref.path + (("?" + ref.query) if ref.query else "")
            ctx["restore_action"] = request.url.path
        return routes_member.render(request, "message.html", ctx, status=exc.status)

    @app.get("/healthz")
    def healthz():
        try:
            with db.tx() as s:
                s.execute(text("select 1"))
            return {"status": "ok"}
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "db_unavailable"}, status_code=503)

    routes_member.install_login_redirect(app, cfg)
    app.include_router(routes_public.router(cfg))
    app.include_router(routes_member.router(cfg))
    return app


def main() -> int:  # pragma: no cover - service entry point
    import sys

    import uvicorn

    from ..config import ConfigError, load
    from ..db import make_engine
    from ..integrations import discord_rest as dr
    from ..secrets import SecretError, get_secret, setup_logging
    setup_logging()
    try:
        cfg = load()
    except ConfigError as exc:
        log.error("invalid configuration: %s", exc)
        return 78
    db = Database(make_engine(cfg["database"]["app_url"], "db-app-password", cfg["database"].get("pool_size", 5),
                              "questersboard-web"))
    sec = get_secret("discord-client-secret", required=False)
    oauth = DiscordOAuth(cfg["discord"]["application_id"], sec.reveal() if sec else None)
    try:
        rest = dr.DiscordRest()
    except SecretError:
        rest = None
        log.warning("discord-bot-token not installed: logins can't verify guild membership")
    holder = ConfigHolder(cfg)
    app = create_app(holder, db, oauth, rest)
    bind = cfg["web"]["bind"]
    if bind.startswith("fd:"):            # socket passed by questersboard-web.socket (systemd)
        kw = {"fd": int(bind[3:])}
    elif bind.startswith("unix:"):
        kw = {"uds": bind[5:]}
    else:
        kw = {"host": bind.rsplit(":", 1)[0], "port": int(bind.rsplit(":", 1)[1])}
    uvicorn.run(app, proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_config=None, **kw)
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["create_app", "DiscordOAuth", "Web", "select", "Member", "HTMLResponse", "RedirectResponse"]
