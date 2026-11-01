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
from ..domain import (calendar, catalog, chronicle, events, permissions, recommendations, recurrence, roster,
                      scheduling, signups)
from ..domain.errors import Invalid, NotFound, PermissionDenied
from ..domain.events import ACTIVITY_TYPES
from ..domain.timeutil import as_local, now, resolve_local
from ..domain import audit
from ..models import (Character, ChronicleEntry, ChronicleUpload, Event, EventCohost, Member, Signup)

log = logging.getLogger("wowquester.web")
FIELDS_TEXT = ("title", "description", "eligibility")


def render(request: Request, name: str, ctx: dict, status: int = 200, public: bool = False) -> HTMLResponse:
    w = request.app.state.web
    sess = getattr(request.state, "sess", None) if not public else None
    if sess is None and not public:
        sess = w.load_session(request)
    a = w.actor(sess) if sess else None
    cfg = w.cfg
    base = dict(request=request, cfg=cfg, b=cfg["branding"], mp=cfg["web"]["member_path"].rstrip("/"),
                cp=cfg["chronicle"]["public_path"].rstrip("/"), actor=a, csrf=sess.csrf if sess else "",
                can=(lambda cap: a is not None and permissions.has(cfg, a, cap)), public=public)
    base.update(ctx)
    return request.app.state.tpl.TemplateResponse(request, name, base, status_code=status)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


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
    if form.get("participant_limit") is not None:
        d["participant_limit"] = form.get("participant_limit") or None
    for k in ("late_join", "chronicle_enabled", "public_listing"):
        d[k] = _bool(form, k)
    if form.get("has_exclusive"):          # field only rendered for the exclusive-designator role
        d["exclusive"] = _bool(form, "exclusive")
    d["roles"] = _roles_from_form(form)
    if form.get("cohosts") is not None:    # field only rendered for the creator / moderators
        d["cohosts"] = [x.strip() for x in re.split(r"[,\s]+", form.get("cohosts", "")) if x.strip()]
    return d


def router(cfg: Config) -> APIRouter:
    r = APIRouter()
    mp = cfg["web"]["member_path"].rstrip("/")

    def W(request):
        return request.app.state.web

    def session_or_login(request: Request, fresh: bool = False):
        w = W(request)
        sess = w.load_session(request)
        request.state.sess = sess
        a = w.actor(sess, fresh=fresh)
        if a is None:
            raise _Login()
        if not w.rate_ok(f"m:{a.discord_id}", w.cfg["rate_limits"].get("web_requests_per_minute", 120)):
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
        state = pysecrets.token_urlsafe(24)
        resp = _redirect("about:blank")
        safe_next = next if next.startswith(mp + "/") and "//" not in next else mp + "/"
        w.new_session(resp, None, {"oauth_state": state, "next": safe_next})
        resp.headers["location"] = w.oauth.authorize_url(
            w.cfg["community"]["public_base_url"].rstrip("/") + mp + "/oauth/callback", state)
        return resp

    @r.get(mp + "/oauth/callback")
    def callback(request: Request, code: str = "", state: str = ""):
        w = W(request)
        pre = w.load_session(request)
        if pre is None or pre.member_id is not None or not state or \
                not pysecrets.compare_digest(state, pre.data.get("oauth_state", "")):
            raise PermissionDenied("That login link expired. Please try logging in again.")
        nxt = pre.data.get("next", mp + "/")
        resp = _redirect(nxt)
        w.drop_session(request, resp)           # state is single-use
        discord_id, name = w.oauth.identify(code, w.cfg["community"]["public_base_url"].rstrip("/") +
                                            mp + "/oauth/callback")
        in_guild, roles, gname = (w.rest.member(w.cfg.guild_id, discord_id) if w.rest else (False, frozenset(), ""))
        from ..domain.actor import Actor
        a = Actor(discord_id=discord_id, source="web", role_ids=roles, in_guild=in_guild, display_name=gname or name)
        permissions.require_member(w.cfg, a)
        with w.db.tx() as s:
            a = roster.ensure_member(s, a)
        w.new_session(resp, a.member_id, {"discord_id": discord_id, "name": a.display_name, "roles": sorted(roles),
                                          "in_guild": in_guild, "roles_at": __import__("time").time()})
        w.cleanup_sessions()
        log.info("login member=%s", a.member_id)
        return resp

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
            else:
                drafts = []
        return render(request, "home.html", {"title": "Quests", "items": items, "mine": mine, "recs": recs,
                                             "drafts": drafts})

    # ------------------------------------------------------------- events
    def _form_ctx(w, a, ev=None):
        with w.db.tx() as s:
            chars = roster.list_characters(s, a.member_id)
        return {"editions": w.cfg.editions, "activity_types": ACTIVITY_TYPES, "presets": w.cfg.role_presets,
                "ev": ev, "chars": chars, "tz": w.cfg["community"]["default_timezone"],
                "max_roles": MAX_ROLE_ROWS}

    @r.get(mp + "/events/new")
    def new_event(request: Request):
        sess, a = session_or_login(request)
        return render(request, "event_form.html", {"title": "New event", **_form_ctx(W(request), a)})

    @r.post(mp + "/events/new")
    async def create_event(request: Request):
        form, a = await post_guard(request)
        w = W(request)
        data = event_data_from_form(w.cfg, form)
        with w.db.tx() as s:
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
            fit = [c for c in chars if c.edition == ev.edition and c.region == ev.region
                   and (not ev.ruleset or c.ruleset == ev.ruleset)
                   and (not ev.realm_slug or c.realm_slug == ev.realm_slug)
                   and (not ev.faction or c.faction == ev.faction)]
            entry = s.scalar(select(ChronicleEntry).where(ChronicleEntry.event_id == ev.id))
            conflicts = []
            if manager and ev.state == "draft":
                conflicts = scheduling.preview_conflicts(s, ev.start_at, ev.end_at, ev.exclusive, ev.id,
                                                         {ev.creator_id} | cohosts, w.cfg.max_simultaneous)
            me = s.get(Member, a.member_id) if a and a.member_id else None
            ctx = {"title": ev.title, "ev": ev, "rows": rows, "mine": mine, "fit": fit, "manager": manager,
                   "recognition_default": bool(me and me.recognition_default),
                   "entry": entry, "conflicts": conflicts, "ed": w.cfg.edition(ev.edition),
                   "chars": chars, "editions": w.cfg.editions,
                   "show_names": manager or (a is not None and roster.staff_can_view_roster(w.cfg, a))}
            return render(request, "event.html", ctx)   # rendered inside the session (lazy loads)

    @r.get(mp + "/events/{public_id}/edit")
    def edit_event(request: Request, public_id: str):
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            ev = ev_by_public(s, public_id)
            permissions.require_event_manager(w.cfg, a, ev.creator_id, scheduling.cohost_ids(s, ev.id))
            co = list(s.scalars(select(Member.discord_id).join(EventCohost, EventCohost.member_id == Member.id)
                                .where(EventCohost.event_id == ev.id)))
            return render(request, "event_form.html", {"title": "Edit event", "cohost_ids": " ".join(co),
                                                       **_form_ctx(w, a, ev)})

    @r.post(mp + "/events/{public_id}/edit")
    async def save_event(request: Request, public_id: str):
        form, a = await post_guard(request)
        w = W(request)
        data = event_data_from_form(w.cfg, form, partial=True)
        with w.db.tx() as s:
            ev = ev_by_public(s, public_id)
            if form.get("scope") == "future" and ev.series_id:
                recurrence.edit_future(s, w.cfg, a, ev.id, {k: v for k, v in data.items()
                                                            if k not in ("start_at", "end_at")},
                                       shift_local_time=form.get("start_time") or None)
            elif ev.series_id:
                recurrence.edit_occurrence(s, w.cfg, a, ev.id, data)
            else:
                events.update(s, w.cfg, a, ev.id, data)
        return _redirect(f"{mp}/events/{public_id}")

    ACTIONS = ("join", "tentative", "leave", "reconfirm", "change", "publish", "start", "end", "cancel",
               "discard", "copy", "attendance", "extend")

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
            elif action == "leave":
                signups.leave(s, c, a, ev.id)
            elif action == "reconfirm":
                signups.reconfirm(s, c, a, ev.id)
            elif action == "change":
                rec = form.get("recognition")
                signups.change(s, c, a, ev.id, character_id=int(form["character_id"]) if form.get("character_id") else None,
                               role_key=form.get("role_key") or None,
                               recognition=None if rec in (None, "") else _bool(form, "recognition"))
            elif action == "publish":
                events.publish(s, c, a, ev.id)
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
    def characters(request: Request):
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            chars = roster.list_characters(s, a.member_id)
            race_map = {ed: catalog.race_class_map(s, ed) for ed in w.cfg.editions}
            from ..models import BattlenetLink
            bn_linked = s.query(BattlenetLink.member_id).filter(BattlenetLink.member_id == a.member_id).first() is not None
        bz = w.cfg["blizzard"]
        return render(request, "characters.html", {"title": "Characters", "chars": chars, "race_map": race_map,
                                                   "editions": w.cfg.editions, "presets": w.cfg.role_presets,
                                                   "bz_enabled": bool(bz.get("enabled")),
                                                   "bn_enabled": bool(bz.get("enabled") and bz.get("battlenet_link_enabled")),
                                                   "bn_linked": bn_linked})

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
        with w.db.tx() as s:
            if form.get("character_id"):
                roster.update_character(s, w.cfg, a, int(form["character_id"]), data)
            else:
                roster.add_character(s, w.cfg, a, data)
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
                n = w.bz.link_account(s, a.member_id, edition, region, code,
                                      w.cfg["community"]["public_base_url"].rstrip("/") + mp + "/battlenet/callback")
            except (Unsupported, ApiError) as exc:
                raise Invalid(str(exc)) from None
            audit.record(s, a, "character.ownership_link", "member", a.member_id, edition=edition, verified=n)
        return render(request, "message.html", {"title": "Battle.net linked", "message":
                      f"{n} of your characters matched your Battle.net account and are now marked as yours.",
                      "back": mp + "/characters"})

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
                                    timezone=form.get("timezone") or None)
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
            from ..models import Attendance
            att = {x.member_id: x.status for x in s.scalars(select(Attendance).where(Attendance.event_id == ev.id))}
            hist = chronicle.history(s, e.id)
            return render(request, "chronicle_edit.html", {"title": "Chronicle", "e": e, "ev": ev, "rev": rev,
                                                       "ups": ups, "problems": problems, "rows": rows, "att": att,
                                                       "hist": hist, "outcomes": chronicle.OUTCOMES,
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
                                        [x for x in re.split(r"[,\s]+", form.get("coauthors", "")) if x])
            elif action == "suggest":
                chronicle.ai_suggest(s, w.cfg, a, entry_id)
            elif action == "clear":
                chronicle.clear_upload(s, w.cfg, a, int(form["upload_id"]), _bool(form, "cleared"))
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
            rows = list(s.execute(select(ChronicleEntry, Event).join(Event, Event.id == ChronicleEntry.event_id)
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
            items = [{"key": o.key, "label": o.label, "parent": o.parent_key} for o in rows]
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
        return _redirect(f"{mp}/login?next={request.url.path}")


__all__ = ["router", "render", "install_login_redirect", "now"]
