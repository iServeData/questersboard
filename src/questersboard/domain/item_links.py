"""Item cards for names in [brackets] in chosen Discord channels ([item_links]).

The bot finds the names and queues one outbox job per message (names and message IDs only; the
message text is never stored). The worker looks each name up in Blizzard's item data and posts the
cards through a webhook, because Discord refuses bot posts in channels linked to in-game guild chat
and webhook posts there are not relayed to the game. Cards have no link: everything shown comes from
Blizzard.

UNVERIFIED live: Blizzard's item search by exact name for WoW: Forever (check with
`questersboard-admin blizzard probe-item forever --name "..."`), and whether webhook posts stay out of
in-game guild chat in every case.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from .profiles import _label, _linked_icon, _tooltip

# [Name] but not a Markdown link [text](url); no nested brackets or new lines.
BRACKETS = re.compile(r"\[([^\[\]\n]{2,80})\](?!\()")
QUALITY_COLORS = {"poor": 0x9D9D9D, "common": 0xFFFFFF, "uncommon": 0x1EFF00, "rare": 0x0070DD,
                  "epic": 0xA335EE, "legendary": 0xFF8000, "artifact": 0xE6CC80, "heirloom": 0x00CCFF}


def parse_names(text: str, limit: int) -> list[str]:
    """Distinct names in [brackets], in order, at most `limit`. Skips "[...]"-style asides and
    brackets without letters."""
    out, seen = [], set()
    for m in BRACKETS.finditer(text or ""):
        name = " ".join(m.group(1).split())
        if name.startswith(("...", "\u2026")) or not re.search(r"[^\W\d_]", name):
            continue
        k = name.casefold()
        if k not in seen:
            seen.add(k)
            out.append(name)
            if len(out) >= limit:
                break
    return out


def find_item_id(s: Session | None, bz, region: str, sns: str, hours: float, name: str, pages: int,
                 trace: list | None = None) -> int | None:
    """The item whose name is exactly `name` (any letter case), from Blizzard's item search. When
    several items share the name, the lowest ID (usually the original) wins; UNVERIFIED which one
    players expect. Reads at most `pages` pages of 100."""
    for n in range(1, pages + 1):
        try:
            st, body = bz.get(s, region, "/data/wow/search/item", sns, hours,
                              params={"name.en_US": name, "orderby": "id", "_pageSize": "100", "_page": str(n)})
        except Exception as exc:                # a failed lookup is just "not found" on Discord
            if trace is not None:
                trace.append({"page": n, "error": f"{type(exc).__name__}: {exc}"[:200]})
            return None
        if st != 200 or not isinstance(body, dict):
            if trace is not None:
                trace.append({"page": n, "error": f"HTTP {st}"})
            return None
        hits = [h.get("data") or {} for h in body.get("results") or [] if isinstance(h, dict)]
        exact = sorted(h["id"] for h in hits if isinstance(h.get("id"), int) and _label(h).casefold() == name.casefold())
        if trace is not None:
            trace.append({"page": n, "results": len(hits), "exact": exact, "page_count": body.get("pageCount")})
        if exact:
            return exact[0]
        if n >= int(body.get("pageCount") or 1):
            return None
    return None


def lookup(s: Session | None, bz, ed, region: str, hours: float, name: str, pages: int,
           trace: list | None = None) -> dict | None:
    """{id, name, quality, tip, icon} for the item named `name`, or None."""
    sns = ed.namespace("static", region)
    if not sns:
        return None
    iid = find_item_id(s, bz, region, sns, hours, name, pages, trace)
    if iid is None:
        return None
    try:
        st, item = bz.get(s, region, f"/data/wow/item/{iid}", sns, hours)
    except Exception:
        return None
    if st != 200 or not isinstance(item, dict):
        return None
    q = item.get("quality")
    out = {"id": iid, "name": _label(item)[:120] or name,
           "quality": ((q.get("type") or "") if isinstance(q, dict) else "").lower()[:16],
           "tip": _tooltip(item.get("preview_item") if isinstance(item.get("preview_item"), dict) else item)}
    try:
        icon = _linked_icon(s, bz, region, sns, item, f"/data/wow/media/item/{iid}")
    except Exception:
        icon = None
    if icon:
        out["icon"] = icon
    return out


def sources(cfg, first: str | None = None) -> list[tuple]:
    """(edition, region, label) to search in order: [item_links] edition (or `first`), then `fallbacks`."""
    il = cfg["item_links"]
    keys = [first or il["edition"]] + [k for k in il["fallbacks"] if k != (first or il["edition"])]
    out = []
    for k in keys:
        ed = cfg.edition(k)
        region = il["region"] if il["region"] in ed.regions else ed.default_region
        out.append((ed, region, il["source_labels"].get(k) or ed.name))
    return out


def find(s: Session | None, bz, cfg, hours: float, name: str, first: str | None = None,
         trace: list | None = None) -> dict | None:
    """The first edition in `sources` whose Blizzard data has an item of exactly this name. The card's
    footer names where it came from, and says so when it isn't the main edition."""
    il = cfg["item_links"]
    chain = sources(cfg, first)
    main = chain[0][2]
    for i, (ed, region, label) in enumerate(chain):
        t: list = []
        got = lookup(s, bz, ed, region, hours, name, il["search_pages"], t)
        if trace is not None:
            trace.append({"edition": ed.key, "region": region, "namespace": ed.namespace("static", region),
                          "pages": t, "found": got["id"] if got else None})
        if got:
            tpl = il["footer_text"] if i == 0 else il["footer_fallback_text"]
            got["footer"] = tpl.replace("{source}", label).replace("{main}", main)
            return got
    return None


def _coins(sell: dict) -> str:
    parts = [f"{sell[k]}{k[0]}" for k in ("gold", "silver", "copper") if sell.get(k, "0") != "0"]
    return " ".join(parts)


def tooltip_lines(t: dict) -> list[str]:
    """The tooltip as plain lines, in the game's order (Discord can't colour lines inside a card)."""
    lines: list[str] = []
    add = lines.append
    for k in ("binding", "unique"):
        if t.get(k):
            add(t[k])
    if t.get("type") or t.get("subtype"):
        add(" · ".join(x for x in (t.get("type"), t.get("subtype")) if x))
    if t.get("damage") or t.get("speed"):
        add(" · ".join(x for x in (t.get("damage"), t.get("speed")) if x))
    for k in ("dps", "armor"):
        if t.get(k):
            add(t[k])
    for st in t.get("stats") or []:
        add(st["text"])
    if t.get("durability"):
        add(t["durability"])
    lines.extend(t.get("requirements") or [])
    if t.get("level"):
        add(t["level"])
    lines.extend(t.get("spells") or [])
    if t.get("set"):
        add(t["set"])
    if t.get("description"):
        add(f"\u201c{t['description']}\u201d")
    if t.get("sell"):
        add("Sell Price: " + _coins(t["sell"]))
    return lines


def card(item: dict) -> dict:
    """One Discord embed: the item's name in its quality colour (no link), tooltip lines, icon, and the
    source in the footer (set by `find`)."""
    e = {"title": item["name"][:256], "color": QUALITY_COLORS.get(item.get("quality") or "", 0xFFFFFF),
         "description": "\n".join(tooltip_lines(item.get("tip") or {}))[:4000],
         "footer": {"text": (item.get("footer") or "Source: Blizzard")[:200]}}
    if item.get("icon"):
        e["thumbnail"] = {"url": item["icon"]}
    if not e["description"]:
        del e["description"]
    return e


NOT_FOUND = "No item called **{name}** found in Blizzard's public game data."


def message(items: list[dict], missing: list[str], jump_url: str, say_not_found: bool,
            not_found_text: str = NOT_FOUND) -> dict | None:
    """The webhook message for one player message, or None when there is nothing to post. `jump_url`
    (a link back to the player's message) is only given when the cards go to another channel."""
    missing = missing if say_not_found else []
    if not items and not missing:
        return None
    lines = ([f"Items from {jump_url}"] if jump_url else []) + \
        [not_found_text.replace("{name}", _md(n)) for n in missing]
    out = {"embeds": [card(i) for i in items][:10], "allowed_mentions": {"parse": []}}
    if lines:
        out["content"] = "\n".join(lines)[:2000]
    return out


def _md(name: str) -> str:
    """A name typed by a player, with Discord formatting characters escaped."""
    return re.sub(r"([\\*_~`|>])", r"\\\1", name)


def should_read(il: dict, channel_id: str, *, self_id: str, author_id: str, is_bot: bool,
                webhook_id: str | None, author_name: str) -> bool:
    """Whether the bot looks at this message for [item names]."""
    if not il.get("enabled") or channel_id not in il.get("watch_channels", []):
        return False
    if author_id == self_id:
        return False
    if webhook_id and author_name == il.get("webhook_name"):
        return False                            # our own cards
    if is_bot and not webhook_id and il.get("ignore_bots"):
        return False
    return True


class Cooldown:
    """The same name in the same channel gets one card per `minutes` (memory only; resets on restart)."""

    def __init__(self, clock=None):
        import time
        self._clock = clock or time.monotonic
        self._seen: dict[tuple[str, str], float] = {}

    def allow(self, channel_id: str, names: list[str], minutes: float) -> list[str]:
        t = self._clock()
        if len(self._seen) > 5000:
            self._seen = {k: v for k, v in self._seen.items() if v > t}
        out = []
        for n in names:
            k = (channel_id, n.casefold())
            if self._seen.get(k, 0) <= t:
                out.append(n)
                if minutes > 0:
                    self._seen[k] = t + minutes * 60
        return out


def enqueue(s: Session, channel_id: str, message_id: str, names: list[str]) -> None:
    """One job per message: names and IDs only, never the message text or who wrote it."""
    from . import outbox
    outbox.enqueue(s, "item_links.post", f"{channel_id}:{message_id}",
                   {"channel_id": channel_id, "message_id": message_id, "names": names})


def enqueue_delete(s: Session, channel_id: str, message_id: str) -> bool:
    """Queue removal of a deleted message's cards, if we posted (or were about to post) any."""
    from sqlalchemy import text
    hit = s.execute(text("SELECT 1 FROM outbox WHERE kind = 'item_links.post' AND key = :k LIMIT 1"),
                    {"k": f"{channel_id}:{message_id}"}).first()
    if hit is None:
        return False
    from . import outbox
    outbox.enqueue(s, "item_links.delete", f"{channel_id}:{message_id}",
                   {"channel_id": channel_id, "message_id": message_id})
    return True
