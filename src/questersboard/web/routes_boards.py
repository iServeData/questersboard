"""Member pages for the Adventure Board (/adventures), profession help (/professions) and one post
(/board/<id>). Every write goes through domain.boards, the same code the Discord buttons use."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from fastapi import Request
from sqlalchemy import select

from ..domain import boards, roster
from ..domain.errors import Invalid, NotFound
from ..domain.events import ACTIVITY_TYPES
from ..domain.permissions import has
from ..domain.timeutil import as_local, now
from ..models import Character, Member

ACTIVITY_LABELS = {"raid": "Raid", "dungeon": "Dungeon", "quest": "Questing", "rp": "Roleplay", "arena": "Arena",
                   "battleground": "Battleground", "world_pvp": "World PvP", "lfg": "Looking for group",
                   "social": "Social", "custom": "Something else"}
PAGES = {"adventure": "adventures", "profession": "professions"}
TIME_PATTERN = "([01][0-9]|2[0-3]):[0-5][0-9]"


def add(r, mp: str, W, session_or_login, post_guard, render, redirect, local_dt) -> None:

    def _filters(request: Request, board: str) -> dict:
        keys = (("kind", "activity", "edition", "tag", "fits") if board == "adventure"
                else ("kind", "profession", "edition", "can_help"))
        return {k: request.query_params.get(k, "") for k in keys}

    def _page(request: Request, board: str, form_error: str = "", form: dict | None = None, bad: str = ""):
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            a = roster.ensure_member(s, a)
            if not boards.enabled(w.cfg, board):
                raise NotFound(f"The {boards.BOARD_LABEL[board]} is turned off.")
            f = _filters(request, board)
            posts = boards.listing(s, w.cfg, a, board, f)
            chars = list(s.scalars(select(Character).where(Character.member_id == a.member_id,
                                                           Character.archived_at.is_(None)).order_by(Character.name)))
            m = s.get(Member, a.member_id)
            tz = (m.timezone if m is not None and getattr(m, "timezone", None) else None) or \
                w.cfg["community"]["default_timezone"]
            sec = boards.section(w.cfg, board)
            keep = {k: v for k, v in f.items() if v and k != "kind"}
            chips = [("", "All")] + list(boards.KINDS[board].items())
            ctx = {"title": boards.BOARD_LABEL[board], "board": board, "posts": posts, "f": f, "chars": chars,
                   "kinds": boards.KINDS[board], "chips": [(k, lbl, "?" + urlencode({**keep, **({"kind": k} if k else {})}))
                                                          for k, lbl in chips],
                   "mine": boards.my_posts(s, a, board), "open_n": boards.open_count(s, a.member_id, board),
                   "sec": sec, "fee_kinds": boards.FEE_KINDS, "editions": w.cfg.editions, "tz": tz, "form_error": form_error, "form": form or {}, "bad": bad,
                   "open_form": bool(form_error) or request.query_params.get("new", "") in ("1", "need_crafted", "can_craft", "ask"),
                   "new_q": request.query_params.get("new", ""),
                   "at_text": boards.at_text, "time_pattern": TIME_PATTERN,
                   "today": as_local(now(), tz).date().isoformat(),
                   "last_day": as_local(now() + timedelta(days=sec.get("open_days", 14)), tz).date().isoformat()}
            if board == "adventure":
                ctx["activities"] = {k: ACTIVITY_LABELS.get(k, k.title()) for k in ACTIVITY_TYPES}
            else:
                ctx["could_help"] = boards.could_help(s, w.cfg, a)
                ctx["craftable"] = boards.craftable_choices(s, a.member_id)
            return render(request, f"{PAGES[board]}.html", ctx)

    @r.get(mp + "/adventures")
    def adventures_page(request: Request):
        return _page(request, "adventure")

    @r.get(mp + "/professions")
    def professions_page(request: Request):
        return _page(request, "profession")

    def _windows(form, tz: str) -> list:
        out = []
        for i in range(boards.MAX_WINDOWS):
            d, a, b = (str(form.get(f"{k}_{i}") or "").strip() for k in ("date", "from", "to"))
            if not (d or a or b):
                continue
            if not (d and a and b):
                raise Invalid("For each time that works, give the date, a start and an end (24-hour, like 19:00).",
                              field=f"time_{i}")
            try:
                start = local_dt(d, a, tz)
                end = local_dt(d, b, tz)
            except Invalid as e:
                raise Invalid(e.message, field=f"time_{i}") from None
            if end <= start:
                end += timedelta(days=1)          # 22:00 to 01:00 runs past midnight
            out.append((start, end, i))
        return out

    def _keep(form) -> dict:
        """What the member typed, so a form with a problem comes back filled in."""
        keys = ["kind", "character_id", "title", "body", "activity_type", "group_size", "timezone",
                "profession", "recipe_name", "craft", "craft_character", "fee", "fee_gold", "fee_silver", "fee_copper"]
        keys += [f"{k}_{i}" for i in range(boards.MAX_WINDOWS) for k in ("date", "from", "to")]
        out = {k: str(form.get(k) or "") for k in keys}
        out["tags"] = list(form.getlist("tags"))
        return out

    async def _create(request: Request, board: str):
        form, a = await post_guard(request)
        w = W(request)
        tz = str(form.get("timezone") or w.cfg["community"]["default_timezone"])
        data = {k: form.get(k) for k in ("kind", "character_id", "title", "body", "activity_type", "group_size",
                                         "profession", "recipe_name", "fee", "fee_gold", "fee_silver", "fee_copper")}
        data["timezone"] = tz
        if board == "adventure":
            data["tags"] = form.getlist("tags")
            try:
                data["windows"] = _windows(form, tz)
            except Invalid as e:
                resp = _page(request, board, form_error=str(e), form=_keep(form), bad=e.detail.get("field", ""))
                resp.status_code = 400
                return resp
        elif data.get("kind") == "can_craft":
            # the Can craft picker sends "<character id>:<recipe name>"
            cid, _, name = str(form.get("craft") or "").partition(":")
            if cid.isdigit():
                data["character_id"], data["recipe_name"] = int(cid), name
            picked = str(form.get("craft_character") or "")
            if picked and picked != cid:                  # without the page script both lists show everything
                resp = _page(request, board, form_error="Pick an item the character you chose can craft.",
                             form=_keep(form), bad="craft")
                resp.status_code = 400
                return resp
        try:
            with w.db.tx() as s:
                p = boards.create(s, w.cfg, a, board, data)
                pid = p.public_id
        except Invalid as e:
            resp = _page(request, board, form_error=str(e), form=_keep(form), bad=e.detail.get("field", ""))
            resp.status_code = 400
            return resp
        return redirect(f"{mp}/board/{pid}")

    @r.post(mp + "/adventures")
    async def adventures_create(request: Request):
        return await _create(request, "adventure")

    @r.post(mp + "/professions")
    async def professions_create(request: Request):
        return await _create(request, "profession")

    def _post_page(request: Request, w, s, a, p, edit: dict | None = None, status: int = 200):
        """A post's own page. edit: {"error", "bad", "form"} when Edit couldn't be saved (it opens again
        with what was typed)."""
        if not boards.enabled(w.cfg, p.board):
            raise NotFound(f"The {boards.BOARD_LABEL[p.board]} is turned off.")
        v = boards.view(s, w.cfg, a, p)
        first = next(((x, y) for x, y in boards.windows_of(p) if y > now()), None)
        start = as_local(first[0], p.timezone) if first else as_local(now() + timedelta(days=1), p.timezone)
        mins = int((first[1] - first[0]).total_seconds() // 60) if first else 120
        c = boards.coins(p.fee_copper) if p.fee_copper else {}
        current = {"body": p.body, "fee": p.fee_kind or "mats",
                   **{f"fee_{k}": (c.get(k) if c.get(k) not in (None, "0") else "") for k in ("gold", "silver", "copper")}}
        resp = render(request, "board_post.html", {
            "title": p.title, "v": v, "p": p, "board_label": boards.BOARD_LABEL[p.board],
            "page": PAGES[p.board], "board_page": PAGES[p.board], "at_text": boards.at_text, "time_pattern": TIME_PATTERN,
            "plan_date": start.date().isoformat(), "plan_time": start.strftime("%H:%M"),
            "plan_minutes": max(15, min(mins, 720)), "fee_kinds": boards.FEE_KINDS,
            "max_fee_gold": boards.section(w.cfg, p.board).get("max_fee_gold", 99999),
            "edit": edit or {}, "edit_form": (edit or {}).get("form") or current,
            "is_mod": has(w.cfg, a, "event_moderator"), "editions": w.cfg.editions})
        resp.status_code = status
        return resp

    @r.get(mp + "/board/{public_id}")
    def board_post_page(request: Request, public_id: str):
        sess, a = session_or_login(request)
        w = W(request)
        with w.db.tx() as s:
            a = roster.ensure_member(s, a)
            return _post_page(request, w, s, a, boards.get(s, public_id))

    @r.get(mp + "/board/{public_id}/icon")
    def board_post_icon(request: Request, public_id: str):
        """The icon of a profession post's item (Blizzard's game art, the same for everyone), fetched
        by QuestersBoard so pages load images only from this site."""
        from fastapi.responses import Response
        sess, a = session_or_login(request, bucket="i")
        w = W(request)
        with w.db.tx() as s:
            p = boards.get(s, public_id)
            if not boards.enabled(w.cfg, p.board):
                raise NotFound("Icon not found.")
            got = boards.recipe_details(s, p)
            url = str((got or {}).get("r", {}).get("icon") or "")
        hosts = set(w.cfg["blizzard"].get("render_image_hosts", ["render.worldofwarcraft.com"]))
        from urllib.parse import urlsplit
        if not url.startswith("https://") or urlsplit(url).hostname not in hosts:
            raise NotFound("Icon not found.")
        img = w.bz.fetch_icon(url)
        if img is None:
            raise NotFound("Blizzard didn't send this icon.")
        return Response(img[0], media_type=img[1], headers={"Cache-Control": "private, max-age=86400"})

    @r.post(mp + "/board/{public_id}/{action}")
    async def board_post_action(request: Request, public_id: str, action: str):
        form, a = await post_guard(request)
        w = W(request)
        if action == "edit":
            data = {k: str(form.get(k) or "") for k in ("body", "fee", "fee_gold", "fee_silver", "fee_copper")}
            try:
                with w.db.tx() as s:
                    boards.edit(s, w.cfg, a, public_id, data)
            except Invalid as e:
                with w.db.tx() as s:
                    me = roster.ensure_member(s, a)
                    return _post_page(request, w, s, me, boards.get(s, public_id), status=400, edit={
                        "error": str(e), "bad": e.detail.get("field", ""), "form": data})
            return redirect(f"{mp}/board/{public_id}")
        with w.db.tx() as s:
            if action == "respond":
                boards.respond(s, w.cfg, a, public_id, str(form.get("kind") or ""),
                               want=form.get("want", "1") != "0", note=str(form.get("note") or ""))
            elif action in ("done", "remove"):
                p = boards.close(s, w.cfg, a, public_id, "done" if action == "done" else "removed")
                if action == "remove":
                    return redirect(f"{mp}/{PAGES[p.board]}")
            elif action == "extend":
                boards.extend(s, w.cfg, a, public_id)
            elif action == "plan":
                p = boards.get(s, public_id)
                tz = str(form.get("timezone") or p.timezone)
                start = local_dt(str(form.get("date") or ""), str(form.get("start_time") or ""), tz)
                mins = max(15, min(int(form.get("minutes") or 120), 12 * 60))
                ev = boards.plan_event(s, w.cfg, a, public_id, start, start + timedelta(minutes=mins), tz)
                return redirect(f"{mp}/events/{ev.public_id}/edit")
            else:
                raise NotFound("Unknown action.")
        back = str(form.get("back") or "")
        if back in ("adventures", "professions"):
            return redirect(f"{mp}/{back}")
        return redirect(f"{mp}/board/{public_id}")
