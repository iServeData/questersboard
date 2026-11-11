"""Character details imported from Blizzard's character pages, chosen and shared per group.

The member ticks which groups to import (all ticked by default) and who may see each group:
private (only them), guild (QuestersBoard members confirmed in the same in-game guild) or members
(everyone signed in to QuestersBoard). Sharing beyond private only counts while the character is
verified as theirs through Battle.net; otherwise a group is treated as private however it was set.

Only a curated summary of each page is stored, never the raw answer. Blizzard's API terms limit how
long their data is kept: the worker deletes profiles older than [blizzard] data_ttl_days, and the
member can refresh or remove them at any time.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import Config
from ..models import SHARE_LEVELS, Character, CharacterProfile
from . import audit
from .actor import Actor
from .errors import Invalid, NotFound
from .permissions import require_member
from .timeutil import now


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    hint: str
    pages: tuple[str, ...]          # path suffixes after the character's profile path ("" = summary)


GROUPS: tuple[Group, ...] = (
    Group("overview", "Overview", "level, item level, achievement points", ("",)),
    Group("guild", "Guild", "guild name; needed for a guild page", ("",)),
    Group("gear", "Gear", "equipped items and item levels", ("/equipment",)),
    Group("talents", "Talents", "specialization and talent points", ("/specializations",)),
    Group("professions", "Professions", "professions, skill levels and known recipes", ("/professions",)),
    Group("stats", "Stats", "health, attributes, armor, crit, hit, resistances", ("/statistics",)),
    Group("portrait", "Portrait", "Blizzard's character images", ("/character-media",)),
    Group("pvp", "PvP", "honorable kills and rank", ("/pvp-summary",)),
    Group("progress", "Progress", "reputations, quests, achievements, mounts",
          ("/reputations", "/quests/completed", "/achievements", "/collections/mounts")),
    Group("pets", "Pets", "hunter pets: name, family and level (hunters only)", ("/hunter-pets",)),
)
GROUP_KEYS = tuple(g.key for g in GROUPS)
# Pill tabs over a character's details (Armory-style). Each group sits in exactly one tab; tabs with
# nothing to show are left out, and without JavaScript every section simply shows in order.
DETAIL_TABS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("character", "Character", ("overview", "guild", "gear", "stats", "portrait")),
    ("talents", "Talents", ("talents",)),
    ("professions", "Professions", ("professions",)),
    ("progress", "Progress", ("pvp", "progress")),
    ("pets", "Pets", ("pets",)),
)
TAB_OF = {k: t for t, _label, keys in DETAIL_TABS for k in keys}
SHARE_LABELS = {"private": "Only me", "guild": "My guild", "members": "All QuestersBoard members"}


def _label(v) -> str:
    if isinstance(v, dict):
        v = v.get("name") if v.get("name") is not None else v.get("type")
        if isinstance(v, dict):
            v = v.get("en_US") or next(iter(v.values()), "")
    return str(v or "").strip()


def _num(v):
    """Blizzard stats are numbers or {base, effective} / {rating, value}; keep one number."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(v, 2) if isinstance(v, float) else v
    if isinstance(v, dict):
        for k in ("effective", "value", "rating_normalized", "rating"):
            if isinstance(v.get(k), (int, float)):
                return _num(v[k])
    return None


# ------------------------------------------------------------------ extractors (page answers -> stored data)
def _overview(summary: dict) -> dict:
    return {k: summary.get(k) for k in ("level", "average_item_level", "equipped_item_level", "achievement_points")
            if isinstance(summary.get(k), int)}


def _guild(summary: dict) -> dict | None:
    g = summary.get("guild")
    if not isinstance(g, dict) or not _label(g):
        return None
    return {"id": str(g.get("id")) if g.get("id") is not None else None, "name": _label(g)[:96]}


def _text(v, n: int = 200) -> str:
    """A display string from Blizzard (plain text, or {display_string}), trimmed for storage."""
    if isinstance(v, dict):
        v = v.get("display_string")
    return v.strip()[:n] if isinstance(v, str) else ""


def _media_ref(item: dict) -> dict | None:
    """Where Blizzard says an item's icon is: the path and namespace of its media link. Forever's item
    media lives in a versioned static namespace, so the link is followed rather than rebuilt."""
    from urllib.parse import parse_qsl, urlsplit
    href = (((item.get("media") or {}).get("key") or {}).get("href") or "") if isinstance(item.get("media"), dict) else ""
    u = urlsplit(href)
    if not u.path.startswith("/data/wow/media/"):
        return None
    ns = dict(parse_qsl(u.query)).get("namespace") or ""
    return {"path": u.path[:120], "ns": ns[:64]} if ns else None


def _tooltip(i: dict) -> dict:
    """The lines of an item tooltip, from the equipment page (only the parts Blizzard sent)."""
    t: dict = {}
    if _label(i.get("binding")):
        t["binding"] = _label(i["binding"])[:60]
    if i.get("unique_equipped"):
        t["unique"] = _text(i["unique_equipped"], 60) or "Unique-Equipped"
    if _label(i.get("inventory_type")):
        t["type"] = _label(i["inventory_type"])[:40]
    if _label(i.get("item_subclass")) and not i.get("is_subclass_hidden"):
        t["subtype"] = _label(i["item_subclass"])[:40]
    w = i.get("weapon") if isinstance(i.get("weapon"), dict) else {}
    for k, src in (("damage", w.get("damage")), ("speed", w.get("attack_speed")), ("dps", w.get("dps"))):
        if _text(src):
            t[k] = _text(src, 80)
    if _text((i.get("armor") or {}).get("display") if isinstance(i.get("armor"), dict) else None):
        t["armor"] = _text(i["armor"]["display"], 60)
    stats = []
    for st in i.get("stats") or []:
        if isinstance(st, dict) and _text(st.get("display")):
            stats.append({"text": _text(st["display"], 80), "green": bool(st.get("is_equip_bonus"))})
    if stats:
        t["stats"] = stats[:12]
    spells = [_text(sp.get("description"), 300) for sp in i.get("spells") or [] if isinstance(sp, dict)]
    if any(spells):
        t["spells"] = [x for x in spells if x][:4]
    if _text(i.get("durability")):
        t["durability"] = _text(i["durability"], 60)
    req = i.get("requirements") if isinstance(i.get("requirements"), dict) else {}
    reqs = [_text(req.get(k)) for k in ("level", "playable_classes", "skill", "faction", "reputation")
            if _text(req.get(k))]
    if reqs:
        t["requirements"] = reqs[:4]
    if _text(i.get("level")):
        t["level"] = _text(i["level"], 40)
    if isinstance(i.get("set"), dict) and _text(i["set"]):
        t["set"] = _text(i["set"], 80)
    if _text(i.get("description"), 300):
        t["description"] = _text(i["description"], 300)
    ds = ((i.get("sell_price") or {}).get("display_strings") or {}) if isinstance(i.get("sell_price"), dict) else {}
    coins = {k: str(ds.get(k) or "0") for k in ("gold", "silver", "copper") if str(ds.get(k) or "").isdigit()}
    if coins and any(v != "0" for v in coins.values()):
        t["sell"] = coins
    tm = i.get("transmog") if isinstance(i.get("transmog"), dict) else {}
    if _label(tm.get("item")):
        t["transmog"] = _label(tm["item"])[:80]
    return t


def _gear(body: dict) -> list[dict]:
    out = []
    for i in body.get("equipped_items") or []:
        if not isinstance(i, dict):
            continue
        row = {"slot": _label(i.get("slot")), "name": _label(i),
               "item_level": (i.get("level") or {}).get("value"),
               "quality": ((i.get("quality") or {}).get("type") or "").lower(),
               "item_id": (i.get("item") or {}).get("id"),
               "enchants": [e.get("display_string") for e in i.get("enchantments") or []
                            if isinstance(e, dict) and e.get("display_string")][:4]}
        slot_type = ((i.get("slot") or {}).get("type") or "") if isinstance(i.get("slot"), dict) else ""
        if slot_type:
            row["slot_type"] = str(slot_type)[:24]
        tip = _tooltip(i)
        if tip:
            row["tip"] = tip
        ref = _media_ref(i)
        if ref:
            row["_media"] = ref                    # resolved to an icon URL at import, then dropped
        out.append(row)
    return out


def _talents(body: dict) -> dict:
    groups = []
    for g in body.get("specialization_groups") or []:
        specs = []
        for sp in g.get("specializations") or []:
            talents = []
            for t in sp.get("talents") or []:
                tip = t.get("spell_tooltip") if isinstance(t.get("spell_tooltip"), dict) else {}
                name = _label(t.get("talent")) or _label(tip.get("spell"))
                if not name:
                    continue
                row = {"name": name, "rank": t.get("talent_rank")}
                if isinstance((t.get("talent") or {}).get("id"), int):
                    row["talent_id"] = t["talent"]["id"]
                sid = (tip.get("spell") or {}).get("id") if isinstance(tip.get("spell"), dict) else None
                if isinstance(sid, int):
                    row["spell_id"] = sid
                for k in ("description", "cast_time", "power_cost", "range", "cooldown"):
                    if _text(tip.get(k), 600):
                        row[k] = _text(tip[k], 600)
                talents.append(row)
            specs.append({"name": sp.get("specialization_name") or _label(sp.get("specialization")),
                          "points": sp.get("spent_points"), "talents": talents})
        groups.append({"active": bool(g.get("is_active")), "specs": specs})
    out = {"groups": groups}
    if isinstance(body.get("active_specialization"), dict):
        out["active"] = _label(body["active_specialization"])
    return out


def _known_recipes(name: str, tiers: list[dict]) -> list[dict]:
    """The recipes a character knows in one profession: id and name, once each, sorted by name. The
    entry that is just the profession itself (Skinning, Fishing) is left out."""
    seen: dict[int, str] = {}
    for t in tiers:
        for r in t.get("known_recipes") or []:
            if isinstance(r, dict) and isinstance(r.get("id"), int) and isinstance(r.get("name"), str):
                label = r["name"].strip()[:120]
                if label and label.lower() != (name or "").lower():
                    seen.setdefault(r["id"], label)
    return [{"id": i, "name": n} for i, n in sorted(seen.items(), key=lambda x: (x[1].lower(), x[0]))]


def _professions(body: dict) -> list[dict]:
    out = []
    for kind in ("primaries", "secondaries"):
        for p in body.get(kind) or []:
            tiers = [t for t in (p.get("tiers") or []) if isinstance(t, dict)]
            if tiers:
                best = max(tiers, key=lambda t: t.get("skill_points") or 0)
                skill, top = best.get("skill_points"), best.get("max_skill_points")
            else:
                skill, top = p.get("skill_points"), p.get("max_skill_points")
            name = _label(p.get("profession"))
            row = {"name": name, "kind": "primary" if kind == "primaries" else "secondary", "skill": skill, "max": top}
            recipes = _known_recipes(name, tiers)
            if recipes:
                row["recipes"] = recipes
            out.append(row)
    return out


_STATS = ("health", "power", "strength", "agility", "intellect", "stamina", "spirit", "armor", "attack_power",
          "ranged_attack_power", "spell_power", "spell_healing", "melee_crit", "ranged_crit", "spell_crit",
          "melee_hit", "ranged_hit", "spell_hit", "dodge", "parry", "block", "defense", "expertise", "mana_regen",
          "fire_resistance", "frost_resistance", "nature_resistance", "shadow_resistance", "arcane_resistance")


def _stats(body: dict) -> dict:
    out = {k: _num(body.get(k)) for k in _STATS if _num(body.get(k)) is not None}
    if isinstance(body.get("power_type"), dict):
        out["power_type"] = _label(body["power_type"])
    return out


def _portrait(body: dict) -> list[dict]:
    return [{"key": a.get("key"), "url": a["value"]} for a in body.get("assets") or []
            if isinstance(a, dict) and str(a.get("value", "")).startswith("https://")][:6]


def _pvp(body: dict) -> dict:
    return {k: body.get(k) for k in ("honorable_kills", "pvp_rank") if isinstance(body.get(k), int)}


def _progress(pages: dict[str, dict | None]) -> dict:
    out: dict = {}
    rep = pages.get("/reputations")
    if rep is not None:
        out["reputations"] = [{"faction": _label(r.get("faction")), "standing": _label(r.get("standing")),
                               "value": (r.get("standing") or {}).get("value"),
                               "max": (r.get("standing") or {}).get("max")}
                              for r in rep.get("reputations") or [] if isinstance(r, dict)]
    q = pages.get("/quests/completed")
    if q is not None:
        out["quests_completed"] = len(q.get("quests") or [])
    ach = pages.get("/achievements")
    if ach is not None:
        out["achievements"] = len(ach.get("achievements") or [])
        if isinstance(ach.get("total_points"), int):
            out["achievement_points"] = ach["total_points"]
    m = pages.get("/collections/mounts")
    if m is not None:
        out["mounts"] = len(m.get("mounts") or [])
    return out


def _pets(body) -> list | None:
    """Hunter pets (their own group, so they can be shared separately from Progress)."""
    pets = [{"name": p.get("name"), "level": p.get("level"), "family": pet_family(p),
             **({"_display": d} if isinstance(d := (p.get("creature_display") or {}).get("id"), int) else {})}
            for p in body.get("hunter_pets") or [] if isinstance(p, dict) and p.get("name")]
    return pets or None


def extract(group: str, pages: dict[str, dict | None]):
    """Stored data for one group from its page answers ({suffix: body or None when not served})."""
    summary = pages.get("") or {}
    if group == "overview":
        return _overview(summary)
    if group == "guild":
        return _guild(summary)
    if group == "progress":
        return _progress(pages) or None
    body = pages.get(next(g for g in GROUPS if g.key == group).pages[0])
    if body is None:
        return None
    return {"gear": _gear, "talents": _talents, "professions": _professions, "stats": _stats,
            "portrait": _portrait, "pvp": _pvp, "pets": _pets}[group](body)


# ------------------------------------------------------------------ member actions
def _own(s: Session, actor: Actor, character_id: int) -> Character:
    ch = s.get(Character, character_id)
    if ch is None or ch.member_id != actor.member_id or ch.archived_at is not None:
        raise NotFound("Character not found.")
    return ch


def parse_form(form) -> tuple[set[str], dict[str, str]]:
    """Ticked groups and the share level for each (form fields: groups=..., share_<group>=...)."""
    getlist = getattr(form, "getlist", None)
    picked = getlist("groups") if getlist else form.get("groups", [])
    groups = {g for g in (picked if isinstance(picked, list) else [picked]) if g in GROUP_KEYS}
    shares = {}
    every = form.get("share_all") or ""                # "Set all": one choice for every group
    for g in GROUP_KEYS:
        v = (every if every in SHARE_LEVELS else "") or form.get(f"share_{g}") or "private"
        shares[g] = v if v in SHARE_LEVELS else "private"
    return groups, shares


def import_profile(s: Session, cfg: Config, bz, actor: Actor, character_id: int, groups: set[str],
                   shares: dict[str, str]) -> CharacterProfile:
    """Fetch the ticked groups from Blizzard and store them. Unticked groups are dropped. Raises
    Invalid if Blizzard has no public profile for the character."""
    require_member(cfg, actor)
    ch = _own(s, actor, character_id)
    if not groups:
        raise Invalid("Tick at least one group to import, or use Remove imported details.")
    m = bz.apply_lookup(s, ch)                 # also refreshes level and the API match
    if not m.found:
        raise Invalid(m.detail)
    ed = cfg.edition(ch.edition)
    base, ns = bz.character_path(ed, ch), m.namespace
    hours = cfg["blizzard"].get("cache_hours_profile", 6)
    pages: dict[str, dict | None] = {}
    for g in GROUPS:
        if g.key not in groups:
            continue
        for suffix in g.pages:
            if suffix not in pages:
                st, body = bz.get(s, ch.region, base + suffix, ns, hours)
                pages[suffix] = body if st == 200 else None
    data = {g: extract(g, pages) for g in GROUP_KEYS if g in groups}
    if data.get("portrait"):
        from urllib.parse import urlsplit
        hosts = set(cfg["blizzard"].get("render_image_hosts", ["render.worldofwarcraft.com"]))
        data["portrait"] = [a for a in data["portrait"] if urlsplit(a["url"]).hostname in hosts]
    # An empty group is dropped, except Portrait: Blizzard makes pictures some time after a character
    # logs out, so a ticked Portrait with nothing yet is kept (empty) and asked for again on refresh.
    data = {g: v for g, v in data.items() if v not in (None, {}, [])} | ({"portrait": data.get("portrait") or []}
                                                                      if "portrait" in data else {})
    cap = cfg["blizzard"].get("known_recipes_max", 500)
    for p in data.get("professions") or []:
        if "recipes" in p:
            p["recipes"] = p["recipes"][:cap]
            if not p["recipes"]:
                del p["recipes"]
    if data.get("talents"):
        _resolve_trees(s, cfg, bz, ed, ch, data["talents"])
    _resolve_icons(s, cfg, bz, ed, ch.region, data)
    guild = data.get("guild") or {}
    prof = s.get(CharacterProfile, ch.id)
    if prof is None:
        prof = CharacterProfile(character_id=ch.id, member_id=ch.member_id)
        s.add(prof)
    prof.edition, prof.region = ch.edition, ch.region
    prof.guild_id, prof.guild_name = guild.get("id"), guild.get("name")
    prof.groups = data
    prof.shares = {g: shares.get(g, "private") for g in data}
    prof.fetched_at = now()
    audit.record(s, actor, "character.profile_import", "character", ch.id, groups=sorted(data))
    return prof


def _same_static(ns: str, sns: str | None) -> bool:
    """A linked namespace is used only if it is the edition's static namespace or a version-pinned copy
    of it (static-1.60.1_70399-forever-us for static-forever-us), never another edition's or a PTR."""
    import re
    if not sns or not ns:
        return False
    if ns == sns:
        return True
    rest = sns[len("static-"):] if sns.startswith("static-") else ""
    return bool(rest) and "ptr" not in ns and re.fullmatch(r"static-[0-9][0-9._]*-" + re.escape(rest), ns) is not None


def _resolve_icons(s: Session, cfg: Config, bz, ed, region: str, data: dict) -> None:
    """Gear and talent icons: Blizzard's media answers give each icon's URL on its image server. Only
    the URL is stored; the image is fetched by QuestersBoard when the page is viewed. [blizzard]
    detail_icons = false skips this; icon_lookups caps the lookups per import (cached like other
    static data). A lookup that fails just leaves that icon out."""
    bzc = cfg["blizzard"]
    gear = data.get("gear") or []
    refs = [(row, row.pop("_media", None)) for row in gear]
    pets = [(row, row.pop("_display", None)) for row in data.get("pets") or [] if isinstance(row, dict)]
    if not bzc.get("detail_icons", True):
        return
    sns = ed.namespace("static", region)
    jobs = [(row, ref["path"], ref["ns"]) for row, ref in refs if ref and _same_static(ref["ns"], sns)]
    # Hunter pets: the creature display's picture ("zoom" render), when Blizzard lists one.
    jobs += [(row, f"/data/wow/media/creature-display/{did}", sns) for row, did in pets if did and sns]
    for grp in (data.get("talents") or {}).get("groups") or []:
        for sp in grp.get("specs") or []:
            for t in sp.get("talents") or []:
                if isinstance(t.get("spell_id"), int) and sns:
                    jobs.append((t, f"/data/wow/media/spell/{t['spell_id']}", sns))
    for tree in (data.get("talents") or {}).get("trees") or []:
        for node in tree.get("nodes") or []:
            if isinstance(node.get("spell_id"), int) and sns:
                jobs.append((node, f"/data/wow/media/spell/{node['spell_id']}", sns))
    budget = int(bzc.get("icon_lookups", 120))
    seen: dict[tuple[str, str], str | None] = {}
    for row, path, ns in jobs:
        key = (path, ns)
        if key not in seen:
            if budget <= 0:
                continue
            budget -= 1
            keys = ("zoom", "icon") if "/creature-display/" in path else ("icon",)
            seen[key] = bz.media_icon(s, region, path, ns, keys)
        if seen[key]:
            row["icon"] = seen[key]


def _static_page(s: Session, bz, region: str, ref, sns: str, hours: float) -> dict:
    """A linked talent-tree page (its href in Blizzard's answer), only within the edition's static data."""
    href = ((ref or {}).get("key") or {}).get("href") or "" if isinstance(ref, dict) else ""
    u = urlsplit(href)
    ns = dict(parse_qsl(u.query)).get("namespace") or sns
    if not u.path.startswith("/data/wow/talent-tree/") or not _same_static(ns, sns):
        return {}
    try:
        st, body = bz.get(s, region, u.path, ns, hours)
    except Exception:                          # a missing tree just keeps the plain talent list
        return {}
    return body if st == 200 and isinstance(body, dict) else {}


def _tip_fields(tip: dict) -> dict:
    out = {}
    for k in ("description", "cast_time", "power_cost", "range", "cooldown"):
        if _text(tip.get(k), 600):
            out[k] = _text(tip[k], 600)
    return out


def _regroup_by_block(pages: list) -> list:
    """[(ref, body, nodes)] per specialization page -> the same, with nodes regrouped by the block of
    class-grid columns they sit in (see _resolve_trees). Unchanged when blocks and pages don't match."""
    seen, owner = {}, {}
    for i, (_r, _b, raw) in enumerate(pages):
        for n in raw:
            key = n.get("id") if n.get("id") is not None else id(n)
            if key not in seen:
                seen[key], owner[key] = n, i
    used = sorted({n["display_col"] for n in seen.values()})
    blocks, run = [], []
    for c in used:
        if run and c != run[-1] + 1:
            blocks.append(run)
            run = []
        run.append(c)
    if run:
        blocks.append(run)
    if len(blocks) < len(pages):
        return pages
    block_of = {c: b for b, run in enumerate(blocks) for c in run}
    members: list[list] = [[] for _ in blocks]
    for key, n in seen.items():
        members[block_of[n["display_col"]]].append(n)
    # Each page's home is the block holding most of its own talents. Two pages with the same home:
    # keep Blizzard's grouping.
    homes = []
    for i, (_r, _b, raw) in enumerate(pages):
        count: dict = {}
        for n in raw:
            count[block_of[n["display_col"]]] = count.get(block_of[n["display_col"]], 0) + 1
        homes.append(max(count, key=lambda b: (count[b], -b)))
    if len(set(homes)) != len(homes):
        return pages
    out = [list(members[h]) for h in homes]
    # A block nobody calls home (a tree with an empty column inside it) stays with the page that sent
    # most of its talents.
    for b, ns in enumerate(members):
        if b in homes:
            continue
        votes: dict = {}
        for n in ns:
            k = owner[n.get("id") if n.get("id") is not None else id(n)]
            votes[k] = votes.get(k, 0) + 1
        out[max(votes, key=lambda i: (votes[i], -i))].extend(ns)
    return [(pages[i][0], pages[i][1], out[i]) for i in range(len(pages))]


def _resolve_trees(s: Session, cfg: Config, bz, ed, ch, talents: dict) -> None:
    """Classic-style talent trees: Blizzard's class tree lists its specialization trees, and each one
    places every talent on a grid (display_row, display_col) with its ranks; the class tree says which
    talent needs which. The character's ranks come from its own talents page. Stored as
    talents["trees"]; the plain list stays as it was. Off when the edition's talent_tree_columns is 0."""
    cols = getattr(ed, "talent_tree_columns", 0)
    sns = ed.namespace("static", ch.region)
    if not cols or not sns:
        return
    hours = cfg["blizzard"].get("cache_hours_static", 168)
    try:
        st, index = bz.get(s, ch.region, "/data/wow/talent-tree/index", sns, hours)
    except Exception:
        return
    if st != 200 or not isinstance(index, dict):
        return
    want = ch.class_key.replace("_", " ").lower()
    pick = next((t for t in index.get("class_talent_trees") or [] if isinstance(t, dict)
                 and _label(t).lower() == want), None)
    ctree = _static_page(s, bz, ch.region, pick, sns, hours)
    if not ctree:
        return
    needs = {n["id"]: [x for x in n.get("locked_by") or [] if isinstance(x, int)]
             for n in ctree.get("talent_nodes") or [] if isinstance(n, dict) and isinstance(n.get("id"), int)}
    active = next((g for g in talents.get("groups") or [] if g.get("active")), None) or \
        next(iter(talents.get("groups") or []), {})
    have_id, have_spell, have_name, points = {}, {}, {}, {}
    for sp in active.get("specs") or []:
        points[sp.get("name")] = sp.get("points")
        for t in sp.get("talents") or []:
            if t.get("talent_id"):
                have_id[t["talent_id"]] = t.get("rank") or 0
            if t.get("spell_id"):
                have_spell[t["spell_id"]] = t.get("rank") or 0
            have_name[t.get("name")] = t.get("rank") or 0
    # Columns are numbered across the whole class grid (one block of columns per specialization, with
    # empty columns between). Blizzard's per-specialization pages don't always hold exactly their own
    # block: WoW: Forever's hunter pages each carry the next tree's leftmost column. So every talent
    # is placed by the block its column falls in (runs of used columns split by empty ones), and each
    # block is named after the page that holds most of its talents. If the blocks don't line up one to
    # one with the pages, each page is drawn as Blizzard sent it.
    pages = []
    for ref in (ctree.get("spec_talent_trees") or [])[:4]:
        body = _static_page(s, bz, ch.region, ref, sns, hours)
        raw = [n for n in body.get("spec_talent_nodes") or [] if isinstance(n, dict)
               and isinstance(n.get("display_row"), int) and isinstance(n.get("display_col"), int)]
        if raw:
            pages.append((ref, body, raw))
    if not pages:
        return
    pages = _regroup_by_block(pages)
    cols = min(8, max([cols] + [max(n["display_col"] for n in raw) - min(n["display_col"] for n in raw) + 1
                                for _r, _b, raw in pages]))
    trees = []
    for ref, body, raw in pages:
        base = min(n["display_col"] for n in raw) - 1
        nodes, pos = [], {}
        for n in sorted(raw, key=lambda n: (n["display_row"], n["display_col"]))[:60]:
            ranks = [r for r in n.get("ranks") or [] if isinstance(r, dict)]
            tips = [r.get("tooltip") or {} for r in ranks]
            first = tips[0] if tips else {}
            tid = (first.get("talent") or {}).get("id")
            name = _label(first.get("talent")) or _label((first.get("spell_tooltip") or {}).get("spell"))
            if not name:
                continue
            spell = ((first.get("spell_tooltip") or {}).get("spell") or {}).get("id")
            # Match the character's rank by talent id, then by any rank's spell id, then by name.
            spells = [((t.get("spell_tooltip") or {}).get("spell") or {}).get("id") for t in tips]
            rank = have_id.get(tid) if tid in have_id else next(
                (have_spell[x] for x in spells if x in have_spell), have_name.get(name, 0))
            rank = max(0, min(int(rank or 0), len(ranks) or 1))
            cur = tips[rank - 1] if rank and rank <= len(tips) else first
            node = {"name": name, "row": n["display_row"], "col": n["display_col"] - base,
                    "rank": rank, "max": max(len(ranks), 1),
                    **_tip_fields(cur.get("spell_tooltip") or {})}
            if rank and rank < len(tips):
                nxt = _tip_fields(tips[rank].get("spell_tooltip") or {}).get("description")
                if nxt:
                    node["next"] = nxt
            if isinstance(spell, int):
                node["spell_id"] = spell
            if not 1 <= node["col"] <= cols:
                continue
            pos[n.get("id")] = len(nodes)
            node["_id"] = n.get("id")
            nodes.append(node)
        for node in nodes:
            req = [nodes[pos[x]]["name"] for x in needs.get(node.pop("_id"), []) if x in pos]
            if req:
                node["requires"] = req[:3]
        name = _label(body.get("playable_specialization")) or _label(body) or _label(ref)
        spent = points.get(name)
        trees.append({"name": name, "points": spent if isinstance(spent, int) else sum(n["rank"] for n in nodes),
                      "rows": max(n["row"] for n in nodes) if nodes else 0, "cols": cols, "nodes": nodes})
    if trees:
        # Size every tree to the columns its talents actually use (the same for all trees, so they
        # line up); a grid wider than its talents left them hugging the left of the frame.
        used = max([n["col"] for t in trees for n in t["nodes"]] or [1])
        for t in trees:
            t["cols"] = used
        talents["trees"] = trees


GEAR_LEFT = ("HEAD", "NECK", "SHOULDER", "BACK", "CHEST", "SHIRT", "TABARD", "WRIST")
GEAR_RIGHT = ("HANDS", "WAIST", "LEGS", "FEET", "FINGER_1", "FINGER_2", "TRINKET_1", "TRINKET_2")


GEAR_BOTTOM = ("MAIN_HAND", "OFF_HAND", "RANGED")
SLOT_LABELS = {"HEAD": "Head", "NECK": "Neck", "SHOULDER": "Shoulder", "BACK": "Back", "CHEST": "Chest",
               "SHIRT": "Shirt", "TABARD": "Tabard", "WRIST": "Wrist", "HANDS": "Hands", "WAIST": "Waist",
               "LEGS": "Legs", "FEET": "Feet", "FINGER_1": "Ring 1", "FINGER_2": "Ring 2",
               "TRINKET_1": "Trinket 1", "TRINKET_2": "Trinket 2", "MAIN_HAND": "Main Hand",
               "OFF_HAND": "Off Hand", "RANGED": "Ranged", "AMMO": "Ammo"}
# Items imported before slot types were kept only have Blizzard's English slot name.
_SLOT_BY_LABEL = {**{v.lower(): k for k, v in SLOT_LABELS.items()}, "shoulders": "SHOULDER",
                  "finger 1": "FINGER_1", "finger 2": "FINGER_2", "relic": "RANGED"}


def _slot_type(i: dict) -> str:
    st = i.get("slot_type")
    if isinstance(st, str) and st:
        return st
    return _SLOT_BY_LABEL.get(str(i.get("slot") or "").strip().lower(), "")


def gear_columns(items, bottom_slots=GEAR_BOTTOM) -> tuple[list, list, list]:
    """Gear laid out like the in-game character sheet, always the same shape: (left, right, bottom)
    lists of (position or None, item or None, slot label). Every slot is listed, empty ones too, so
    characters with missing items (or imported before slot types were kept) line up the same way.
    Items in slots that aren't listed are added after the weapon row."""
    by_slot: dict[str, tuple[int, dict]] = {}
    extra = []
    for n, i in enumerate(items or []):
        if not isinstance(i, dict):
            continue
        st = _slot_type(i)
        if st and st not in by_slot:
            by_slot[st] = (n, i)
        else:
            extra.append((n, i, i.get("slot") or ""))
    bottom_slots = tuple(bottom_slots or GEAR_BOTTOM)

    def col(slots):
        out = []
        for st in slots:
            n, i = by_slot.pop(st, (None, None))
            out.append((n, i, SLOT_LABELS.get(st, st.replace("_", " ").title())))
        return out
    left, right, bottom = col(GEAR_LEFT), col(GEAR_RIGHT), col(bottom_slots)
    rest = sorted(by_slot.values(), key=lambda x: x[0])
    bottom += [(n, i, i.get("slot") or "") for n, i in rest] + extra
    return left, right, bottom


TRIM_MAX_PIXELS = 4_000_000


def trim_render(body: bytes, ctype: str, margin: float = 0.04) -> tuple[bytes, str]:
    """Blizzard's full-body render is a large transparent canvas with the character small in the
    middle; crop it to the character (plus a small margin) so it fills the space beside the gear.
    Anything that isn't a transparent PNG, or can't be read, is returned unchanged."""
    if ctype != "image/png":
        return body, ctype
    import io

    from PIL import Image
    try:
        with Image.open(io.BytesIO(body)) as im:
            if im.format != "PNG" or im.width * im.height > TRIM_MAX_PIXELS or "A" not in im.getbands():
                return body, ctype
            box = im.getchannel("A").getbbox()
            if not box:
                return body, ctype
            pad = int(max(box[2] - box[0], box[3] - box[1]) * margin)
            box = (max(0, box[0] - pad), max(0, box[1] - pad), min(im.width, box[2] + pad), min(im.height, box[3] + pad))
            if box == (0, 0, im.width, im.height):
                return body, ctype
            out = io.BytesIO()
            im.crop(box).save(out, "PNG", compress_level=6)
            return out.getvalue(), "image/png"
    except (OSError, ValueError, Image.DecompressionBombError):
        return body, ctype


def cut_backdrop(body: bytes, ctype: str, threshold: int = 5, margin: float = 0.04,
                 max_side: int = 420) -> tuple[bytes, str]:
    """Blizzard's creature pictures (pets) are JPEGs with the animal on a flat backdrop. Make the
    backdrop transparent (only the part connected to the edges, so dark patches inside the animal
    stay) and crop to the animal, so it can stand on the page like the character does. Returns a
    PNG; anything that can't be read, or has no clear backdrop, is returned unchanged."""
    if ctype not in ("image/jpeg", "image/png"):
        return body, ctype
    import io

    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    try:
        with Image.open(io.BytesIO(body)) as src:
            if src.width * src.height > TRIM_MAX_PIXELS:
                return body, ctype
            im = src.convert("RGB")
        w, h = im.size
        edge = ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))
        corners = [im.getpixel(p) for p in edge]
        bg = tuple(sorted(c[i] for c in corners)[1] for i in range(3))
        if max(abs(c[i] - bg[i]) for c in corners for i in range(3)) > 3 * threshold:
            return body, ctype                                   # corners differ: no flat backdrop
        diff = ImageChops.difference(im, Image.new("RGB", im.size, bg)).convert("L")
        near = diff.point(lambda v: 255 if v <= threshold else 0)
        for p in edge:
            if near.getpixel(p) == 255:
                ImageDraw.floodfill(near, p, 128)
        alpha = near.point(lambda v: 0 if v == 128 else 255)
        box = alpha.getbbox()
        if not box or (box[2] - box[0]) * (box[3] - box[1]) > 0.9 * w * h:
            return body, ctype
        rgba = im.copy()
        rgba.putalpha(alpha.filter(ImageFilter.GaussianBlur(1.2)))      # soft edge
        pad = int(max(box[2] - box[0], box[3] - box[1]) * margin)
        rgba = rgba.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(w, box[2] + pad), min(h, box[3] + pad)))
        rgba.thumbnail((max_side, max_side))
        out = io.BytesIO()
        rgba.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    except (OSError, ValueError, Image.DecompressionBombError):
        return body, ctype


GEAR_PORTRAIT_KEYS = ("main-raw", "main", "inset", "avatar")


def silhouette_key(race_key: str | None, available: frozenset[str]) -> str:
    """The placeholder figure shown when a character has no picture: its race's, else a generic one."""
    k = (race_key or "").strip().lower().replace(" ", "_").replace("-", "_").replace("'", "")
    return k if k in available else "generic"


def portrait_state(groups, visible=None) -> str:
    """For the gear centre: "picture" (one to show), "none_yet" (Portrait imported and visible, but
    Blizzard had no picture) or "hidden" (Portrait not imported, or not shared with this viewer)."""
    if gear_portrait_key(groups, visible):
        return "picture"
    if isinstance(groups, dict) and "portrait" in groups and (visible is None or "portrait" in visible):
        return "none_yet"
    return "hidden"


def gear_portrait_key(groups, visible=None) -> str | None:
    """The portrait shown between the gear columns: only when both Gear and Portrait are visible to
    this viewer. Prefers the full-body render, like the in-game character panel."""
    if not isinstance(groups, dict) or "gear" not in groups or "portrait" not in groups:
        return None
    if visible is not None and not {"gear", "portrait"} <= set(visible):
        return None
    keys = [a.get("key") for a in groups.get("portrait") or [] if isinstance(a, dict)]
    return next((k for k in GEAR_PORTRAIT_KEYS if k in keys), None)


def icon_url(data, group: str, ref: str) -> str | None:
    """The stored icon URL for a gear or pet position ("3") or a talent ("0-1-4": group, spec, talent)."""
    try:
        idx = [int(x) for x in ref.split("-")]
        if group in ("gear", "pets") and len(idx) == 1 and isinstance(data, list):
            row = data[idx[0]]
        elif group == "tree" and len(idx) == 2 and isinstance(data, dict):
            row = data["trees"][idx[0]]["nodes"][idx[1]]
        elif group == "talents" and len(idx) == 3 and isinstance(data, dict):
            row = data["groups"][idx[0]]["specs"][idx[1]]["talents"][idx[2]]
        else:
            return None
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    url = row.get("icon") if isinstance(row, dict) else None
    return url if isinstance(url, str) and url.startswith("https://") else None


def remove_profile(s: Session, cfg: Config, actor: Actor, character_id: int) -> bool:
    require_member(cfg, actor)
    ch = _own(s, actor, character_id)
    n = s.execute(delete(CharacterProfile).where(CharacterProfile.character_id == ch.id)).rowcount
    if n:
        audit.record(s, actor, "character.profile_remove", "character", ch.id)
    return bool(n)


def _bulk_groups(prof: CharacterProfile) -> set[str]:
    """The groups a refresh keeps: what was imported, plus groups since split out of those."""
    got = set(prof.groups or {})
    return {g for g in GROUP_KEYS if g in got or SHARE_INHERITS.get(g) in got}


def refresh_all(s: Session, cfg: Config, bz, actor: Actor, level: str = "") -> tuple[list[str], list[str]]:
    """Refresh every one of the member's imported characters from Blizzard, keeping each one's groups.
    level: one sharing choice for every group of every character, or "" to keep each one's choices.
    At most [blizzard] refresh_all_max characters (oldest import first). Returns (refreshed, failed)
    character names; one character failing doesn't stop the others."""
    from ..integrations.blizzard import ApiError, Unsupported
    require_member(cfg, actor)
    if level and level not in SHARE_LEVELS:
        raise Invalid("Pick who can see it.")
    rows = s.execute(select(CharacterProfile, Character).join(Character, Character.id == CharacterProfile.character_id)
                     .where(CharacterProfile.member_id == actor.member_id, Character.archived_at.is_(None))
                     .order_by(CharacterProfile.fetched_at)).all()
    limit = cfg["blizzard"].get("refresh_all_max", 10)
    done, failed = [], []
    for prof, ch in rows:
        if cfg.edition(ch.edition).capability("character") == "unsupported":
            continue
        if len(done) + len(failed) >= limit:
            failed.append(ch.name)
            continue
        groups = _bulk_groups(prof)
        shares = {g: (level or share_choice(prof, g)) for g in GROUP_KEYS}
        try:
            with s.begin_nested():
                import_profile(s, cfg, bz, actor, ch.id, groups, shares)
            done.append(ch.name)
        except (Invalid, NotFound, ApiError, Unsupported):
            failed.append(ch.name)
    return done, failed


def refresh_one(s: Session, cfg: Config, bz, actor: Actor, character_id: int) -> CharacterProfile:
    """Refresh one imported character from Blizzard with the same groups and sharing choices (the
    character page's Refresh from Blizzard). Only the owner; NotFound if nothing is imported yet."""
    require_member(cfg, actor)
    prof = s.get(CharacterProfile, character_id)
    if prof is None or prof.member_id != actor.member_id:
        raise NotFound("Nothing imported for this character yet.")
    return import_profile(s, cfg, bz, actor, character_id, _bulk_groups(prof),
                          {g: share_choice(prof, g) for g in GROUP_KEYS})


def operator_refresh_list(s: Session, cfg: Config) -> list[tuple[int, str, int]]:
    """(character id, name, member id) for every imported character the operator command can refresh:
    not archived, on an edition whose character pages Blizzard serves. Oldest import first."""
    rows = s.execute(select(CharacterProfile, Character).join(Character, Character.id == CharacterProfile.character_id)
                     .where(Character.archived_at.is_(None)).order_by(CharacterProfile.fetched_at)).all()
    return [(ch.id, ch.name, ch.member_id) for _p, ch in rows
            if cfg.edition(ch.edition).capability("character") != "unsupported"]


def operator_refresh(s: Session, cfg: Config, bz, character_id: int) -> CharacterProfile:
    """questersboard-admin refresh-details: refresh one imported character from Blizzard on its owner's
    behalf, with the same groups and sharing choices (exactly what their own Refresh would do). Runs as
    the system actor scoped to the owner, so it never changes who owns or can see anything."""
    prof = s.get(CharacterProfile, character_id)
    if prof is None:
        raise NotFound("Nothing imported for this character.")
    from .actor import SYSTEM
    actor = SYSTEM.with_member(prof.member_id)
    return import_profile(s, cfg, bz, actor, character_id, _bulk_groups(prof),
                          {g: share_choice(prof, g) for g in GROUP_KEYS})


def share_all(s: Session, cfg: Config, actor: Actor, level: str) -> int:
    """One sharing choice for every imported group of every one of the member's characters (no
    Blizzard calls). Beyond private still only applies while a character is verified as theirs."""
    require_member(cfg, actor)
    if level not in SHARE_LEVELS:
        raise Invalid("Pick who can see it.")
    n = 0
    for prof in s.scalars(select(CharacterProfile).where(CharacterProfile.member_id == actor.member_id)):
        prof.shares = {g: level for g in (prof.groups or {})}
        n += 1
    if n:
        audit.record(s, actor, "character.profile_share_all", "member", actor.member_id, level=level, count=n)
    return n


def drop_for_character(s: Session, character_id: int) -> None:
    """Used when a character is archived or its identity changes: imported details no longer apply."""
    s.execute(delete(CharacterProfile).where(CharacterProfile.character_id == character_id))


def profiles_for(s: Session, member_id: int) -> dict[int, CharacterProfile]:
    return {p.character_id: p for p in s.scalars(select(CharacterProfile)
                                                  .where(CharacterProfile.member_id == member_id))}


# A group split out of another keeps that group's sharing until the member picks one.
SHARE_INHERITS = {"pets": "progress"}


def share_choice(prof: CharacterProfile | None, group: str) -> str:
    """The sharing level to pre-select on the import form for a group."""
    shares = (prof.shares or {}) if prof is not None else {}
    if group in shares:
        return shares[group]
    return shares.get(SHARE_INHERITS.get(group, ""), "private")


def effective_share(prof: CharacterProfile, ch: Character, group: str) -> str:
    """Who can see a group right now: beyond private only while the character is verified as theirs."""
    level = (prof.shares or {}).get(group, "private")
    if ch.verification != "ownership_verified" or level not in SHARE_LEVELS:
        return "private"
    if level == "guild" and not prof.guild_id:
        return "private"
    return level


# ------------------------------------------------------------------ hunter pets, stats display
def pet_family(p: dict) -> str:
    """Forever/Classic pets carry the beast as `creature`; Retail as `creature_family`."""
    return _label(p.get("creature_family")) or _label(p.get("creature")) or _label(p.get("family"))


PERCENT_STATS = {"melee_crit", "ranged_crit", "spell_crit", "melee_hit", "ranged_hit", "spell_hit", "dodge", "parry",
                 "block"}
STAT_SECTIONS = (
    ("Basics", ("health", "power")),
    ("Attributes", ("strength", "agility", "stamina", "intellect", "spirit")),
    ("Melee", ("attack_power", "melee_crit", "melee_hit", "expertise")),
    ("Ranged", ("ranged_attack_power", "ranged_crit", "ranged_hit")),
    ("Spell", ("spell_power", "spell_healing", "spell_crit", "spell_hit", "mana_regen")),
    ("Defense", ("armor", "defense", "dodge", "parry", "block")),
    ("Resistances", ("fire_resistance", "frost_resistance", "nature_resistance", "shadow_resistance",
                     "arcane_resistance")),
)
STAT_LABELS = {"attack_power": "Attack power", "ranged_attack_power": "Attack power", "melee_crit": "Crit",
               "ranged_crit": "Crit", "spell_crit": "Crit", "melee_hit": "Hit", "ranged_hit": "Hit", "spell_hit": "Hit",
               "spell_power": "Spell power", "spell_healing": "Healing", "mana_regen": "Mana regen",
               "fire_resistance": "Fire", "frost_resistance": "Frost", "nature_resistance": "Nature",
               "shadow_resistance": "Shadow", "arcane_resistance": "Arcane"}


def stat_sections(stats: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """Stats grouped for display; zeros are left out and crit, hit, dodge, parry and block show as %."""
    out = []
    for title, keys in STAT_SECTIONS:
        rows = []
        for k in keys:
            v = stats.get(k)
            if not isinstance(v, (int, float)) or v == 0:
                continue
            label = STAT_LABELS.get(k, k.replace("_", " ").capitalize())
            if k == "power" and stats.get("power_type"):
                label = str(stats["power_type"])
            if k in PERCENT_STATS:
                text = f"{v:.2f}%".replace(".00%", "%")
            elif isinstance(v, float) and not v.is_integer():
                text = f"{v:.1f}"
            else:
                text = f"{int(v):,}"
            rows.append((label, text))
        if rows:
            out.append((title, rows))
    return out


# ------------------------------------------------------------------ who sees what
def viewer_guilds(s: Session, member_id: int) -> set[tuple[str, str, str]]:
    """(edition, region, guild_id) the viewer is confirmed in and listed on: a Battle.net-verified
    character of theirs whose imported guild is shared (My guild or All members). Seeing a guild page
    means being on it."""
    q = (select(Character, CharacterProfile).join(CharacterProfile, CharacterProfile.character_id == Character.id)
         .where(CharacterProfile.member_id == member_id, CharacterProfile.guild_id.is_not(None),
                Character.archived_at.is_(None), Character.verification == "ownership_verified"))
    return {(p.edition, p.region, p.guild_id) for ch, p in s.execute(q)
            if "guild" in (p.groups or {}) and effective_share(p, ch, "guild") != "private"}


def visible_groups(prof: CharacterProfile, ch: Character, viewer_id: int,
                   vguilds: set[tuple[str, str, str]]) -> list[str]:
    if ch.member_id == viewer_id:
        return [g for g in GROUP_KEYS if g in (prof.groups or {})]
    same_guild = bool(prof.guild_id) and (prof.edition, prof.region, prof.guild_id) in vguilds
    out = []
    for g in GROUP_KEYS:
        if g not in (prof.groups or {}):
            continue
        lv = effective_share(prof, ch, g)
        if lv == "members" or (lv == "guild" and same_guild):
            out.append(g)
    return out


def view_character(s: Session, cfg: Config, actor: Actor, character_id: int):
    """(character, profile, visible groups) for someone looking at a character. NotFound unless at least
    one group is visible to them, so a private character's existence isn't revealed."""
    require_member(cfg, actor)
    ch = s.get(Character, character_id)
    prof = s.get(CharacterProfile, character_id) if ch is not None else None
    if ch is None or prof is None or ch.archived_at is not None:
        raise NotFound("Character not found.")
    vis = visible_groups(prof, ch, actor.member_id, viewer_guilds(s, actor.member_id))
    if not vis:
        raise NotFound("Character not found.")
    return ch, prof, vis


def _best_spec(talents: dict | None) -> str:
    if not talents:
        return ""
    if talents.get("active"):
        return talents["active"]
    groups = talents.get("groups") or []
    grp = next((g for g in groups if g.get("active")), groups[0] if groups else None)
    specs = [sp for sp in (grp or {}).get("specs", []) if sp.get("name")]
    return max(specs, key=lambda sp: sp.get("points") or 0)["name"] if specs else ""


@dataclass
class RosterRow:
    ch: Character
    prof: CharacterProfile
    visible: list[str]
    item_level: int | None
    spec: str
    professions: list[str]
    recipes: list[tuple[str, str]] = field(default_factory=list)    # (recipe, profession), when shared
    crafts: list[tuple[str, str]] = field(default_factory=list)     # the ones matching "Who can craft"


def _row(ch: Character, prof: CharacterProfile, vis: list[str]) -> RosterRow:
    g = prof.groups or {}
    ilvl = (g.get("overview") or {}).get("equipped_item_level") if "overview" in vis else None
    spec = (_best_spec(g.get("talents")) if "talents" in vis else "") or (ch.spec or "")
    shown = [p for p in g.get("professions") or [] if p.get("name")] if "professions" in vis else []
    recipes = [(r["name"], p["name"]) for p in shown for r in p.get("recipes") or [] if r.get("name")]
    return RosterRow(ch, prof, vis, ilvl, spec, [p["name"] for p in shown], recipes)


def guild_roster(s: Session, cfg: Config, actor: Actor, edition: str, region: str, guild_id: str):
    """Characters that shared their guild membership, for confirmed members of that guild only.
    Returns (guild name, rows). NotFound for anyone outside the guild or when nobody shared."""
    require_member(cfg, actor)
    vg = viewer_guilds(s, actor.member_id)
    if (edition, region, guild_id) not in vg:
        raise NotFound("Guild page not found.")
    rows, name = [], ""
    q = (select(Character, CharacterProfile).join(CharacterProfile, CharacterProfile.character_id == Character.id)
         .where(CharacterProfile.edition == edition, CharacterProfile.region == region,
                CharacterProfile.guild_id == guild_id, Character.archived_at.is_(None),
                Character.verification == "ownership_verified").order_by(Character.name))
    for ch, prof in s.execute(q):
        if "guild" not in (prof.groups or {}) or effective_share(prof, ch, "guild") == "private":
            continue                       # listed only when they shared their guild membership
        vis = visible_groups(prof, ch, actor.member_id, vg)
        rows.append(_row(ch, prof, vis))
        name = name or (prof.guild_name or "")
    if not rows:
        raise NotFound("Guild page not found.")
    return name, rows


def shared_with_members(s: Session, cfg: Config, actor: Actor) -> list[RosterRow]:
    """Characters with at least one group shared with all QuestersBoard members."""
    require_member(cfg, actor)
    vg = viewer_guilds(s, actor.member_id)
    q = (select(Character, CharacterProfile).join(CharacterProfile, CharacterProfile.character_id == Character.id)
         .where(Character.archived_at.is_(None), Character.verification == "ownership_verified")
         .order_by(Character.name))
    out = []
    for ch, prof in s.execute(q):
        if any(effective_share(prof, ch, g) == "members" for g in (prof.groups or {})):
            out.append(_row(ch, prof, visible_groups(prof, ch, actor.member_id, vg)))
    return out


def craft_query(text: str | None) -> str:
    """The "Who can craft" search, tidied: trimmed, single spaces, at most 80 characters."""
    return " ".join((text or "").split())[:80]


def filter_rows(rows: list[RosterRow], f: dict) -> list[RosterRow]:
    """Filters from the query string: class, role, spec, profession, min_level, min_ilvl, edition, and
    craft (part of a recipe name; only recipes in Professions shared with this viewer count)."""
    def num(k):
        try:
            return int(f.get(k) or 0)
        except ValueError:
            return 0
    out = []
    for r in rows:
        if f.get("edition") and r.ch.edition != f["edition"]:
            continue
        if f.get("class") and r.ch.class_key != f["class"]:
            continue
        if f.get("role") and f["role"] not in (r.ch.eligible_roles or []):
            continue
        if f.get("spec") and r.spec != f["spec"]:
            continue
        if f.get("profession") and f["profession"] not in r.professions:
            continue
        if num("min_level") and (r.ch.level or 0) < num("min_level"):
            continue
        if num("min_ilvl") and (r.item_level or 0) < num("min_ilvl"):
            continue
        q = craft_query(f.get("craft")).casefold()
        if q:
            hits = [x for x in r.recipes if q in x[0].casefold()]
            if not hits:
                continue
            r = replace(r, crafts=hits)
        out.append(r)
    return out


def filter_options(rows: list[RosterRow]) -> dict[str, list[str]]:
    return {"class": sorted({r.ch.class_key for r in rows}),
            "role": sorted({x for r in rows for x in (r.ch.eligible_roles or [])}),
            "spec": sorted({r.spec for r in rows if r.spec}),
            "profession": sorted({p for r in rows for p in r.professions}),
            "craft": sorted({x[0] for r in rows for x in r.recipes}, key=str.casefold),
            "edition": sorted({r.ch.edition for r in rows})}


def my_guild_links(s: Session, member_id: int) -> list[dict]:
    """Guild pages the member can open (confirmed in the guild and someone has shared)."""
    out = []
    for e, r, g in sorted(viewer_guilds(s, member_id)):
        name = s.scalar(select(CharacterProfile.guild_name).where(CharacterProfile.edition == e,
                                                                  CharacterProfile.region == r,
                                                                  CharacterProfile.guild_id == g).limit(1))
        out.append({"edition": e, "region": r, "guild_id": g, "name": name or "Guild"})
    return out
