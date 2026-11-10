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

from dataclasses import dataclass

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
    Group("professions", "Professions", "professions and skill levels", ("/professions",)),
    Group("stats", "Stats", "health, attributes, armor, crit, hit, resistances", ("/statistics",)),
    Group("portrait", "Portrait", "Blizzard's character images", ("/character-media",)),
    Group("pvp", "PvP", "honorable kills and rank", ("/pvp-summary",)),
    Group("progress", "Progress", "reputations, quests, achievements, mounts, hunter pets",
          ("/reputations", "/quests/completed", "/achievements", "/collections/mounts", "/hunter-pets")),
)
GROUP_KEYS = tuple(g.key for g in GROUPS)
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
            out.append({"name": _label(p.get("profession")), "kind": "primary" if kind == "primaries" else "secondary",
                        "skill": skill, "max": top})
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
    hp = pages.get("/hunter-pets")
    if hp is not None:
        out["hunter_pets"] = [{"name": p.get("name"), "level": p.get("level"), "family": pet_family(p)}
                              for p in hp.get("hunter_pets") or [] if isinstance(p, dict)]
    return out


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
            "portrait": _portrait, "pvp": _pvp}[group](body)


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
    data = {g: v for g, v in data.items() if v not in (None, {}, [])}
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
    if not bzc.get("detail_icons", True):
        return
    sns = ed.namespace("static", region)
    jobs = [(row, ref["path"], ref["ns"]) for row, ref in refs if ref and _same_static(ref["ns"], sns)]
    for grp in (data.get("talents") or {}).get("groups") or []:
        for sp in grp.get("specs") or []:
            for t in sp.get("talents") or []:
                if isinstance(t.get("spell_id"), int) and sns:
                    jobs.append((t, f"/data/wow/media/spell/{t['spell_id']}", sns))
    budget = int(bzc.get("icon_lookups", 80))
    seen: dict[tuple[str, str], str | None] = {}
    for row, path, ns in jobs:
        key = (path, ns)
        if key not in seen:
            if budget <= 0:
                continue
            budget -= 1
            seen[key] = bz.media_icon(s, region, path, ns)
        if seen[key]:
            row["icon"] = seen[key]


GEAR_LEFT = ("HEAD", "NECK", "SHOULDER", "BACK", "CHEST", "SHIRT", "TABARD", "WRIST")
GEAR_RIGHT = ("HANDS", "WAIST", "LEGS", "FEET", "FINGER_1", "FINGER_2", "TRINKET_1", "TRINKET_2")


def gear_columns(items) -> tuple[list, list, list]:
    """Gear laid out like the in-game character sheet: (left, right, bottom) lists of (position, item).
    Weapons, and items imported before slot types were kept, go in the bottom row."""
    left, right, bottom = [], [], []
    for n, i in enumerate(items or []):
        st = i.get("slot_type") or ""
        (left if st in GEAR_LEFT else right if st in GEAR_RIGHT else bottom).append((n, i))
    left.sort(key=lambda x: GEAR_LEFT.index(x[1]["slot_type"]))
    right.sort(key=lambda x: GEAR_RIGHT.index(x[1]["slot_type"]))
    return left, right, bottom


GEAR_PORTRAIT_KEYS = ("main-raw", "main", "inset", "avatar")


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
    """The stored icon URL for a gear position ("3") or a talent ("0-1-4": group, spec, talent)."""
    try:
        idx = [int(x) for x in ref.split("-")]
        if group == "gear" and len(idx) == 1 and isinstance(data, list):
            row = data[idx[0]]
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


def drop_for_character(s: Session, character_id: int) -> None:
    """Used when a character is archived or its identity changes: imported details no longer apply."""
    s.execute(delete(CharacterProfile).where(CharacterProfile.character_id == character_id))


def profiles_for(s: Session, member_id: int) -> dict[int, CharacterProfile]:
    return {p.character_id: p for p in s.scalars(select(CharacterProfile)
                                                  .where(CharacterProfile.member_id == member_id))}


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


def _row(ch: Character, prof: CharacterProfile, vis: list[str]) -> RosterRow:
    g = prof.groups or {}
    ilvl = (g.get("overview") or {}).get("equipped_item_level") if "overview" in vis else None
    spec = (_best_spec(g.get("talents")) if "talents" in vis else "") or (ch.spec or "")
    profs = [p["name"] for p in g.get("professions") or [] if p.get("name")] if "professions" in vis else []
    return RosterRow(ch, prof, vis, ilvl, spec, profs)


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


def filter_rows(rows: list[RosterRow], f: dict) -> list[RosterRow]:
    """Filters from the query string: class, role, spec, profession, min_level, min_ilvl, edition."""
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
        out.append(r)
    return out


def filter_options(rows: list[RosterRow]) -> dict[str, list[str]]:
    return {"class": sorted({r.ch.class_key for r in rows}),
            "role": sorted({x for r in rows for x in (r.ch.eligible_roles or [])}),
            "spec": sorted({r.spec for r in rows if r.spec}),
            "profession": sorted({p for r in rows for p in r.professions}),
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
