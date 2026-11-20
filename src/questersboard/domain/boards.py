"""The Adventure Board ([adventures]) and the profession help board ([professions]).

Members post before there's an event:
* Adventure Board: a goal they need help with, an offer to help, or looking for company. Others press
  "I need this too" / "I can help" / "I'm in". The poster can turn the post into a draft event; once it's
  published, everyone who answered gets a message with the link (nobody is signed up for them).
* Profession help: a crafted item they need, a recipe they can craft (checked against the character's
  Blizzard profile, and closed if Blizzard stops showing it), a question about where a recipe comes from,
  or company to farm. Crafters arrange trades in game; QuestersBoard never handles gold or materials.

Posts are members only (the website and the board's Discord channel). Each board's cards go to its
configured channel_id; empty means website only. Posts close by themselves (open_days / post_days).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import BoardPost, BoardResponse, Character, CharacterProfile, Event, Member
from . import audit, outbox
from .actor import Actor
from .errors import Invalid, InvalidTransition, NotFound, PermissionDenied
from .permissions import has, require_member
from .roster import ensure_member
from .timeutil import as_local, now

BOARDS = {"adventure": "adventures", "profession": "professions"}       # board -> config section
BOARD_LABEL = {"adventure": "Adventure Board", "profession": "Profession help"}
KINDS = {
    "adventure": {"need_help": "Need help", "offer_help": "Happy to help", "company": "Looking for company"},
    "profession": {"need_crafted": "Need crafted", "can_craft": "Can craft", "recipe": "Looking for recipe",
                   "farm": "Farm together"},
}
KIND_BOARD = {k: b for b, ks in KINDS.items() for k in ks}
# What others can press on each kind of post: response kind -> button label.
RESPONSES = {
    "need_help": {"need": "I need this too", "help": "I can help"},
    "company": {"need": "I'm in"},
    "offer_help": {"ask": "Ask for help"},
    "need_crafted": {"can_make": "I can make this", "need": "I need this too"},
    "can_craft": {"ask": "Ask to have it made"},
    "recipe": {"answer": "Answer"},
    "farm": {"need": "I'm interested"},
}
# How many pressed it: (one, several).
COUNTS = {
    ("need_help", "need"): ("{n} needs this too", "{n} need this too"),
    ("need_help", "help"): ("{n} can help", "{n} can help"),
    ("company", "need"): ("{n} is in", "{n} are in"),
    ("offer_help", "ask"): ("{n} asked", "{n} asked"),
    ("need_crafted", "can_make"): ("{n} can make this", "{n} can make this"),
    ("need_crafted", "need"): ("{n} other needs this", "{n} others need this"),
    ("can_craft", "ask"): ("{n} asked", "{n} asked"),
    ("recipe", "answer"): ("{n} answer", "{n} answers"),
    ("farm", "need"): ("{n} interested", "{n} interested"),
}
# Responses the poster is told about by direct message ([adventures]/[professions] notify_poster).
NOTIFY = {"help", "can_make", "ask", "answer"}
# Responses whose names everyone on the board sees (offers of help); the rest are counts except to the poster.
NAMED = {"help", "can_make", "answer"}
PLANNABLE = {"need_help", "company", "offer_help", "farm"}
CLOSED_TEXT = {"done": "Marked done", "removed": "Removed", "expired": "Ran out of time", "evented": "Became an event",
               "recipe_gone": "Blizzard no longer shows this recipe", "character": "The character was removed"}
MAX_WINDOWS = 3
MAX_WINDOW_HOURS = 12                 # default for [adventures] max_window_hours


def section(cfg: Config, board: str) -> dict:
    if board not in BOARDS:
        raise NotFound("Board not found.")
    return cfg[BOARDS[board]]


def enabled(cfg: Config, board: str) -> bool:
    return bool(section(cfg, board)["enabled"])


def _require_on(cfg: Config, board: str) -> None:
    if not enabled(cfg, board):
        raise NotFound(f"The {BOARD_LABEL[board]} is turned off.")


def get(s: Session, public_id: str | uuid.UUID, lock: bool = False) -> BoardPost:
    try:
        pid = public_id if isinstance(public_id, uuid.UUID) else uuid.UUID(str(public_id))
    except ValueError:
        raise NotFound("Post not found.") from None
    q = select(BoardPost).where(BoardPost.public_id == pid)
    p = s.scalar(q.with_for_update() if lock else q)
    if p is None or p.state == "removed":
        raise NotFound("Post not found.")
    return p


# --------------------------------------------------------------------------- time text

def when_text(start: datetime, end: datetime, tz: str) -> str:
    """'Tue 24 Nov 19:00 to 22:00 PST (03:00 to 06:00 UTC)': 24-hour, the poster's zone plus UTC."""
    a, b = as_local(start, tz), as_local(end, tz)
    ua, ub = start.astimezone(UTC), end.astimezone(UTC)
    day = a.strftime("%a %-d %b")
    other = "" if b.date() == a.date() else b.strftime("%a %-d %b ")
    return (f"{day} {a.strftime('%H:%M')} to {other}{b.strftime('%H:%M')} {a.tzname() or ''} "
            f"({ua.strftime('%H:%M')} to {ub.strftime('%H:%M')} UTC)").replace("  ", " ")


def at_text(t: datetime, tz: str) -> str:
    """'Thu 26 Nov, 18:40 PST (02:40 UTC)'."""
    d = as_local(t, tz)
    return f"{d.strftime('%a %-d %b, %H:%M')} {d.tzname() or ''} ({t.astimezone(UTC).strftime('%H:%M')} UTC)"


def windows_of(p: BoardPost) -> list[tuple[datetime, datetime]]:
    out = []
    for w in p.windows or []:
        try:
            out.append((datetime.fromisoformat(w[0]), datetime.fromisoformat(w[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


# --------------------------------------------------------------------------- recipes (Blizzard profiles)

def _recipes(prof: CharacterProfile | None) -> list[dict]:
    """{name, prof, skill, ref, r} for every recipe on an imported profile (no sharing check)."""
    if prof is None:
        return []
    profs = (prof.groups or {}).get("professions")
    if not isinstance(profs, list):
        return []
    out = []
    for pi, p in enumerate(profs):
        if not isinstance(p, dict) or not p.get("name"):
            continue
        for ri, r in enumerate(p.get("recipes") or []):
            if isinstance(r, dict) and r.get("name"):
                out.append({"name": r["name"], "prof": p["name"], "skill": p.get("skill"), "ref": f"{pi}-{ri}", "r": r})
    return out


def known_recipe(s: Session, character_id: int | None, name: str) -> dict | None:
    """The recipe on this character's Blizzard profile with this exact name (any case), or None."""
    if not character_id or not name:
        return None
    key = name.strip().casefold()
    for x in _recipes(s.get(CharacterProfile, character_id)):
        if x["name"].casefold() == key:
            return x
    return None


def skill_text(s: Session, character_id: int | None, profession: str | None) -> str:
    """'Leatherworking 165' from the character's Blizzard profile, or just the profession."""
    if not profession:
        return ""
    prof = s.get(CharacterProfile, character_id) if character_id else None
    for p in ((prof.groups or {}).get("professions") or []) if prof else []:
        if isinstance(p, dict) and str(p.get("name", "")).casefold() == profession.casefold() and p.get("skill"):
            return f"{p['name']} {p['skill']}"
    return profession


def members_crafters(s: Session, recipe: str) -> int:
    """Characters whose Professions are shared with all members and who know this recipe (the Discord
    card's count: the channel is for every member, so guild-only sharing doesn't count there)."""
    from .profiles import effective_share
    key = (recipe or "").strip().casefold()
    if not key:
        return 0
    q = (select(Character, CharacterProfile).join(CharacterProfile, CharacterProfile.character_id == Character.id)
         .where(Character.archived_at.is_(None)))
    return sum(1 for ch, prof in s.execute(q) if effective_share(prof, ch, "professions") == "members"
               and any(x["name"].casefold() == key for x in _recipes(prof)))


def viewer_pool(s: Session, cfg: Config, actor: Actor) -> dict[str, list]:
    """Recipe name (casefold) -> [(RosterRow, recipe)] the viewer may see: characters shared with all
    members, and with the viewer's guilds. The same rule as Who can craft."""
    from . import profiles
    rows = {r.ch.id: r for r in profiles.shared_with_members(s, cfg, actor)}
    for e, rg, g in profiles.viewer_guilds(s, actor.member_id):
        try:
            for r in profiles.guild_roster(s, cfg, actor, e, rg, g)[1]:
                rows.setdefault(r.ch.id, r)
        except NotFound:
            continue
    pool: dict[str, list] = {}
    for row in rows.values():
        for x in row.recipes:
            pool.setdefault(x["name"].casefold(), []).append((row, x))
    return pool


def my_recipes(s: Session, member_id: int) -> dict[str, list[tuple[Character, dict]]]:
    """Recipe name (casefold) -> [(own character, recipe)], from the member's own imported profiles."""
    out: dict[str, list] = {}
    q = (select(Character, CharacterProfile).join(CharacterProfile, CharacterProfile.character_id == Character.id)
         .where(Character.member_id == member_id, Character.archived_at.is_(None)).order_by(Character.name))
    for ch, prof in s.execute(q):
        for x in _recipes(prof):
            out.setdefault(x["name"].casefold(), []).append((ch, x))
    return out


# --------------------------------------------------------------------------- Discord card upkeep

def card_extra(s: Session, p: BoardPost) -> dict:
    """Facts the worker can't read itself (it never sees profile data): crafter count and skill line."""
    extra = {}
    if p.kind == "need_crafted":
        extra["crafters"] = members_crafters(s, p.recipe_name or p.title)
    if p.kind in ("can_craft", "recipe", "farm") and p.profession:
        extra["skill"] = skill_text(s, p.character_id, p.profession)
    return extra


def request_render(s: Session, p: BoardPost) -> None:
    s.flush()
    outbox.enqueue(s, "discord.render_board_post", str(p.id), {"post_id": p.id, **card_extra(s, p)})


def _delete_card(s: Session, p: BoardPost) -> None:
    if p.discord_channel_id and p.discord_message_id:
        outbox.enqueue(s, "discord.delete_board_card", f"{p.discord_channel_id}:{p.discord_message_id}",
                       {"channel_id": p.discord_channel_id, "message_id": p.discord_message_id})


# --------------------------------------------------------------------------- writing

def _own_character(s: Session, actor: Actor, character_id: int) -> Character:
    ch = s.get(Character, int(character_id or 0))
    if ch is None or ch.member_id != actor.member_id or ch.archived_at is not None:
        raise Invalid("Pick one of your own characters.")
    return ch


def open_count(s: Session, member_id: int, board: str) -> int:
    return s.scalar(select(func.count()).select_from(BoardPost).where(
        BoardPost.member_id == member_id, BoardPost.board == board, BoardPost.state == "open")) or 0


def _text(v, lo: int, hi: int, what: str) -> str:
    t = " ".join(str(v or "").split())
    if not lo <= len(t) <= hi:
        raise Invalid(f"{what} must be {lo} to {hi} characters." if lo else f"{what} can be up to {hi} characters.")
    return t


def create(s: Session, cfg: Config, actor: Actor, board: str, data: dict) -> BoardPost:
    """data: kind, character_id, title, body, and per board: activity_type, tags, windows (list of
    (start, end) UTC datetimes), group_size, timezone; or profession, recipe_name."""
    require_member(cfg, actor)
    _require_on(cfg, board)
    actor = ensure_member(s, actor)
    sec = section(cfg, board)
    kind = str(data.get("kind") or "")
    if kind not in KINDS[board]:
        raise Invalid("Pick what kind of post this is.")
    ch = _own_character(s, actor, data.get("character_id"))
    if open_count(s, actor.member_id, board) >= sec["max_open_per_member"]:
        raise Invalid(f"You have {sec['max_open_per_member']} open posts on the {BOARD_LABEL[board]}. "
                      "Mark one done or remove it first.")
    body = " ".join(str(data.get("body") or "").split())[:600]
    if len(str(data.get("body") or "").strip()) > 500:
        raise Invalid("The details can be up to 500 characters.")
    tz = str(data.get("timezone") or cfg["community"]["default_timezone"])
    p = BoardPost(board=board, kind=kind, member_id=actor.member_id, character_id=ch.id, edition=ch.edition,
                  body=body, timezone=tz, tags=[], windows=[])
    t = now()
    if board == "adventure":
        p.title = _text(data.get("title"), 3, 120, "The goal")
        from .events import ACTIVITY_TYPES
        act = str(data.get("activity_type") or "quest")
        if act not in ACTIVITY_TYPES:
            raise Invalid("Pick an activity.")
        p.activity_type = act
        allowed = {x.casefold(): x for x in sec["tags"]}
        p.tags = [allowed[x.casefold()] for x in dict.fromkeys(data.get("tags") or []) if x.casefold() in allowed][:6]
        gs = data.get("group_size") or sec["group_size"]
        try:
            gs = int(gs)
        except (TypeError, ValueError):
            raise Invalid("Group size must be a number.") from None
        if not 2 <= gs <= 40:
            raise Invalid("Group size must be from 2 to 40.")
        p.group_size = gs
        wins = [(a, b) for a, b in (data.get("windows") or []) if a and b]
        if len(wins) > MAX_WINDOWS:
            raise Invalid(f"Give up to {MAX_WINDOWS} times that work.")
        limit = t + timedelta(days=sec["open_days"])
        most = int(sec.get("max_window_hours", MAX_WINDOW_HOURS))
        for a, b in wins:
            if b <= a:
                raise Invalid("Each time that works needs to end after it starts.")
            if b - a > timedelta(hours=most):
                hrs = (b - a).total_seconds() / 3600
                raise Invalid(f"{when_text(a, b, tz)} is {hrs:g} hours long; each time that works "
                              f"can be up to {most} hours. An end earlier than the start means the next day.")
            if b <= t:
                raise Invalid("That time has already passed.")
            if a > limit:
                raise Invalid(f"Times that work must be within the next {sec['open_days']} days.")
        wins.sort()
        p.windows = [[a.astimezone(UTC).isoformat(), b.astimezone(UTC).isoformat()] for a, b in wins]
        p.expires_at = min(max(b for _, b in wins), limit) if wins else limit
    else:
        names = {x.casefold(): x for x in sec["names"]}
        prof = str(data.get("profession") or "").strip()
        if kind == "can_craft":
            got = known_recipe(s, ch.id, str(data.get("recipe_name") or data.get("title") or ""))
            if got is None:
                raise Invalid(f"{ch.name}'s Blizzard profile doesn't show that recipe. Pick it from the list, or "
                              "press Refresh from Blizzard on the character page if they learned it recently.")
            p.title = p.recipe_name = got["name"][:120]
            p.profession = got["prof"][:40]
        else:
            if prof and prof.casefold() not in names:
                raise Invalid("Pick a profession from the list.")
            p.profession = names.get(prof.casefold()) if prof else None
            if kind in ("recipe", "farm") and not p.profession:
                raise Invalid("Pick the profession.")
            p.title = _text(data.get("title") or data.get("recipe_name"), 3, 120,
                            "The item" if kind == "need_crafted" else "The title")
            if kind == "need_crafted":
                p.recipe_name = p.title
        p.expires_at = t + timedelta(days=sec["post_days"])
    s.add(p)
    s.flush()
    audit.record(s, actor, "board.post", "board_post", p.id, board=board, kind=kind)
    if sec["channel_id"]:
        p.discord_channel_id = sec["channel_id"]
        request_render(s, p)
    return p


def _response_kind(p: BoardPost, kind: str) -> str:
    if kind not in RESPONSES.get(p.kind, {}):
        raise Invalid("That isn't an option on this post.")
    return kind


def respond(s: Session, cfg: Config, actor: Actor, public_id: str, kind: str, want: bool = True,
            note: str = "", character_id: int | None = None) -> BoardResponse | None:
    """Press a button on someone's post (want=False takes it back). can_make picks the member's character
    whose Blizzard profile knows the recipe (character_id narrows it); answer needs a note."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    p = get(s, public_id, lock=True)
    _require_on(cfg, p.board)
    kind = _response_kind(p, kind)
    if p.member_id == actor.member_id:
        raise Invalid("That's your own post.")
    key = (p.id, actor.member_id, kind)
    had = s.get(BoardResponse, key)
    if not want:
        if had is not None:
            s.delete(had)
            audit.record(s, actor, "board.withdraw", "board_post", p.id, kind=kind)
            if p.discord_channel_id:
                request_render(s, p)
        return None
    if p.state != "open":
        raise InvalidTransition("This post is closed.")
    cid, skill = None, ""
    if kind == "can_make":
        mine = my_recipes(s, actor.member_id).get((p.recipe_name or p.title).casefold(), [])
        if character_id:
            mine = [(c, x) for c, x in mine if c.id == int(character_id)]
        if not mine:
            raise Invalid(f"None of your characters' Blizzard profiles shows the recipe for {p.recipe_name or p.title}. "
                          "If one learned it recently, press Refresh from Blizzard on its character page.")
        cid = mine[0][0].id
        skill = skill_text(s, cid, mine[0][1]["prof"])
    elif character_id:
        cid = _own_character(s, actor, character_id).id
    text = ""
    if kind == "answer":
        text = _text(note, 3, 400, "Your answer")
    elif note:
        text = _text(note, 0, 400, "Your note")
    if had is not None:
        had.note, had.character_id = text or had.note, cid or had.character_id
        r = had
    else:
        r = BoardResponse(post_id=p.id, member_id=actor.member_id, kind=kind, character_id=cid, note=text)
        s.add(r)
    s.flush()
    audit.record(s, actor, "board.respond", "board_post", p.id, kind=kind)
    if had is None and kind in NOTIFY and section(cfg, p.board)["notify_poster"]:
        outbox.enqueue(s, "notify.board_response", f"{p.id}:{actor.member_id}:{kind}",
                       {"post_id": p.id, "member_id": actor.member_id, "kind": kind, "skill": skill})
    if p.discord_channel_id:
        request_render(s, p)
    return r


def _mine_or_mod(cfg: Config, actor: Actor, p: BoardPost, mod_ok: bool) -> bool:
    if p.member_id == actor.member_id:
        return False
    if mod_ok and has(cfg, actor, "event_moderator"):
        return True
    raise PermissionDenied("Only the person who posted it can do that.")


def close(s: Session, cfg: Config, actor: Actor, public_id: str, how: str) -> BoardPost:
    """how: done (the poster) or removed (the poster, or an event moderator)."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    if how not in ("done", "removed"):
        raise Invalid("Unknown action.")
    p = get(s, public_id, lock=True)
    as_mod = _mine_or_mod(cfg, actor, p, mod_ok=how == "removed")
    if how == "done" and p.state != "open":
        raise InvalidTransition("This post is already closed.")
    p.state = how if how == "removed" else "done"
    p.closed_reason = how
    p.updated_at = now()
    audit.record(s, actor, "board.remove" if how == "removed" else "board.done", "board_post", p.id,
                 moderator=as_mod)
    if how == "removed":
        _delete_card(s, p)            # a removed post's card goes away; others stay, marked closed
    elif p.discord_channel_id:
        request_render(s, p)
    return p


def extend(s: Session, cfg: Config, actor: Actor, public_id: str) -> BoardPost:
    """Keep an open post up for another open_days / post_days from now."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    p = get(s, public_id, lock=True)
    _mine_or_mod(cfg, actor, p, mod_ok=False)
    if p.state != "open":
        raise InvalidTransition("This post is closed.")
    sec = section(cfg, p.board)
    p.expires_at = max(p.expires_at, now() + timedelta(days=sec["open_days" if p.board == "adventure" else "post_days"]))
    p.updated_at = now()
    audit.record(s, actor, "board.extend", "board_post", p.id)
    if p.discord_channel_id:
        request_render(s, p)
    return p


def plan_event(s: Session, cfg: Config, actor: Actor, public_id: str, start_at: datetime, end_at: datetime,
               tz: str) -> Event:
    """The poster turns their post into a draft event (Plan an event's edit page opens next). When it's
    published, everyone who answered the post is sent the link (see on_publish)."""
    require_member(cfg, actor)
    actor = ensure_member(s, actor)
    p = get(s, public_id, lock=True)
    _mine_or_mod(cfg, actor, p, mod_ok=False)
    if p.kind not in PLANNABLE:
        raise Invalid("This kind of post can't become an event.")
    if p.state != "open":
        raise InvalidTransition("This post is closed.")
    if p.event_id:
        ev = s.get(Event, p.event_id)
        if ev is not None and ev.state == "draft":
            return ev                      # already planned: keep working on the same draft
    from . import events
    ch = s.get(Character, p.character_id) if p.character_id else None
    if ch is not None and (ch.archived_at is not None or ch.member_id != actor.member_id):
        ch = None
    where = f"Planned from the {BOARD_LABEL[p.board]} post “{p.title}”."
    data = dict(edition=p.edition, title=p.title, activity_type=p.activity_type or "custom",
                description=((p.body + "\n\n") if p.body else "") + where,
                start_at=start_at, end_at=end_at, timezone=tz, public_listing=True)
    if ch is not None:
        data.update(region=ch.region, ruleset=ch.ruleset, realm_slug=ch.realm_slug, faction=ch.faction or "any")
    if p.group_size:
        data["participant_limit"] = p.group_size
    ev = events.create_draft(s, cfg, actor, data)
    if ch is not None:
        ev.host_character_id = ch.id
    p.event_id = ev.id
    p.updated_at = now()
    audit.record(s, actor, "board.plan", "event", ev.id, post=p.id)
    return ev


def on_publish(s: Session, ev: Event) -> None:
    """events.publish: a draft planned from a board post went live. The post closes as "Became an event"
    and everyone who answered it is sent the event link."""
    p = s.scalar(select(BoardPost).where(BoardPost.event_id == ev.id, BoardPost.state == "open"))
    if p is None:
        return
    p.state, p.closed_reason, p.updated_at = "closed", "evented", now()
    outbox.enqueue(s, "notify.board_event", f"{p.id}:{ev.id}", {"post_id": p.id, "event_id": ev.id})
    if p.discord_channel_id:
        request_render(s, p)


def recheck_offers(s: Session, character_id: int, gone: bool = False) -> int:
    """Can craft offers from this character close when its Blizzard profile no longer shows the recipe
    (refresh, removed details, archived or deleted character). Runs in the same transaction."""
    n = 0
    for p in s.scalars(select(BoardPost).where(BoardPost.character_id == character_id, BoardPost.state == "open")):
        if gone:
            if p.kind == "can_craft":
                p.state, p.closed_reason = "closed", "character"
            else:
                continue
        elif p.kind != "can_craft" or known_recipe(s, character_id, p.recipe_name or p.title) is not None:
            continue
        else:
            p.state, p.closed_reason = "closed", "recipe_gone"
        p.updated_at = now()
        n += 1
        if p.discord_channel_id:
            request_render(s, p)
    return n


def erase_member(s: Session, member_id: int) -> int:
    """Remove me: the member's posts (and their Discord cards) and every answer they gave."""
    answered = set(s.scalars(select(BoardResponse.post_id).where(BoardResponse.member_id == member_id)))
    s.execute(delete(BoardResponse).where(BoardResponse.member_id == member_id))
    mine = list(s.scalars(select(BoardPost).where(BoardPost.member_id == member_id)))
    for p in mine:
        _delete_card(s, p)
        s.delete(p)
    s.flush()
    for pid in answered - {p.id for p in mine}:
        p = s.get(BoardPost, pid)
        if p is not None and p.discord_channel_id:
            request_render(s, p)
    return len(mine)


# --------------------------------------------------------------------------- reading

@dataclass
class PostView:
    p: BoardPost
    kind_label: str
    character: Character | None
    who: str                                  # "Fixture Brannoc · Orc Hunter 19 · Forever"
    windows: list[str]
    counts: dict[str, int]
    count_text: list[str]
    named: dict[str, list[str]]               # response kind -> names (offers, or everything for the poster)
    answers: list[tuple[str, str]]            # (name, note) for recipe questions
    mine: set[str]                            # response kinds the viewer has pressed
    own: bool
    actions: dict[str, str]                   # response kind -> label, for the viewer
    crafters: int = 0                         # need_crafted: characters the viewer can see who know it
    icon: str = ""                            # member-area path of a recipe icon the viewer may load
    skill: str = ""
    event: Event | None = None
    closed_text: str = ""
    can_plan: bool = False
    ready: bool = False                       # enough people for the group size
    people: int = 0
    can_make_with: list[str] = field(default_factory=list)   # viewer's characters that know the recipe


def char_line(cfg: Config, ch: Character | None, extra: str = "") -> str:
    if ch is None:
        return "A removed character"
    bits = [ch.name, " ".join(x for x in (ch.race_key.replace("_", " ").title(), ch.class_key.replace("_", " ").title(),
                                         str(ch.level or "")) if x)]
    try:
        bits.append(extra or cfg.edition(ch.edition).name)
    except Exception:  # noqa: BLE001 - an edition removed from the config
        bits.append(extra or ch.edition)
    return " · ".join(b for b in bits if b)


def _name(s: Session, member_id: int, character_id: int | None) -> str:
    ch = s.get(Character, character_id) if character_id else None
    if ch is not None:
        return ch.name
    m = s.get(Member, member_id)
    return (m.display_name if m and not m.erased_at else "") or "A member"


def count_line(kind: str, rkind: str, n: int) -> str:
    one, many = COUNTS.get((kind, rkind), ("{n}", "{n}"))
    return (one if n == 1 else many).format(n=n)


def view(s: Session, cfg: Config, actor: Actor, p: BoardPost, pool: dict | None = None,
         mine_recipes: dict | None = None) -> PostView:
    own = p.member_id == actor.member_id
    ch = s.get(Character, p.character_id) if p.character_id else None
    resp = list(s.scalars(select(BoardResponse).where(BoardResponse.post_id == p.id)
                          .order_by(BoardResponse.created_at)))
    counts: dict[str, int] = {}
    named: dict[str, list[str]] = {}
    answers = []
    for r in resp:
        counts[r.kind] = counts.get(r.kind, 0) + 1
        if r.kind == "answer":
            answers.append((_name(s, r.member_id, r.character_id), r.note))
        elif own or r.kind in NAMED:
            named.setdefault(r.kind, []).append(_name(s, r.member_id, r.character_id))
    mine = {r.kind for r in resp if r.member_id == actor.member_id}
    skill = skill_text(s, p.character_id, p.profession) if p.profession else ""
    pv = PostView(p=p, kind_label=KINDS[p.board][p.kind], character=ch,
                  who=char_line(cfg, ch, skill if p.board == "profession" and skill else ""),
                  windows=[when_text(a, b, p.timezone) for a, b in windows_of(p)], counts=counts,
                  count_text=[count_line(p.kind, k, counts[k]) for k in RESPONSES.get(p.kind, {}) if counts.get(k)],
                  named=named, answers=answers, mine=mine, own=own,
                  actions={} if own or p.state != "open" else dict(RESPONSES.get(p.kind, {})), skill=skill)
    if p.event_id:
        pv.event = s.get(Event, p.event_id)
    if p.state != "open":
        pv.closed_text = CLOSED_TEXT.get(p.closed_reason, "Closed")
    key = (p.recipe_name or p.title).casefold()
    if p.kind == "need_crafted":
        if pool is None:
            pool = viewer_pool(s, cfg, actor)
        hits = pool.get(key, [])
        pv.crafters = len(hits)
        icon = next((h for h in hits if h[1]["r"].get("icon")), None)
        if icon:
            pv.icon = f"/characters/{icon[0].ch.id}/icon/recipes/{icon[1]['ref']}"
        if mine_recipes is None:
            mine_recipes = my_recipes(s, actor.member_id)
        pv.can_make_with = [c.name for c, _x in mine_recipes.get(key, [])]
    elif p.kind == "can_craft" and p.character_id:
        if own:
            got = known_recipe(s, p.character_id, p.recipe_name or p.title)
            pv.icon = f"/characters/{p.character_id}/icon/recipes/{got['ref']}" if got and got["r"].get("icon") else ""
        else:
            if pool is None:
                pool = viewer_pool(s, cfg, actor)
            hit = next((h for h in pool.get(key, []) if h[0].ch.id == p.character_id), None)
            pv.icon = f"/characters/{p.character_id}/icon/recipes/{hit[1]['ref']}" if hit and hit[1]["r"].get("icon") else ""
    if p.group_size:
        pv.people = 1 + counts.get("need", 0) + counts.get("help", 0)
        pv.ready = pv.people >= p.group_size
    pv.can_plan = own and p.state == "open" and p.kind in PLANNABLE and enabled(cfg, p.board)
    return pv


def listing(s: Session, cfg: Config, actor: Actor, board: str, f: dict | None = None) -> list[PostView]:
    """Open posts (and ones that became an event that's still to come), newest first, filtered:
    kind, activity / profession, edition, tag, fits (an edition and faction one of my characters has),
    can_help (profession: a recipe one of my characters knows)."""
    require_member(cfg, actor)
    _require_on(cfg, board)
    actor = ensure_member(s, actor)
    f = f or {}
    q = (select(BoardPost).outerjoin(Event, Event.id == BoardPost.event_id)
         .where(BoardPost.board == board,
                (BoardPost.state == "open") |
                ((BoardPost.closed_reason == "evented") & Event.state.in_(("scheduled", "in_progress"))))
         .order_by(BoardPost.created_at.desc()).limit(200))
    posts = list(s.scalars(q))
    if f.get("kind") in KINDS[board]:
        posts = [p for p in posts if p.kind == f["kind"]]
    if f.get("activity"):
        posts = [p for p in posts if p.activity_type == f["activity"]]
    if f.get("profession"):
        posts = [p for p in posts if (p.profession or "").casefold() == str(f["profession"]).casefold()]
    if f.get("edition"):
        posts = [p for p in posts if p.edition == f["edition"]]
    if f.get("tag"):
        posts = [p for p in posts if str(f["tag"]).casefold() in {t.casefold() for t in p.tags or []}]
    if f.get("fits"):
        chars = list(s.scalars(select(Character).where(Character.member_id == actor.member_id,
                                                       Character.archived_at.is_(None))))
        def fits(p):
            pc = s.get(Character, p.character_id) if p.character_id else None
            return any(c.edition == p.edition and (pc is None or c.faction == pc.faction) for c in chars)
        posts = [p for p in posts if p.member_id == actor.member_id or fits(p)]
    pool = viewer_pool(s, cfg, actor) if board == "profession" else {}
    mine = my_recipes(s, actor.member_id) if board == "profession" else {}
    if f.get("can_help") and board == "profession":
        posts = [p for p in posts if p.kind == "need_crafted" and (p.recipe_name or p.title).casefold() in mine
                 and p.member_id != actor.member_id]
    return [view(s, cfg, actor, p, pool, mine) for p in posts]


def my_posts(s: Session, actor: Actor, board: str) -> list[BoardPost]:
    return list(s.scalars(select(BoardPost).where(BoardPost.member_id == actor.member_id, BoardPost.board == board,
                                                  BoardPost.state == "open").order_by(BoardPost.expires_at)))


def could_help(s: Session, cfg: Config, actor: Actor) -> list[tuple[BoardPost, str, list[str]]]:
    """Open Need crafted posts for recipes the viewer's own characters know: (post, poster name, my chars)."""
    mine = my_recipes(s, actor.member_id)
    out = []
    for p in s.scalars(select(BoardPost).where(BoardPost.board == "profession", BoardPost.kind == "need_crafted",
                                               BoardPost.state == "open", BoardPost.member_id != actor.member_id)
                       .order_by(BoardPost.created_at.desc()).limit(50)):
        hits = mine.get((p.recipe_name or p.title).casefold())
        if hits:
            out.append((p, _name(s, p.member_id, p.character_id),
                        [f"{c.name}, {x['prof']}" for c, x in hits]))
    return out[:10]


def craftable_choices(s: Session, member_id: int) -> list[dict]:
    """The member's own known recipes, for the Can craft form: {character_id, character, name, prof}."""
    out = []
    for key, hits in my_recipes(s, member_id).items():
        for c, x in hits:
            out.append({"character_id": c.id, "character": c.name, "name": x["name"], "prof": x["prof"]})
    return sorted(out, key=lambda r: (r["character"].casefold(), r["name"].casefold()))


def responders(s: Session, p: BoardPost) -> list[int]:
    return sorted(set(s.scalars(select(BoardResponse.member_id).join(Member, Member.id == BoardResponse.member_id)
                                .where(BoardResponse.post_id == p.id, Member.erased_at.is_(None)))) - {p.member_id})


__all__ = ["BOARDS", "KINDS", "RESPONSES", "create", "respond", "close", "extend", "plan_event", "on_publish",
           "recheck_offers", "erase_member", "listing", "view", "my_posts", "could_help", "craftable_choices",
           "when_text", "at_text", "get", "enabled", "members_crafters", "card_extra", "responders"]
