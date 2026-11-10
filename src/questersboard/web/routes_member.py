"""Member routes under [web] member_path. Every write goes through the same domain
services the bot uses, so permissions and conflict rules are identical everywhere."""

from __future__ import annotations

import logging
import re
import secrets as pysecrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select

from ..config import Config
from ..domain import (calendar, catalog, chronicle, covers, eventtext, erasure, events, people, permissions, recommendations, recurrence, roster,
                      scheduling, signups)
from ..domain.errors import EditionLocked, Invalid, NeedsLeave, NotFound, PermissionDenied
from ..domain.events import ACTIVITY_TYPES
from ..domain.timeutil import as_local, now, resolve_local
from ..domain import audit, voice
from ..models import (Attendance, Character, ChronicleEntry, ChronicleUpload, Event, EventCohost, Member, Signup)
from . import seo

log = logging.getLogger("questersboard.web")
FIELDS_TEXT = ("title", "description", "eligibility", "objective", "difficulty")


def render(request: Request, name: str, ctx: dict, status: int = 200, public: bool = False) -> HTMLResponse:
    w = request.app.state.web
    sess = getattr(request.state, "sess", None) if not public else None
    if sess is None and not public:
        sess = w.load_session(request)
    a = w.actor(sess) if sess else None
    cfg = w.cfg
    base = dict(request=request, cfg=cfg, b=cfg["branding"], mp=cfg["web"]["member_path"].rstrip("/"),
                cp=cfg["chronicle"]["public_path"].rstrip("/"), actor=a, csrf=sess.csrf if sess else "",
                can=(lambda cap: a is not None and permissions.has(cfg, a, cap)), public=public,
                probation=(permissions.probation_message(cfg) if a is not None and cfg["publishing"]["open_to_all"]
                           and permissions.on_probation(cfg, a) else ""))
    base.update(ctx)
    base["indexable"] = public and status == 200 and not ctx.get("noindex")   # errors, notices, guest links: no
    base["preview"] = bool(ctx.get("preview") and ctx.get("seo"))   # link-preview card only, never indexed
    if not base["indexable"] and not base["preview"]:
        base["seo"] = None
    elif "seo" not in base:
        base["seo"] = seo.meta(request)
    return request.app.state.tpl.TemplateResponse(request, name, base, status_code=status)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _local_next(value: str, mp: str) -> str:
    """Where to send someone after login: only a path inside the member area, never another site.
    Rebuilt as the constant member prefix + the checked remainder, so the host can't come from input."""
    prefix = mp + "/"
    rest = value[len(prefix):] if isinstance(value, str) and value.startswith(prefix) else ""
    if not re.fullmatch(r"[A-Za-z0-9_./?=&%-]*", rest) or rest.startswith("/") or "//" in rest or ".." in rest:
        rest = ""
    return prefix + rest


def _bool(form, k) -> bool:
    return str(form.get(k, "")).lower() in ("1", "on", "true", "yes")


def _local_dt(date_s: str, time_s: str, tz: str, fold: str | None = None) -> datetime:
    try:
        local = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise Invalid("Enter the date as YYYY-MM-DD and the time as HH:MM.") from None
    return resolve_local(local, tz, int(fold) if fold in ("0", "1") else None)


MAX_ROLE_ROWS = 12


def _roles_from_form(form) -> list[dict]:
    out = []
    for i in range(MAX_ROLE_ROWS):
        key = str(form.get(f"role_key_{i}", "")).strip()
        label = str(form.get(f"role_label_{i}", "")).strip()
        qty = str(form.get(f"role_qty_{i}", "")).strip()
        if not (key or label or qty):
            continue
        name = key or label
        if not name:
            raise Invalid(f"Group role row {i + 1} has a number but no role. Enter a role, or clear the number.")
        if not qty:
            raise Invalid(f"Enter how many {name} you need, or clear that row.")
        out.append({"key": name.lower().replace(" ", "_"), "label": label, "quantity": qty})
    return out


def copy_suggestion(ev, now: datetime | None = None) -> tuple[str, str]:
    """Date and start time for "Copy to a new draft": the same local time a week on, moved forward
    in whole weeks until it's in the future. A copy at the very same time would clash with the original."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(ev.timezone)
    local = ev.start_at.astimezone(tz)
    now = now or datetime.now(tz)
    day = local.date() + timedelta(days=7)
    while datetime.combine(day, local.time(), tz) <= now:
        day += timedelta(days=7)
    return day.isoformat(), local.strftime("%H:%M")


def current_local(ev) -> tuple[str, str, int]:
    """The event's date, start (HH:MM) and length in minutes, as the edit form shows them."""
    from zoneinfo import ZoneInfo
    local = ev.start_at.astimezone(ZoneInfo(ev.timezone))
    return local.date().isoformat(), local.strftime("%H:%M"), int((ev.end_at - ev.start_at).total_seconds() // 60)


def event_data_from_form(cfg: Config, form, partial: bool = False) -> dict:
    tz = form.get("timezone") or cfg["community"]["default_timezone"]
    d: dict = {"timezone": tz}
    for k in ("edition", "region", "ruleset", "faction", "activity_type", "signup_policy", *FIELDS_TEXT):
        if form.get(k) is not None:
            d[k] = form.get(k)
    if form.get("realm_slug"):
        d["realm_slug"] = form.get("realm_slug").strip().lower()
    if form.get("date") and form.get("start_time"):
        start = _local_dt(form["date"], form["start_time"], tz, form.get("fold"))
        mins = int(form.get("duration_minutes") or 120)
        d["start_at"], d["end_at"] = start, start + timedelta(minutes=mins)
    d["activity_location"] = {"label": form.get("location", ""), "zone": form.get("location_zone", ""),
                              "ref_key": form.get("location_ref", ""), "link": form.get("location_link", "")}
    d["muster_location"] = {"label": form.get("muster", "")}
    for k in ("level_min", "level_max", "host_character_id"):
        if form.get(k) is not None:
            d[k] = form.get(k) or None
    if form.get("remove_cover"):
        d["cover_image"] = None
    if form.get("participant_limit") is not None:
        d["participant_limit"] = form.get("participant_limit") or None
    for k in ("late_join", "chronicle_enabled", "public_listing"):
        d[k] = _bool(form, k)
    if form.get("has_exclusive"):          # field only rendered for the exclusive-designator role
        d["exclusive"] = _bool(form, "exclusive")
    d["roles"] = _roles_from_form(form)
    if form.get("cohosts") is not None:    # field only rendered for the creator / moderators
        d["cohosts"] = people.split(form.get("cohosts", ""))   # Discord usernames or IDs, comma separated
    return d


async def _take_cover(cfg: Config, form, data: dict) -> None:
    """A new cover upload (re-encoded and stored now), or one already stored on the "Before saving" page."""
    f = form.get("cover")
    if f is not None and not isinstance(f, str) and getattr(f, "filename", ""):
        blob = await f.read()
        if blob:
            data["cover_image"] = covers.save(cfg, blob)
            return
    saved = form.get("cover_saved")
    if isinstance(saved, str) and covers.file_for(cfg, saved):
        data["cover_image"] = saved


def router(cfg: Config) -> APIRouter:
    r = APIRouter()
    mp = cfg["web"]["member_path"].rstrip("/")

    def W(request):
        return request.app.state.web

    def session_or_login(request: Request, fresh: bool = False, bucket: str = "m"):
        """bucket: "m" for pages; "i" for icons, which come many to a page and have their own limit
        ([rate_limits] icon_requests_per_minute)."""
        w = W(request)
        sess = w.load_session(request)
        request.state.sess = sess
        a = w.actor(sess, fresh=fresh)
        if a is None:
            raise _Login()
        per_min = (w.cfg["rate_limits"].get("icon_requests_per_minute", 600) if bucket == "i"
                   else w.cfg["rate_limits"].get("web_requests_per_minute", 120))
        if not w.rate_ok(f"{bucket}:{a.discord_id}", per_min):
            raise PermissionDenied("Too many requests. Please wait a minute.")
        return sess, a

    async def post_guard(request: Request, fresh: bool = True):
        """POST: valid session + CSRF + action rate limit + fresh roles."""
        w = W(request)
        form = await request.form()
        sess = w.load_session(request)
        request.state.sess = sess
        if not w.csrf_ok(request, sess, form.get("csrf")):
            raise PermissionDenied("Your session expired or the form was stale. Reload the page and try again.")
        a = w.actor(sess, fresh=fresh)
        if a is None:
            raise _Login()
        if not w.rate_ok(f"act:{a.discord_id}", w.cfg["rate_limits"].get("actions_per_minute", 30)):
            raise PermissionDenied("You're doing that too fast. Wait a minute and try again.")
        return form, a

    def ev_by_public(s, public_id: str) -> Event:
        try:
            pid = uuid.UUID(public_id)
        except ValueError:
            raise NotFound("Event not found.") from None
        ev = s.scalar(select(Event).where(Event.public_id == pid))
        if ev is None:
            raise NotFound("Event not found.")
        return ev

    # ------------------------------------------------------------- login
    @r.get(mp + "/login")
    def login(request: Request, next: str = ""):
        w = W(request)
        if w.oauth is None:
            return render(request, "message.html", {"title": "Login unavailable",
                                                    "message": "Discord login isn't configured on this server."})
        return _start_login(w, _local_next(next, mp), "none")

    def _start_login(w, safe_next: str, prompt: str):
        state = pysecrets.token_urlsafe(24)
        resp = _redirect("about:blank")
        w.new_session(resp, None, {"oauth_state": state, "next": safe_next, "prompt": prompt})
        resp.headers["location"] = w.oauth.authorize_url(
            w.cfg["community"]["public_base_url"].rstrip("/") + mp + "/oauth/callback", state, prompt=prompt)
        return resp

    @r.get(mp + "/oauth/callback")
    def callback(request: Request, code: str = "", state: str = "", error: str = ""):
        w = W(request)
        pre = w.load_session(request)
        if pre is not None and pre.member_id is not None:
            # Already signed in: a reloaded, reopened or duplicate login link. The code is never used.
            return _redirect(_local_next("", mp))
        if pre is None or not state or \
                not pysecrets.compare_digest(state, pre.data.get("oauth_state", "")):
            raise PermissionDenied("That login link expired. Please try logging in again.")
        nxt = _local_next(pre.data.get("next", ""), mp)
        if error or not code:
            if error == "access_denied" and pre.data.get("prompt") == "none":
                # First login (or the app's authorization was removed): show Discord's Authorize screen.
                w.forget_session(request)       # old state is single-use; the new cookie replaces it
                return _start_login(w, nxt, "consent")
            resp = render(request, "message.html", {
                "title": "Login not finished",
                "message": "Discord didn't confirm the login (Cancel was pressed, or Discord declined it). "
                           "Nothing was saved. You can try again.",
                "retry_url": (f"{mp}/v/{pre.data['guest_code']}" if pre.data.get("guest_code")
                              else f"{mp}/login?next={nxt}")}, status=400, public=True)
            w.drop_session(request, resp)
            return resp
        if pre.data.get("guest_code"):
            return _guest_callback(request, w, pre, code)
        resp = _redirect(nxt)
        w.drop_session(request, resp)           # state is single-use
        ident = w.oauth.identify(code, w.cfg["community"]["public_base_url"].rstrip("/") + mp + "/oauth/callback")
        discord_id, name = ident[0], ident[1]
        username = ident[2] if len(ident) > 2 else ""
        info = w.rest.member(w.cfg.guild_id, discord_id) if w.rest else (False, frozenset(), "")
        in_guild, roles, gname = info
        joined = getattr(info, "joined_at", None)
        from ..domain.actor import Actor
        a = Actor(discord_id=discord_id, source="web", role_ids=roles, in_guild=in_guild, display_name=gname or name,
                  username=username, joined_at=joined)
        permissions.require_member(w.cfg, a)
        with w.db.tx() as s:
            a = roster.ensure_member(s, a)
        w.new_session(resp, a.member_id, {"discord_id": discord_id, "name": a.display_name, "username": username,
                                          "roles": sorted(roles), "joined_at": joined.isoformat() if joined else None,
                                          "in_guild": in_guild, "roles_at": __import__("time").time()})
        w.cleanup_sessions()
        log.info("login member=%s", a.member_id)
        return resp

    # ------------------------------------------------------------- guest voice links
    def _guest_page(request, title: str, message: str, status: int = 200, **extra):
        return render(request, "voice_invite.html", {"title": title, "message": message, "noindex": True, **extra},
                      status=status, public=True)

    @r.get(mp + "/v/{code}")
    def guest_invite(request: Request, code: str):
        w = W(request)
        from .app import client_ip
        if not w.rate_ok("guest:" + client_ip(request), 20):
            return _guest_page(request, "Slow down", "Too many tries. Wait a minute and open the link again.", 429)
        try:
            with w.db.tx() as s:
                ev = voice.check_invite(s, w.cfg, code)
                title, end = ev.title, ev.end_at
        except NotFound as exc:
            return _guest_page(request, "Voice link ended", str(exc), 404)
        return _guest_page(request, "Join the voice channel", "", event_title=title, end_at=end,
                           go_url=f"{mp}/v/{code.strip().lower()}/go", max_guests=w.cfg["voice"]["max_guests_per_invite"])

    @r.get(mp + "/v/{code}/go")
    def guest_go(request: Request, code: str):
        w = W(request)
        if w.oauth is None:
            return _guest_page(request, "Sign-in unavailable", "Discord sign-in isn't set up on this server.", 503)
        with w.db.tx() as s:
            try:
                voice.check_invite(s, w.cfg, code)
            except NotFound as exc:
                return _guest_page(request, "Voice link ended", str(exc), 404)
        cur = w.load_session(request)
        if cur is not None and cur.member_id is not None:
            return _guest_finish(request, w, code.strip().lower(), cur.data.get("discord_id", ""), False)
        state = pysecrets.token_urlsafe(24)
        resp = _redirect("about:blank")
        w.new_session(resp, None, {"oauth_state": state, "guest_code": code.strip().lower(), "next": mp + "/"})
        resp.headers["location"] = w.oauth.authorize_url(
            w.cfg["community"]["public_base_url"].rstrip("/") + mp + "/oauth/callback", state, prompt="consent",
            scope="identify guilds.join")
        return resp

    def _guest_callback(request, w, pre, code: str):
        """A guest signed in: add them to the server if needed and open this event's voice to them.
        No QuestersBoard account or session is created; the Discord token is used once and dropped."""
        gcode = pre.data["guest_code"]
        w.forget_session(request)               # state is single-use; the cookie is cleared on the reply
        ident = w.oauth.guest_identify(code, w.cfg["community"]["public_base_url"].rstrip("/") + mp + "/oauth/callback")
        discord_id, token = ident[0], ident[3]
        try:
            with w.db.tx() as s:
                voice.check_invite(s, w.cfg, gcode, discord_id)     # before adding anyone to the server
        except NotFound as exc:
            return _guest_page(request, "Voice link ended", str(exc), 404)
        except PermissionDenied as exc:
            return _guest_page(request, "Can't join this voice", str(exc), 403)
        in_guild = w.rest.member(w.cfg.guild_id, discord_id)[0] if w.rest else False
        if not in_guild:
            try:
                w.rest.add_guild_member(w.cfg.guild_id, discord_id, token)
            except Exception:  # noqa: BLE001  (banned from the server, server full, Discord refused)
                log.info("guest voice: could not add a guest to the server")
                return _guest_page(request, "Couldn't add you to the server",
                                   f"Discord didn't let us add you to the {w.cfg['community']['name']} server. "
                                   "Ask the person who sent the link to invite you.", 403)
        resp = _guest_finish(request, w, gcode, discord_id, not in_guild)
        w.drop_session(request, resp)
        return resp

    def _guest_finish(request, w, gcode: str, discord_id: str, added: bool):
        try:
            with w.db.tx() as s:
                got = voice.redeem(s, w.cfg, gcode, discord_id)
                title, ch = got.event.title, got.channel_id
        except (NotFound, PermissionDenied) as exc:
            return _guest_page(request, "Can't join this voice", str(exc), 403)
        log.info("guest voice: joined (new to server=%s)", added)
        return _guest_page(request, "You're in", "", joined=True, event_title=title,
                           channel_url=voice.link(w.cfg, ch), added=added)

    @r.post(mp + "/logout")
    async def logout(request: Request):
        await post_guard(request, fresh=False)
        resp = _redirect(mp + "/")
        W(request).drop_session(request, resp)
        return resp

    # ------------------------------------------------------------- dashboard
    @r.get(mp + "/")
    def home(request: Request):
        w = W(request)
        sess = w.load_session(request)
        request.state.sess = sess
        a = w.actor(sess)
        with w.db.tx() as s:
            from .routes_public import upcoming
            items = upcoming(s, w.cfg, 20)
            mine, recs = [], []
            if a:
                mine = list(s.execute(select(Signup, Event).join(Event, Event.id == Signup.event_id).where(
                    Signup.member_id == a.member_id, Signup.state.in_(signups.ACTIVE),
                    Event.state.in_(("scheduled", "in_progress"))).order_by(Event.start_at)).all())
                recs = recommendations.for_member(s, w.cfg, a.member_id)
                drafts = list(s.scalars(select(Event).where(Event.creator_id == a.member_id,
                                                            Event.state == "draft").order_by(Event.start_at)))
                waiting = ([(e, m.display_name or m.username or "a member") for e, m in events.awaiting_publish(s)]
                           if permissions.has(w.cfg, a, "event_moderator") else [])
            else:
                drafts, waiting = [], []
        return render(request, "home.html", {"title": "Quests", "items": items, "mine": mine, "recs": recs,
                                             "drafts": drafts, "waiting": waiting, "now": now()})

    # ------------------------------------------------------------- events
    def _form_ctx(w, a, ev=None):
        with w.db.tx() as s:
            chars = roster.list_characters(s, a.member_id)
            my_tz = s.scalar(select(Member.timezone).where(Member.id == a.member_id)) if a.member_id else None
        # New events start in the host's saved zone (My settings); without one, the browser's zone
        # (filled in by the page script), then the community default.
            host_chars = [c for c in chars if c.archived_at is None]
            if ev is not None and ev.host_character_id and ev.host_character_id not in {c.id for c in host_chars}:
                cur = s.get(Character, ev.host_character_id)
                if cur is not None:
                    host_chars.insert(0, cur)
            for c in host_chars:
                s.expunge(c)
        return {"editions": w.cfg.editions, "activity_types": ACTIVITY_TYPES, "presets": w.cfg.role_presets,
                "host_chars": host_chars, "cover_url": covers.url(w.cfg, ev) if ev is not None else None,
                "max_level": max(e.max_level for e in w.cfg.editions.values()),
                "max_bytes": w.cfg["uploads"].get("max_bytes", 5_000_000),
                "ev": ev, "chars": chars, "tz": my_tz or w.cfg["community"]["default_timezone"],
                "tz_from_settings": bool(my_tz),
                "max_roles": MAX_ROLE_ROWS, "party_sizes": w.cfg["signups"]["party_size_max"]}

    @r.get(mp + "/events/new")
    def new_event(request: Request):
        sess, a = session_or_login(request)
        return render(request, "event_form.html", {"title": "New event", **_form_ctx(W(request), a)})

    @r.post(mp + "/events/new")
    async def create_event(request: Request):
        form, a = await post_guard(request)
        w = W(request)
        data = event_data_from_form(w.cfg, form)
        await _take_cover(w.cfg, form, data)
        with w.db.tx() as s:
            if data.get("cohosts"):
                data["cohosts"] = people.resolve(s, data["cohosts"], w.member_search)
            if form.get("rrule"):
                tz = data["timezone"]
                local = as_local(data["start_at"], tz).replace(tzinfo=None)
                series, evs, notes = recurrence.create_series(
                    s, w.cfg, a, data, form["rrule"], local, tz,
                    int((data["end_at"] - data["start_at"]).total_seconds() // 60))
                return _redirect(f"{mp}/events/{evs[0].public_id}")
            ev = events.create_draft(s, w.cfg, a, data)
            if form.get("publish"):
                events.publish(s, w.cfg, a, ev.id)
            elif form.get("request_publish"):
                events.request_publish(s, w.cfg, a, ev.id)
            pid = ev.public_id
        return _redirect(f"{mp}/events/{pid}")

    @r.get(mp + "/events/{public_id}")
    def show_event(request: Request, public_id: str):
        w = W(request)
        sess = w.load_session(request)
        request.state.sess = sess
        a = w.actor(sess)
        with w.db.tx() as s:
            ev = ev_by_public(s, public_id)
            cohosts = scheduling.cohost_ids(s, ev.id)
            manager = a is not None and permissions.is_event_manager(w.cfg, a, ev.creator_id, cohosts)
            if ev.state == "draft" and not manager:
                raise NotFound("Event not found.")
            if a is None and not ev.public_listing:
                raise _Login()
            rows = signups.roster(s, ev.id)
            mine = next((x for x in rows if a and x.member_id == a.member_id and x.state in signups.ACTIVE), None)
            chars = roster.list_characters(s, a.member_id) if a else []
            # same rules as signups._check_character, so every listed character can actually join
            fit = [c for c in chars if not signups.fit_problems(w.cfg, ev, c)]
            misfits = [(c.name, signups.fit_problems(w.cfg, ev, c)) for c in chars if c not in fit]
            entry = chronicle.lead(s, ev.id)
            my_story = can_story = None
            stories = []
            if a is not None and ev.state == "completed":
                my_story = s.scalar(select(ChronicleEntry).where(ChronicleEntry.event_id == ev.id,
                                                                 ChronicleEntry.author_id == a.member_id))
                can_story = my_story is None and chronicle.story_eligible(s, w.cfg, a, ev)
                if manager:
                    stories = [(x, m.display_name or m.username or "a member") for x, m in s.execute(
                        select(ChronicleEntry, Member).join(Member, Member.id == ChronicleEntry.author_id)
                        .where(ChronicleEntry.event_id == ev.id).order_by(ChronicleEntry.id)).all()]
            attendance = {x.member_id: x.status for x in s.scalars(
                select(Attendance).where(Attendance.event_id == ev.id))} if manager else {}
            conflicts = []
            if manager and ev.state == "draft":
                conflicts = scheduling.preview_conflicts(s, ev.start_at, ev.end_at, ev.exclusive, ev.id,
                                                         {ev.creator_id} | cohosts, w.cfg.max_simultaneous)
            me = s.get(Member, a.member_id) if a and a.member_id else None
            owner = s.get(Member, ev.creator_id) if ev.state == "draft" and ev.creator_id != (a and a.member_id) else None
            creator_name = (owner.display_name or owner.username) if owner else ""
            ctx = {"title": ev.title, "ev": ev, "rows": rows, "creator_name": creator_name, "mine": mine, "fit": fit, "misfits": misfits,
                   "requirement": signups.event_requirement(w.cfg, ev), "manager": manager,
                   "recognition_default": bool(me and me.recognition_default),
                   "entry": entry, "conflicts": conflicts, "attendance": attendance,
                     "my_story": my_story, "can_story": can_story, "stories": stories,
                   "copy_date": copy_suggestion(ev)[0] if manager else "", "copy_time": copy_suggestion(ev)[1] if manager else "", "ed": w.cfg.edition(ev.edition),
                   "chars": chars, "editions": w.cfg.editions,
                   "cover_url": covers.url(w.cfg, ev),
                   "faction_color": w.cfg["event_posts"]["faction_colors"][ev.faction or "any"], "difficulty_line": eventtext.difficulty_line(ev),
                   "host_name": (s.get(Character, ev.host_character_id).name if ev.host_character_id else ""),
                   "thread_url": (f"https://discord.com/channels/{w.cfg.guild_id}/{ev.discord_thread_id}"
                                  if ev.discord_thread_id else ""),
                   "show_names": manager or (a is not None and roster.staff_can_view_roster(w.cfg, a)),
                   "voice_url": (voice.link(w.cfg, ev.discord_voice_channel_id)
                                 if ev.state == "in_progress" and ev.discord_voice_channel_id and a is not None
                                 and voice.can_join(s, w.cfg, a, ev) else ""),
                   "voice_reopen": (manager and w.cfg["voice"]["enabled"] and ev.state == "in_progress"
                                    and not ev.discord_voice_channel_id and ev.voice_closed_at is not None)}
            return render(request, "event.html", ctx)   # rendered inside the session (lazy loads)

    @r.get(mp + "/events/{public_id}/edit")
    def edit_event(request: Request, public_id: str):
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            ev = ev_by_public(s, public_id)
            permissions.require_event_manager(w.cfg, a, ev.creator_id, scheduling.cohost_ids(s, ev.id))
            co = [people.label(m) for m in s.scalars(
                select(Member).join(EventCohost, EventCohost.member_id == Member.id)
                .where(EventCohost.event_id == ev.id).order_by(EventCohost.member_id))]
            ld, lt, _ = current_local(ev)
            return render(request, "event_form.html", {"title": "Edit event", "cohost_ids": ", ".join(co),
                                                       "local_date": ld, "local_time": lt,
                                                       **_form_ctx(w, a, ev)})

    @r.post(mp + "/events/{public_id}/edit")
    async def save_event(request: Request, public_id: str):
        form, a = await post_guard(request)
        w = W(request)
        data = event_data_from_form(w.cfg, form, partial=True)
        with w.db.tx() as s:
            ev = ev_by_public(s, public_id)
            permissions.require_event_manager(w.cfg, a, ev.creator_id, scheduling.cohost_ids(s, ev.id))
        await _take_cover(w.cfg, form, data)
        with w.db.tx() as s:
            ev = ev_by_public(s, public_id)
            if data.get("cohosts"):
                data["cohosts"] = people.resolve(s, data["cohosts"], w.member_search)
            # The form now shows the current date/time; sending them back unchanged is not a reschedule.
            cur_date, cur_time, cur_mins = current_local(ev)
            sent_mins = str(form.get("duration_minutes") or "").strip()
            time_untouched = (form.get("date") == cur_date and form.get("start_time") == cur_time
                              and (form.get("timezone") or ev.timezone) == ev.timezone)
            if time_untouched and sent_mins in ("", str(cur_mins)):
                data.pop("start_at", None)
                data.pop("end_at", None)
            shift = None if time_untouched else (form.get("start_time") or None)
            if not form.get("confirm_fit"):
                # Edition/ruleset/faction/realm changed: say whose character stops fitting before saving.
                names = events.fit_impact(s, w.cfg, a, ev.id, data)
                if names:
                    n = len(names)
                    keep = [(k, v) for k, v in form.multi_items()
                            if k not in ("csrf", "confirm_fit", "cover", "cover_saved") and isinstance(v, str)]
                    if data.get("cover_image"):
                        keep.append(("cover_saved", data["cover_image"]))
                    return render(request, "message.html", {
                        "title": "Before saving",
                        "message": (f"With this change, {n} signed-up character{'s' if n != 1 else ''} won't fit "
                                    f"the event any more. They'll be moved to tentative and asked by DM to pick "
                                    f"another character. Everyone else stays signed up and gets a notice."),
                        "confirm_list": names, "confirm_fields": keep,
                        "confirm_action": f"{mp}/events/{public_id}/edit",
                        "confirm_label": "Save and ask them",
                        "back_url": f"{mp}/events/{public_id}/edit",
                        "restore_action": f"{mp}/events/{public_id}/edit"}, status=409)
            if form.get("scope") == "future" and ev.series_id:
                recurrence.edit_future(s, w.cfg, a, ev.id, {k: v for k, v in data.items()
                                                            if k not in ("start_at", "end_at")},
                                       shift_local_time=shift)
            elif ev.series_id:
                recurrence.edit_occurrence(s, w.cfg, a, ev.id, data)
            else:
                events.update(s, w.cfg, a, ev.id, data)
            if form.get("publish") and ev.state == "draft":
                events.publish(s, w.cfg, a, ev.id)
            elif form.get("request_publish") and ev.state == "draft":
                events.request_publish(s, w.cfg, a, ev.id)
        return _redirect(f"{mp}/events/{public_id}")

    ACTIONS = ("join", "tentative", "confirm", "leave", "reconfirm", "change", "publish", "request_publish",
               "withdraw_publish", "start", "end", "cancel",
               "discard", "copy", "attendance", "extend", "story", "reopen_voice")

    @r.post(mp + "/events/{public_id}/{action}")
    async def event_action(request: Request, public_id: str, action: str):
        if action not in ACTIONS:
            raise NotFound("Unknown action.")
        form, a = await post_guard(request)
        w, c = W(request), W(request).cfg
        dest = f"{mp}/events/{public_id}"
        with w.db.tx() as s:
            ev = ev_by_public(s, public_id)
            if action == "join":
                rec = form.get("recognition")
                signups.join(s, c, a, ev.id, int(form.get("character_id") or 0), form.get("role_key") or None,
                             tentative=_bool(form, "tentative"), recognition=None if rec in (None, "") else _bool(form, "recognition"),
                             note=form.get("note", ""))
            elif action == "tentative":
                signups.set_tentative(s, c, a, ev.id)
            elif action == "confirm":
                signups.confirm(s, c, a, ev.id, form.get("role_key") or None)
            elif action == "leave":
                signups.leave(s, c, a, ev.id)
            elif action == "reconfirm":
                signups.reconfirm(s, c, a, ev.id, form.get("role_key") or None,
                                  int(form["character_id"]) if form.get("character_id") else None)
            elif action == "change":
                rec = form.get("recognition")
                signups.change(s, c, a, ev.id, character_id=int(form["character_id"]) if form.get("character_id") else None,
                               role_key=form.get("role_key") or None,
                               recognition=None if rec in (None, "") else _bool(form, "recognition"))
            elif action == "publish":
                events.publish(s, c, a, ev.id)
            elif action == "request_publish":
                events.request_publish(s, c, a, ev.id)
            elif action == "withdraw_publish":
                events.withdraw_publish_request(s, c, a, ev.id)
            elif action == "start":
                events.start(s, c, a, ev.id)
            elif action == "end":
                events.end(s, c, a, ev.id)
            elif action == "cancel":
                events.cancel(s, c, a, ev.id, form.get("reason", ""))
            elif action == "discard":
                events.discard_draft(s, c, a, ev.id)
                dest = mp + "/"
            elif action == "extend":
                mins = int(form.get("minutes") or 30)
                events.update(s, c, a, ev.id, {"end_at": ev.end_at + timedelta(minutes=max(15, min(mins, 240)))})
            elif action == "copy":
                tz = form.get("timezone") or ev.timezone
                start = _local_dt(form["date"], form["start_time"], tz, form.get("fold"))
                new = events.copy(s, c, a, ev.id, start, start + (ev.end_at - ev.start_at), tz)
                dest = f"{mp}/events/{new.public_id}/edit"
            elif action == "attendance":
                st = {int(k[4:]): v for k, v in form.items() if k.startswith("att_") and k[4:].isdigit()}
                events.record_attendance(s, c, a, ev.id, st)
            elif action == "reopen_voice":
                voice.reopen(s, c, a, ev.id)
            elif action == "story":
                dest = f"{mp}/chronicle/{chronicle.start_story(s, c, a, ev.id).id}"
        return _redirect(dest)

    @r.post(mp + "/signups/{signup_id}/{decision}")
    async def decide(request: Request, signup_id: int, decision: str):
        form, a = await post_guard(request)
        w = W(request)
        fn = {"approve": signups.approve, "waitlist": signups.waitlist}.get(decision)
        with w.db.tx() as s:
            su = s.get(Signup, signup_id)
            if su is None:
                raise NotFound("Signup not found.")
            pid = s.get(Event, su.event_id).public_id
            if decision == "reject":
                signups.reject(s, w.cfg, a, signup_id, form.get("reason", ""))
            elif decision == "role":
                signups.move_role(s, w.cfg, a, signup_id, form.get("role_key", ""))
            elif fn:
                fn(s, w.cfg, a, signup_id)
            else:
                raise NotFound("Unknown decision.")
        return _redirect(f"{mp}/events/{pid}")

    # ------------------------------------------------------------- characters
    @r.get(mp + "/characters")
    def characters(request: Request, edit: int = 0, saved: int = 0, removed: int = 0):
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            chars = roster.list_characters(s, a.member_id)
            race_map = {ed: catalog.race_class_map(s, ed) for ed in w.cfg.editions}
            editing = next((c for c in chars if c.id == edit), None)
            if edit and editing is None:
                raise NotFound("Character not found.")
            race_label = class_label = ""
            if editing is not None:   # show catalog labels ("Skyborne (Windshaper)"), not stored keys
                m = race_map.get(editing.edition) or {}
                race_label = next((r["label"] for r in (m.get("factions") or {}).get(editing.faction, [])
                                   if r["key"] == editing.race_key), editing.race_key.replace("_", " ").title())
                class_label = (m.get("classes") or {}).get(editing.class_key,
                                                           editing.class_key.replace("_", " ").title())
            from ..models import BattlenetLink
            # The Battle.net list itself is deleted soon after the login; "verified" characters still count.
            bn_linked = (s.query(BattlenetLink.member_id).filter(BattlenetLink.member_id == a.member_id).first()
                         is not None or any(c.verification == "ownership_verified" for c in chars))
            from ..domain import profiles
            details = profiles.profiles_for(s, a.member_id)
            for p in details.values():
                s.expunge(p)
            guild_links = profiles.my_guild_links(s, a.member_id)
        bz = w.cfg["blizzard"]
        return render(request, "characters.html", {"title": "Characters", "chars": chars, "race_map": race_map,
                                                   "editions": w.cfg.editions, "presets": w.cfg.role_presets,
                                                   "bz_enabled": bool(bz.get("enabled")),
                                                   "bn_enabled": bool(bz.get("enabled") and bz.get("battlenet_link_enabled")),
                                                   "bn_linked": bn_linked, "editing": editing,
                                                   "race_label": race_label, "class_label": class_label,
                                                   "details": details, "detail_groups": profiles.GROUPS,
                                                   "share_labels": profiles.SHARE_LABELS,
                                                   "effective_share": profiles.effective_share,
                                                   "stat_sections": profiles.stat_sections,
                                                   "gear_columns": profiles.gear_columns, "gear_portrait_key": profiles.gear_portrait_key,
                                                   "guild_links": guild_links, "saved": saved,
                                                   "removed": removed})

    @r.post(mp + "/characters")
    async def add_character(request: Request):
        form, a = await post_guard(request, fresh=False)
        w = W(request)
        data = {k: form.get(k) for k in ("edition", "region", "ruleset", "realm_slug", "realm_name", "name",
                                         "faction", "race_key", "class_key", "level", "spec", "preferred_role",
                                         "notes")}
        data = {k: v for k, v in data.items() if v not in (None, "")}
        data["eligible_roles"] = form.getlist("eligible_roles")
        data["focus_tags"] = [t for t in re.split(r"[,\s]+", form.get("focus_tags", "")) if t]
        data["recognition_default"] = _bool(form, "recognition_default")
        if form.get("character_id"):
            # The edit form shows every field, so a field left blank means "clear it", not "keep it".
            for k in ("level", "spec", "preferred_role", "realm_slug"):
                data.setdefault(k, None)
        keep = [(k, v) for k, v in form.multi_items() if k not in ("csrf", "leave_events", "as_new")]
        try:
            with w.db.tx() as s:
                if form.get("character_id") and not form.get("as_new"):
                    roster.update_character(s, w.cfg, a, int(form["character_id"]), data,
                                            leave_events=_bool(form, "leave_events"))
                else:
                    roster.add_character(s, w.cfg, a, data)
        except (NeedsLeave, EditionLocked) as exc:
            # Offer the one-step fix instead of a dead end; the buttons re-send what was typed.
            ctx = {"title": "Before saving", "message": exc.message, "fields": keep,
                   "back_url": f"{mp}/characters?edit={form.get('character_id')}#character-form",
                   "restore_action": f"{mp}/characters"}
            if isinstance(exc, NeedsLeave):
                ctx["leave_events"] = exc.events
            else:
                ctx["as_new_fields"] = [(k, v) for k, v in keep if k != "character_id"]
                ctx["as_new_edition"] = w.cfg.edition(data.get("edition") or "").name if data.get("edition") in w.cfg.editions else ""
            return render(request, "message.html", ctx, status=409)
        return _redirect(mp + "/characters")

    @r.post(mp + "/characters/{character_id}/remove")
    async def remove_character(request: Request, character_id: int):
        form, a = await post_guard(request, fresh=False)
        with W(request).db.tx() as s:
            roster.remove_character(s, W(request).cfg, a, character_id)
        return _redirect(mp + "/characters")

    @r.post(mp + "/characters/{character_id}/lookup")
    async def lookup_character(request: Request, character_id: int):
        """Ask Blizzard whether this character exists. A match is NOT proof of ownership."""
        from ..integrations.blizzard import ApiError, Unsupported
        form, a = await post_guard(request, fresh=False)
        w = W(request)
        with w.db.tx() as s:
            ch = s.get(Character, character_id)
            if ch is None or ch.member_id != a.member_id or ch.archived_at is not None:
                raise NotFound("Character not found.")
            try:
                m = w.bz.apply_lookup(s, ch)
            except Unsupported as exc:
                raise Invalid(str(exc)) from None
            except ApiError:
                raise Invalid("Blizzard's API didn't answer. Your character is unchanged; try again later.") from None
            audit.record(s, a, "character.lookup", "character", ch.id, found=m.found)
        return _redirect(mp + "/characters")

    @r.post(mp + "/characters/{character_id}/details")
    async def import_details(request: Request, character_id: int):
        """Import the ticked groups of Blizzard character details, each with its own sharing choice."""
        from ..domain import profiles
        from ..integrations.blizzard import ApiError, Unsupported
        form, a = await post_guard(request, fresh=False)
        w = W(request)
        groups, shares = profiles.parse_form(form)
        with w.db.tx() as s:
            try:
                profiles.import_profile(s, w.cfg, w.bz, a, character_id, groups, shares)
            except Unsupported as exc:
                raise Invalid(str(exc)) from None
            except ApiError:
                raise Invalid("Blizzard's API didn't answer. Nothing was changed; try again later.") from None
        return _redirect(f"{mp}/characters?saved={character_id}#char-{character_id}")

    def _details_ctx(w):
        from ..domain import profiles
        return {"detail_groups": profiles.GROUPS, "share_labels": profiles.SHARE_LABELS,
                "effective_share": profiles.effective_share, "stat_sections": profiles.stat_sections,
                "gear_columns": profiles.gear_columns, "gear_portrait_key": profiles.gear_portrait_key, "editions": w.cfg.editions}

    @r.get(mp + "/characters/{character_id}/view")
    def character_view(request: Request, character_id: int):
        """A character's shared details, as this viewer may see them (owner: everything)."""
        from ..domain import profiles
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            ch, prof, vis = profiles.view_character(s, w.cfg, a, character_id)
            owner = ch.member_id == a.member_id
            guild_ok = bool(prof.guild_id) and "guild" in vis and \
                (prof.edition, prof.region, prof.guild_id) in profiles.viewer_guilds(s, a.member_id)
            s.expunge(ch)
            s.expunge(prof)
        with w.db.tx() as s:
            guild_links = profiles.my_guild_links(s, a.member_id)
        return render(request, "character_view.html", {"guild_links": guild_links, "title": ch.name, "c": ch, "d": prof, "visible": vis,
                                                      "owner_view": owner, "open_groups": True,
                                                      "portrait_key": "main-raw", "guild_ok": guild_ok,
                                                      **_details_ctx(w)})

    @r.get(mp + "/characters/{character_id}/portrait/{key}")
    def character_portrait(request: Request, character_id: int, key: str):
        """The portrait, fetched from Blizzard by QuestersBoard (visitors' browsers never contact Blizzard)."""
        from fastapi.responses import Response

        from ..domain import profiles
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            ch, prof, vis = profiles.view_character(s, w.cfg, a, character_id)
            url = next((x["url"] for x in (prof.groups or {}).get("portrait") or [] if x.get("key") == key), None)
        if "portrait" not in vis or not url:
            raise NotFound("Picture not found.")
        got = w.bz.fetch_image(url)
        if got is None:
            raise NotFound("Blizzard didn't send a picture.")
        body, ctype = profiles.trim_render(*got)
        return Response(body, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})

    @r.get(mp + "/characters/{character_id}/icon/{group}/{ref}")
    def character_icon(request: Request, character_id: int, group: str, ref: str):
        """A gear or talent icon (Blizzard's game art), fetched by QuestersBoard like the portrait.
        ref: the item's position in the gear list, or group-spec-talent positions for talents."""
        from fastapi.responses import Response

        from ..domain import profiles
        if group not in ("gear", "talents") or not re.fullmatch(r"\d{1,3}(-\d{1,3}){0,2}", ref):
            raise NotFound("Icon not found.")
        sess, a = session_or_login(request, bucket="i")
        w = W(request)
        with w.db.tx() as s:
            ch, prof, vis = profiles.view_character(s, w.cfg, a, character_id)
            url = profiles.icon_url((prof.groups or {}).get(group), group, ref) if group in vis else None
        if not url:
            raise NotFound("Icon not found.")
        got = w.bz.fetch_icon(url)
        if got is None:
            raise NotFound("Blizzard didn't send this icon.")
        body, ctype = got
        return Response(body, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})

    def _roster_page(request, w, a, title, rows, scope, guild=None):
        from ..domain import profiles
        f = {k: request.query_params.get(k, "") for k in ("class", "role", "spec", "profession", "min_level",
                                                           "min_ilvl", "edition")}
        shown = profiles.filter_rows(rows, f)
        with w.db.tx() as s:
            guild_links = profiles.my_guild_links(s, a.member_id)
        return render(request, "roster.html", {"title": title, "rows": shown, "total": len(rows), "f": f,
                                               "opts": profiles.filter_options(rows), "scope": scope,
                                               "guild": guild, "editions": w.cfg.editions,
                                               "presets": {p.key: p.label for p in w.cfg.role_presets},
                                               "guild_links": guild_links})

    @r.get(mp + "/guilds/{edition}/{region}/{guild_id}")
    def guild_page(request: Request, edition: str, region: str, guild_id: str):
        """Guild roster: only characters that shared their guild, only for confirmed members of it."""
        from ..domain import profiles
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            name, rows = profiles.guild_roster(s, w.cfg, a, edition, region, guild_id)
            for r_ in rows:
                s.expunge(r_.ch)
                s.expunge(r_.prof)
        return _roster_page(request, w, a, f"{name or 'Guild'} roster", rows, "guild",
                            {"name": name, "edition": edition, "region": region})

    @r.get(mp + "/players")
    def shared_players(request: Request):
        """Characters with details shared with all QuestersBoard members."""
        from ..domain import profiles
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            rows = profiles.shared_with_members(s, w.cfg, a)
            for r_ in rows:
                s.expunge(r_.ch)
                s.expunge(r_.prof)
        return _roster_page(request, w, a, "Shared characters", rows, "members")

    @r.post(mp + "/characters/{character_id}/details/remove")
    async def remove_details(request: Request, character_id: int):
        from ..domain import profiles
        form, a = await post_guard(request, fresh=False)
        w = W(request)
        with w.db.tx() as s:
            profiles.remove_profile(s, w.cfg, a, character_id)
        return _redirect(f"{mp}/characters?removed={character_id}#char-{character_id}")

    @r.get(mp + "/characters/battlenet")
    def battlenet_start(request: Request, edition: str = "", region: str = ""):
        """Optional ownership check: Battle.net login (wow.profile), one-time character list read."""
        from ..integrations.blizzard import Unsupported
        sess, a = session_or_login(request)
        w = W(request)
        try:
            ed = w.cfg.edition(edition)
            region = region or ed.default_region
            if region not in ed.regions:
                raise Invalid("Unknown region for this edition.")
            w.bz.require(ed, "ownership", "profile", region)
            url, state = w.bz.authorize_url(region, w.cfg["community"]["public_base_url"].rstrip("/")
                                            + mp + "/battlenet/callback")
        except (Unsupported, KeyError) as exc:
            raise Invalid(str(exc) if isinstance(exc, Unsupported) else "Unknown edition.") from None
        w.update_session_data(sess, bn_state=state, bn_edition=edition, bn_region=region)
        return RedirectResponse(url, status_code=303)

    @r.get(mp + "/battlenet/callback")
    def battlenet_callback(request: Request, code: str = "", state: str = ""):
        from ..integrations.blizzard import ApiError, Unsupported
        sess, a = session_or_login(request)
        w = W(request)
        expected = sess.data.get("bn_state", "")
        edition, region = sess.data.get("bn_edition", ""), sess.data.get("bn_region", "")
        w.update_session_data(sess, bn_state="", bn_edition="", bn_region="")      # single-use
        if not expected or not state or not code or not pysecrets.compare_digest(state, expected):
            raise Invalid("That Battle.net login link expired. Start again from My characters.")
        with w.db.tx() as s:
            try:
                res = w.bz.link_account(s, a.member_id, edition, region, code,
                                        w.cfg["community"]["public_base_url"].rstrip("/") + mp + "/battlenet/callback")
            except (Unsupported, ApiError) as exc:
                raise Invalid(str(exc)) from None
            audit.record(s, a, "character.ownership_link", "member", a.member_id, edition=edition,
                         verified=len(res["verified"]), offered=len(res["new"]))
        return _battlenet_review(request, w, edition, region, res["verified"], res["new"])

    def _battlenet_review(request, w, edition, region, verified, offers, added=(), skipped=()):
        ed = w.cfg.edition(edition)
        rows = []
        for i, c in offers:
            # Blizzard gives characters awaiting a forced rename a placeholder like "Name1A2B3C"; WoW names
            # never have digits, so those can't be added until they're renamed in game.
            named = bool(roster.NAME_RE.match(c.get("name") or ""))
            rows.append({"i": i, "c": c, "ruleset": c.get("ruleset"), "named": named,
                         "ready": named and bool(c.get("faction") in ed.factions and c.get("race") and c.get("class"))})
        return render(request, "battlenet_sync.html", {
            "title": "Battle.net characters", "ed": ed, "edition": edition, "region": region,
            "verified": verified, "rows": rows, "added": list(added), "skipped": list(skipped),
            "minutes": int(w.cfg["blizzard"].get("battlenet_import_minutes", 60))})

    @r.post(mp + "/characters/battlenet/import")
    async def battlenet_import(request: Request):
        """Add characters picked from the Battle.net list. Reads the member's own encrypted snapshot, so it
        needs no Battle.net token; the list is only usable for battlenet_import_minutes after the login."""
        form, a = await post_guard(request, fresh=False)
        w = W(request)
        region = form.get("region", "")
        idx = set()
        for v in form.getlist("pick"):
            if not str(v).isdigit():
                raise Invalid("Pick characters from the list.")
            idx.add(int(v))
        with w.db.tx() as s:
            snap = w.bz.read_snapshot(s, a.member_id, region,
                                      max_age_minutes=w.cfg["blizzard"].get("battlenet_import_minutes", 60))
            if snap is None:
                raise Invalid("That Battle.net list has expired. Log in with Battle.net again from My characters.")
            edition, chars = snap
            if edition not in w.cfg.editions:
                raise Invalid("That Battle.net list is for an edition this site no longer has.")
            if not idx:
                raise Invalid("Tick at least one character to add.")
            # Only entries still on offer: a page sent twice doesn't add anything twice.
            offered = dict(w.bz.offer(s, a.member_id, edition, region, chars))
            picks = [(offered[i], form.get(f"ruleset_{i}") or None) for i in sorted(idx) if i in offered]
            added, skipped = roster.import_verified(s, w.cfg, a, edition, region, picks)
            left = w.bz.offer(s, a.member_id, edition, region, chars)
        if not skipped and not left:
            return _redirect(mp + "/characters")
        return _battlenet_review(request, w, edition, region, [], left, added=added, skipped=skipped)

    @r.post(mp + "/characters/battlenet/unlink")
    async def battlenet_unlink(request: Request):
        """Forget the Battle.net snapshot; characters verified through it drop back to self-reported."""
        from ..models import BattlenetLink
        form, a = await post_guard(request, fresh=False)
        w = W(request)
        with w.db.tx() as s:
            n = s.query(BattlenetLink).filter(BattlenetLink.member_id == a.member_id).delete()
            k = (s.query(Character).filter(Character.member_id == a.member_id,
                                           Character.verification == "ownership_verified")
                 .update({"verification": "self_reported"}))
            audit.record(s, a, "character.ownership_unlink", "member", a.member_id, links=n, characters=k)
        return _redirect(mp + "/characters")

    # ------------------------------------------------------------- me / calendar
    @r.get(mp + "/me")
    def me(request: Request):
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            m = s.get(Member, a.member_id)
            s.expunge(m)
        return render(request, "me.html", {"title": "My settings", "m": m,
                                           "new_token": sess.data.get("new_cal_token")})

    @r.post(mp + "/me")
    async def save_me(request: Request):
        form, a = await post_guard(request, fresh=False)
        w = W(request)
        with w.db.tx() as s:
            roster.set_member_prefs(s, w.cfg, a, recognition_default=_bool(form, "recognition_default"),
                                    timezone=form.get("timezone"))
        return _redirect(mp + "/me")

    @r.post(mp + "/me/calendar")
    async def calendar_token(request: Request):
        form, a = await post_guard(request, fresh=False)
        w = W(request)
        with w.db.tx() as s:
            if form.get("revoke"):
                calendar.revoke_tokens(s, w.cfg, a)
                tok = None
            else:
                tok = calendar.issue_token(s, w.cfg, a)     # also revokes earlier tokens
        w.update_session_data(request.state.sess, new_cal_token=tok)   # shown once, then cleared
        return _redirect(mp + "/me")

    # ------------------------------------------------------------- remove me (erasure)
    def _remove_page(request, a, status=200, error=""):
        w = W(request)
        with w.db.tx() as s:
            p = erasure.plan(s, a.member_id)
            for ev in {id(e): e for e in p.leave + p.cancel}.values():
                s.expunge(ev)
        return render(request, "remove_me.html", {"title": "Remove me from QuestersBoard", "p": p, "error": error,
                                                  "phrase": erasure.CONFIRM_PHRASE}, status=status)

    @r.get(mp + "/me/remove")
    def remove_me(request: Request):
        sess, a = session_or_login(request)
        return _remove_page(request, a)

    @r.post(mp + "/me/remove")
    async def remove_me_confirm(request: Request):
        form, a = await post_guard(request, fresh=False)
        w = W(request)
        if not erasure.phrase_ok(form.get("confirm", "")):
            return _remove_page(request, a, 400, f"Type \"{erasure.CONFIRM_PHRASE}\" in the box to confirm. "
                                                 "Nothing has been removed.")
        with w.db.tx() as s:
            receipt = erasure.erase(s, w.cfg, a.member_id, source="web")
        log.info("member erased (self-service) member=%s", a.member_id)
        request.state.sess = None           # the session row is gone: render the receipt logged out
        resp = render(request, "removed.html", {"title": "Your request is complete", "r": receipt})
        w.drop_session(request, resp)
        resp.headers["cache-control"] = "no-store"
        return resp

    # ------------------------------------------------------------- chronicle (authoring)
    @r.get(mp + "/chronicle/{entry_id}")
    def chronicle_edit(request: Request, entry_id: int):
        sess, a = session_or_login(request)
        w = W(request)
        if sess.data.get("new_cal_token"):
            w.update_session_data(sess, new_cal_token=None)
        with w.db.tx() as s:
            e = chronicle.get(s, entry_id)
            if not (chronicle.can_edit(s, w.cfg, a, e) or permissions.has(w.cfg, a, "chronicle_reviewer")):
                raise PermissionDenied("Only the event's hosts and co-authors can edit this story.")
            ev = s.get(Event, e.event_id)
            rev = chronicle.working(s, e)
            ups = list(s.scalars(select(ChronicleUpload).where(ChronicleUpload.entry_id == e.id,
                                                               ChronicleUpload.removed_at.is_(None))))
            problems = chronicle.check(s, w.cfg, e, rev)
            rows = signups.roster(s, ev.id)
            att = {x.member_id: x.status for x in s.scalars(select(Attendance).where(Attendance.event_id == ev.id))}
            hist = chronicle.history(s, e.id)
            names = {m.id: (m.display_name or m.username or "a member") for m in s.scalars(
                select(Member).where(Member.id.in_({h.created_by for h in hist} | {e.author_id})))}
            choices = chronicle.coauthor_choices(s, w.cfg, e)
            current = chronicle.coauthor_ids(s, e.id)
            others = [(x, names.get(x.author_id) or "") for x in chronicle.entries(s, ev.id) if x.id != e.id]
            if any(not n for _, n in others):
                extra = {m.id: m.display_name or m.username or "a member" for m in s.scalars(
                    select(Member).where(Member.id.in_([x.author_id for x, _ in others])))}
                others = [(x, extra.get(x.author_id, "a member")) for x, _ in others]
            return render(request, "chronicle_edit.html", {"title": "Chronicle", "e": e, "ev": ev, "rev": rev,
                                                       "ups": ups, "problems": problems, "rows": rows, "att": att,
                                                       "hist": hist, "outcomes": chronicle.OUTCOMES,
                                                       "names": names, "choices": choices, "current": current,
                                                       "host_story": e.author_id == ev.creator_id,
                                                       "others": others,
                                                       "ai": w.cfg["ai"].get("enabled", False)})

    @r.post(mp + "/chronicle/{entry_id}/{action}")
    async def chronicle_action(request: Request, entry_id: int, action: str):
        w = W(request)
        if action == "upload":
            form, a = await post_guard(request, fresh=False)
            f = form.get("file")
            if f is None or not hasattr(f, "read"):
                raise Invalid("Choose an image to upload.")
            data = await f.read(w.cfg["uploads"].get("max_bytes", 5_000_000) + 1)
            with w.db.tx() as s:
                chronicle.add_upload(s, w.cfg, a, entry_id, data, form.get("caption", ""))
            return _redirect(f"{mp}/chronicle/{entry_id}")
        form, a = await post_guard(request)
        with w.db.tx() as s:
            if action == "save":
                chronicle.save(s, w.cfg, a, entry_id, {k: form.get(k) for k in
                                                       ("title", "attempted", "happened", "outcome", "moments", "body")
                                                       if form.get(k) is not None})
            elif action == "submit":
                chronicle.submit(s, w.cfg, a, entry_id)
            elif action == "coauthors":
                chronicle.set_coauthors(s, w.cfg, a, entry_id,
                                        [int(x) for x in form.getlist("coauthor") if str(x).isdigit()])
            elif action == "suggest":
                chronicle.ai_suggest(s, w.cfg, a, entry_id)
            elif action == "clear":
                chronicle.clear_upload(s, w.cfg, a, int(form["upload_id"]), _bool(form, "cleared"))
            elif action == "banner":
                chronicle.set_banner(s, w.cfg, a, entry_id, int(form["upload_id"]) if form.get("upload_id") else None)
            elif action == "remove_upload":
                chronicle.remove_upload(s, w.cfg, a, int(form["upload_id"]))
            elif action == "review":
                chronicle.review(s, w.cfg, a, entry_id, form.get("decision", ""), form.get("note", ""))
            elif action == "unpublish":
                chronicle.unpublish(s, w.cfg, a, entry_id, form.get("reason", ""))
            elif action == "remove_me":
                e = chronicle.get(s, entry_id)
                chronicle.remove_recognition(s, w.cfg, a, e.event_id)
            else:
                raise NotFound("Unknown action.")
        return _redirect(f"{mp}/chronicle/{entry_id}" if action not in ("review",) else f"{mp}/review")

    @r.get(mp + "/uploads/{public_id}.webp")
    def upload_preview(request: Request, public_id: str):
        """Authors/reviewers preview screenshots before they are cleared and published."""
        from pathlib import Path

        from fastapi.responses import FileResponse
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            try:
                u = s.scalar(select(ChronicleUpload).where(ChronicleUpload.public_id == uuid.UUID(public_id)))
            except ValueError:
                u = None
            if u is None or u.removed_at:
                raise NotFound("No such image.")
            e = chronicle.get(s, u.entry_id)
            if not (chronicle.can_edit(s, w.cfg, a, e) or permissions.has(w.cfg, a, "chronicle_reviewer")):
                raise NotFound("No such image.")
        return FileResponse(Path(w.cfg["uploads"]["storage_dir"]) / u.filename, media_type="image/webp",
                            headers={"Cache-Control": "private, no-store"})

    @r.get(mp + "/review")
    def review_queue(request: Request):
        sess, a = session_or_login(request, fresh=True)
        w = W(request)
        permissions.require(w.cfg, a, "chronicle_reviewer", "The review queue needs the chronicle reviewer role.")
        with w.db.tx() as s:
            rows = list(s.execute(select(ChronicleEntry, Event, Member).join(Event, Event.id == ChronicleEntry.event_id)
                                  .join(Member, Member.id == ChronicleEntry.author_id)
                                  .where(ChronicleEntry.state == "pending_review")
                                  .order_by(ChronicleEntry.updated_at)).all())
            published = s.scalar(select(func.count()).select_from(ChronicleEntry)
                                 .where(ChronicleEntry.state == "published"))
        return render(request, "review.html", {"title": "Review queue", "rows": rows, "published": published})

    # ------------------------------------------------------------- autocomplete
    @r.get(mp + "/api/options")
    def options(request: Request, edition: str, kind: str, q: str = "", parent: str | None = None):
        sess, a = session_or_login(request)
        w = W(request)
        if kind not in ("race", "class", "quest", "zone", "location", "instance"):
            raise Invalid("Unknown option list.")
        w.cfg.edition(edition)
        with w.db.tx() as s:
            rows = catalog.options(s, edition, kind, parent, q[:60] or None, limit=25)
            items = [{"key": o.key, "label": o.label, "parent": o.parent_key,
                      **{k: (o.data or {})[k] for k in ("modes", "minimum_level") if k in (o.data or {})}}
                     for o in rows]
        status = w.cfg.edition(edition).capability("quests" if kind == "quest" else
                                                   "locations" if kind in ("zone", "location", "instance")
                                                   else "reference")
        return JSONResponse({"items": items, "api_status": status,
                             "free_text": True})   # always allowed: catalog gaps never block planning

    return r


class _Login(Exception):
    pass


def install_login_redirect(app, cfg: Config) -> None:
    mp = cfg["web"]["member_path"].rstrip("/")

    @app.exception_handler(_Login)
    async def _to_login(request: Request, exc: _Login):
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"error": "login_required"}, status_code=401)
        ua = request.headers.get("user-agent", "").lower()
        agents = cfg["web"].get("link_preview_agents", DEFAULT_PREVIEW_AGENTS)
        if request.method == "GET" and ua and any(x.lower() in ua for x in agents):
            return _preview_card(request, mp)
        return _redirect(f"{mp}/login?next={request.url.path}")


# Link-preview fetchers (Discord, Slack...) don't log in. Without this they follow the login redirect to
# Discord's own site and the chat shows Discord's card. They get a small generic card instead: the page's
# name and what it's for, never anyone's characters, guild names or other member data.
DEFAULT_PREVIEW_AGENTS = ("Discordbot", "Slackbot", "facebookexternalhit", "Twitterbot", "TelegramBot",
                          "WhatsApp", "LinkedInBot", "redditbot", "Applebot", "SkypeUriPreview")
_PREVIEW_PAGES = (  # (path prefix after the member path, title, what the page is for)
    ("/characters/", "Character page", "A member's character details, showing only what its owner chose to share."),
    ("/characters", "My characters", "Add your WoW characters, import gear, talents and professions from "
                                     "Blizzard, and choose who can see each part."),
    ("/guilds/", "Guild roster", "Your guild's shared characters, filterable by class, role, spec, profession, "
                                 "level and item level. Visible only to verified guild members."),
    ("/players", "Shared characters", "Characters whose owners shared their details with all members."),
    ("/events", "Events", "Join guild events with the character and role you're bringing."),
)


def _preview_card(request: Request, mp: str):
    from . import seo
    rest = request.url.path[len(mp):]
    title, desc = next(((t, d) for p, t, d in _PREVIEW_PAGES if rest.startswith(p)),
                       ("Members' area", "Sign in with Discord to plan WoW events, sign up with your characters "
                                         "and see characters your guild has shared."))
    site = request.app.state.web.cfg["branding"]["site_name"]
    return render(request, "message.html", {
        "title": title, "preview": True, "noindex": True,
        "seo": seo.meta(request, description=f"{desc} Sign in with Discord on {site}'s QuestersBoard."),
        "message": f"{desc} Log in with Discord to open it.",
        "retry_url": f"{mp}/login?next={request.url.path}"}, public=True)


__all__ = ["router", "render", "install_login_redirect", "now"]
