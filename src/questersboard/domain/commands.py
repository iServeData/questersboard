"""What the /board, /craft who, /character refresh and /questersboard help slash commands say.

Plain functions over a session, so the replies are tested without Discord. Every one follows the
same rules as the web pages: members only, posts as listed on the boards, crafters only from
Professions shared with all members, refreshes only of the member's own characters.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import BoardPost, BoardResponse, Character, CharacterProfile
from ..sorting import alpha
from . import boards, outbox
from .actor import Actor
from .errors import Invalid, NotFound
from .permissions import require_member
from .roster import ensure_member
from .timeutil import as_local, now

REFRESH_JOB = "web.blizzard_refresh"           # run by the web service (web/web_jobs.py)
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"), 1)}
_DAYS = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?"
_RANGE = re.compile(r"(\d{1,2}):?(\d{2})\s*(?:-|\u2013|\u2014|to)\s*(\d{1,2}):?(\d{2})\s*$", re.I)
# What /board post can start, labelled as on the Post a goal form.
ADVENTURE_KINDS = {"need_help": "Looking for help", "offer_help": "Happy to help", "company": "Looking for company"}


def member_url(cfg: Config, *parts: str) -> str:
    return "/".join([cfg["community"]["public_base_url"].rstrip("/") + cfg["web"]["member_path"].rstrip("/"),
                     *parts])


def _md(text: str) -> str:
    return "".join("\\" + c if c in "[]()*_~`|\\<>#" else c for c in str(text))


# --------------------------------------------------------------------------- /board list, /board my

def board_list(s: Session, cfg: Config, actor: Actor, board: str) -> str:
    """Open posts on one board, newest first, at most [commands] board_list_max."""
    if board not in boards.BOARDS:
        raise Invalid("Pick the Adventure Board or Profession help.")
    if not boards.enabled(cfg, board):
        raise Invalid(f"The {boards.BOARD_LABEL[board]} is turned off.")
    posts = [v for v in boards.listing(s, cfg, actor, board) if v.p.state == "open"]
    page = member_url(cfg, boards.BOARDS[board])
    if not posts:
        return f"Nothing open on the {boards.BOARD_LABEL[board]} yet. Post one with `/board post` or at <{page}>"
    most = cfg["commands"]["board_list_max"]
    n = len(posts)
    lines = [f"**{boards.BOARD_LABEL[board]}: {n} open {'post' if n == 1 else 'posts'}**"]
    for v in posts[:most]:
        bits = [v.character.name if v.character else ""] + list(v.count_text)
        best = next((t for t in v.times if t["best"]), None)
        if best:
            bits.append(f"most people: {best['text']} (+{best['n']})")
        lines.append(f"\u2022 **{v.kind_label}** \u00b7 [{_md(v.p.title)}](<{member_url(cfg, 'board', str(v.p.public_id))}>)\n"
                     f"  {' \u00b7 '.join(b for b in bits if b)}")
    if n > most:
        lines.append(f"\u2026and {n - most} more.")
    lines.append(f"All of them: <{page}>")
    return "\n".join(lines)


def board_my(s: Session, cfg: Config, actor: Actor) -> str:
    """The member's open posts on both boards, and open posts they've answered."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    mine = list(s.scalars(select(BoardPost).where(BoardPost.member_id == actor.member_id, BoardPost.state == "open")
                          .order_by(BoardPost.expires_at)))
    answered = s.execute(select(BoardPost, BoardResponse).join(BoardResponse, BoardResponse.post_id == BoardPost.id)
                         .where(BoardResponse.member_id == actor.member_id, BoardPost.state == "open",
                                BoardPost.member_id != actor.member_id)
                         .order_by(BoardPost.expires_at)).all()
    if not mine and not answered:
        return ("You have no open posts and haven't answered any. Try `/board list`, or post one with "
                "`/board post`.")

    def link(p):
        return f"[{_md(p.title)}](<{member_url(cfg, 'board', str(p.public_id))}>)"
    lines = []
    if mine:
        lines.append("**Your posts**")
        for p in mine:
            v = boards.view(s, cfg, actor, p)
            extra = " \u00b7 ".join([boards.BOARD_LABEL[p.board]] + list(v.count_text))
            lines.append(f"\u2022 {link(p)} \u00b7 {extra}")
    if answered:
        lines.append("**You answered**")
        seen = set()
        for p, r in answered:
            if p.id in seen:
                continue
            seen.add(p.id)
            label = boards.RESPONSES.get(p.kind, {}).get(r.kind, r.kind)
            when = ""
            wins = boards.windows_of(p)
            if wins and r.kind in boards.TIME_KINDS and r.windows is not None and len(r.windows) < len(wins):
                when = " \u00b7 " + ", ".join(boards.when_text(*wins[i], p.timezone) for i in r.windows if i < len(wins))
            lines.append(f"\u2022 {link(p)} \u00b7 {label}{when}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- /board post

def parse_times(text: str, today: date) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Lines like "Sat 28 Nov 19:00-22:00", "Nov 28 19:00 to 22:00" or "2026-11-28 1900-2200" ->
    ([(YYYY-MM-DD, HH:MM, HH:MM)], lines not understood). 24-hour only; a date without a year is the
    next one from today. At most boards.MAX_WINDOWS; extra lines count as not understood."""
    got, bad = [], []
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        m = _RANGE.search(line)
        d = _date_part(line[:m.start()] if m else "", today) if m else None
        if not m or d is None or re.search(r"\b(am|pm)\b", line, re.I) or len(got) >= boards.MAX_WINDOWS:
            bad.append(line[:60])
            continue
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59 or (h2 == 24 and m2):
            bad.append(line[:60])
            continue
        got.append((d.isoformat(), f"{h1:02d}:{m1:02d}", f"{h2 % 24:02d}:{m2:02d}"))
    return got, bad


def _date_part(text: str, today: date) -> date | None:
    t = re.sub(rf"^\s*{_DAYS}\s*", "", text.strip().lower()).strip(" ,")
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{1,2})\s+([a-z]{3})[a-z]*\.?(?:\s+(\d{4}))?", t):
            day, mon, year = int(m[1]), MONTHS.get(m[2]), m[3]
        elif m := re.fullmatch(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?", t):
            day, mon, year = int(m[2]), MONTHS.get(m[1]), m[3]
        else:
            return None
        if mon is None:
            return None
        if year:
            return date(int(year), mon, day)
        d = date(today.year, mon, day)
        return d if d >= today else date(today.year + 1, mon, day)
    except ValueError:
        return None


def post_link(cfg: Config, kind: str, title: str, body: str, times: list[tuple[str, str, str]], tz: str) -> str:
    """The Adventure Board's Post a goal form, opened and filled in (nothing is saved until Post goal)."""
    if kind not in ADVENTURE_KINDS:
        raise Invalid("Pick what kind of goal this is.")
    q = {"new": "1", "kind": kind, "title": title.strip()[:120], "body": " ".join(body.split())[:500],
         "timezone": tz}
    for i, (d, a, b) in enumerate(times[:boards.MAX_WINDOWS]):
        q.update({f"date_{i}": d, f"from_{i}": a, f"to_{i}": b})
    return member_url(cfg, "adventures") + "?" + urlencode({k: v for k, v in q.items() if v})


def post_reply(cfg: Config, kind: str, title: str, body: str, times_text: str, tz: str,
               today: date | None = None) -> str:
    """The /board post answer: what was understood, and the link that opens the filled-in form."""
    today = today or as_local(now(), tz).date()
    times, bad = parse_times(times_text, today)
    lines = [f"**{_md(title.strip())}** is filled in on the Adventure Board's Post a goal form. "
             "Pick your character there and press **Post goal**; it appears in the board's channel once posted."]
    for d, a, b in times:
        start = datetime.fromisoformat(f"{d}T{a}")
        end = datetime.fromisoformat(f"{d}T{b}")
        if end <= start:
            end += timedelta(days=1)
        lines.append(f"\u2022 {boards.when_text(_aware(start, tz), _aware(end, tz), tz)}")
    if bad:
        lines.append("Not understood (add them on the form): " + "; ".join(f"\"{_md(x)}\"" for x in bad)
                     + ". Write times like Sat 28 Nov 19:00-22:00, 24-hour.")
    lines.append(f"Finish here: <{post_link(cfg, kind, title, body, times, tz)}>")
    return "\n".join(lines)


def _aware(local: datetime, tz: str) -> datetime:
    from .timeutil import resolve_local
    try:
        return resolve_local(local, tz)
    except Exception:  # noqa: BLE001 - a daylight-saving edge: show it as if one fold; the form checks it
        return resolve_local(local, tz, fold=0)


# --------------------------------------------------------------------------- /craft who

def _crafter_rows(s: Session, cfg: Config, actor: Actor):
    from . import profiles
    return profiles.shared_with_members(s, cfg, actor)


def craft_suggest(s: Session, cfg: Config, actor: Actor, current: str) -> list[tuple[str, str]]:
    """(item, profession) pairs for the item box, A to Z, from recipes shared with all members."""
    q = " ".join((current or "").split()).casefold()
    seen: dict[str, str] = {}
    for r in _crafter_rows(s, cfg, actor):
        for x in r.recipes:
            if not q or q in x["name"].casefold():
                seen.setdefault(x["name"], x["prof"])
    return alpha(seen.items(), lambda kv: kv[0])[:25]


def craft_who(s: Session, cfg: Config, actor: Actor, item: str) -> tuple[str, str]:
    """(reply, Ask for a craft link). Exact name matches first; otherwise everything containing it."""
    from . import profiles
    q = profiles.craft_query(item)
    if len(q) < 2:
        raise Invalid("Type at least two letters of the item.")
    rows = profiles.filter_rows(_crafter_rows(s, cfg, actor), {"craft": q})
    exact = [r for r in rows if any(x["name"].casefold() == q.casefold() for x in r.crafts)]
    rows = exact or rows
    ask = member_url(cfg, "professions") + "?" + urlencode({"new": "need", "title": q})
    if not rows:
        return (f"Nobody who shares their professions has **{_md(q)}** yet. Ask for it on Profession help and "
                "crafters will see it.", ask)
    offers = {(p.character_id, (p.recipe_name or p.title).casefold()): p for p in s.scalars(
        select(BoardPost).where(BoardPost.kind == "can_craft", BoardPost.state == "open"))}
    names = sorted({x["name"] for r in rows for x in r.crafts}, key=str.casefold)
    head = names[0] if len(names) == 1 else q
    most = cfg["commands"]["craft_who_max"]
    lines = [f"**{len(rows)} {'person' if len(rows) == 1 else 'people'} can craft {_md(head)}**"]
    for r in alpha(rows, lambda r: r.ch.name)[:most]:
        x = next((c for c in r.crafts if c["name"].casefold() == q.casefold()), r.crafts[0])
        bits = [x["prof"], cfg.edition(r.ch.edition).name]
        if len(names) > 1:
            bits.insert(0, x["name"])
        offer = offers.get((r.ch.id, x["name"].casefold()))
        if offer is not None:
            bits.append(f"offers it: {boards.fee_text(offer)}")
        lines.append(f"\u2022 **{_md(r.ch.name)}** \u00b7 " + " \u00b7 ".join(_md(b) for b in bits if b))
    if len(rows) > most:
        lines.append(f"\u2026and {len(rows) - most} more on Who can craft.")
    lines.append("Only characters whose owners share their professions with all members are listed.")
    return "\n".join(lines), ask


def craft_page(cfg: Config, item: str) -> str:
    """Who can craft on the site, searched for the item."""
    from .profiles import craft_query
    return member_url(cfg, "players") + "?" + urlencode({"craft": craft_query(item)}) + "#craft"


# --------------------------------------------------------------------------- /character refresh

def refresh_choices(s: Session, cfg: Config, actor: Actor) -> list[tuple[int, str]]:
    """The member's characters that have Blizzard details to refresh, A to Z."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    rows = s.execute(select(Character).join(CharacterProfile, CharacterProfile.character_id == Character.id)
                     .where(Character.member_id == actor.member_id, Character.archived_at.is_(None))).scalars()
    out = []
    for c in rows:
        ed = cfg.edition(c.edition)
        if ed.capability("character") == "unsupported":
            continue
        out.append((c.id, f"{c.name} \u00b7 {ed.name}"))
    return alpha(out, lambda x: x[1])[:25]


def refresh_request(s: Session, cfg: Config, actor: Actor, character_id: int) -> tuple[str, str, datetime]:
    """Ask the worker to refresh one of the member's characters. Returns (state, name, fetched_at before):
    "queued", or "recent" when it was refreshed within [commands] refresh_again_minutes."""
    require_member(cfg, actor)
    if not cfg["blizzard"].get("enabled"):
        raise Invalid("Refresh from Blizzard is turned off here.")
    actor = ensure_member(s, actor)
    ch = s.get(Character, character_id)
    prof = s.get(CharacterProfile, character_id)
    if ch is None or ch.member_id != actor.member_id or ch.archived_at is not None or prof is None:
        raise NotFound("That isn't one of your characters with Blizzard details. Bring them in on My characters first.")
    again = timedelta(minutes=cfg["commands"]["refresh_again_minutes"])
    if again and prof.fetched_at and now() - prof.fetched_at < again:
        return "recent", ch.name, prof.fetched_at
    outbox.enqueue(s, REFRESH_JOB, str(ch.id), {"character_id": ch.id, "member_id": actor.member_id})
    return "queued", ch.name, prof.fetched_at


def refresh_state(s: Session, character_id: int, before: datetime | None) -> str:
    """"done" once the details are newer than before; "failed" if the job gave up; else "waiting"."""
    prof = s.get(CharacterProfile, character_id)
    if prof is not None and prof.fetched_at and (before is None or prof.fetched_at > before):
        return "done"
    from ..models import Outbox
    job = s.scalars(select(Outbox).where(Outbox.kind == REFRESH_JOB, Outbox.key == str(character_id))
                    .order_by(Outbox.id.desc()).limit(1)).first()
    if job is not None and job.dead_at is not None:
        return "failed"
    return "waiting"


def refresh_done_text(s: Session, cfg: Config, character_id: int) -> str:
    ch = s.get(Character, character_id)
    lvl = f": level {ch.level}" if ch is not None and ch.level else ""
    return (f"Refreshed **{_md(ch.name if ch else 'your character')}** from Blizzard{lvl}. Your sharing choices "
            f"haven't changed. <{member_url(cfg, 'characters', str(character_id), 'view')}>")


def run_refresh(s: Session, cfg: Config, bz, character_id: int, member_id: int) -> str:
    """The web service's half of /character refresh: the owner's own Refresh from Blizzard, run as the system
    actor scoped to the owner. "refreshed", or "skipped" when the character changed hands or went."""
    from . import profiles
    prof = s.get(CharacterProfile, character_id)
    ch = s.get(Character, character_id)
    if prof is None or ch is None or ch.archived_at is not None or prof.member_id != member_id:
        return "skipped"
    with profiles._fresh(bz):
        profiles.operator_refresh(s, cfg, bz, character_id)
    return "refreshed"


# --------------------------------------------------------------------------- /questersboard help

def help_text(cfg: Config) -> str:
    lines = ["**QuestersBoard quick guide**",
             "**Characters**: `/character add` `/character list` `/character refresh` `/character edit` "
             "`/character remove`",
             "**Events**: `/quest list` `/quest my` `/quest join` `/quest leave` `/quest create`"]
    goals = []
    if boards.enabled(cfg, "adventure") or boards.enabled(cfg, "profession"):
        goals += ["`/board list`", "`/board my`"]
    if boards.enabled(cfg, "adventure"):
        goals.append("`/board post`")
    if cfg["blizzard"].get("enabled"):
        goals.append("`/craft who`")
    if goals:
        lines.append("**Goals and crafting**: " + " ".join(goals))
    if cfg["voice"].get("enabled"):
        lines.append("**Event voice**: `/voice invite` for a guest link; hosts also have `/voice kick` "
                     "`/voice ban` `/voice unban`")
    lines.append("Buttons on posts do the rest: join or leave events, and answer goals and craft posts, "
                 "picking the times that work for you.")
    lines.append(f"How it works: <{member_url(cfg, 'help')}> \u00b7 Privacy: <{member_url(cfg, 'privacy')}>")
    return "\n".join(lines)
