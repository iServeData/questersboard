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


def _gear(body: dict) -> list[dict]:
    out = []
    for i in body.get("equipped_items") or []:
        if not isinstance(i, dict):
            continue
        out.append({"slot": _label(i.get("slot")), "name": _label(i),
                    "item_level": (i.get("level") or {}).get("value"),
                    "quality": ((i.get("quality") or {}).get("type") or "").lower(),
                    "item_id": (i.get("item") or {}).get("id"),
                    "enchants": [e.get("display_string") for e in i.get("enchantments") or []
                                 if isinstance(e, dict) and e.get("display_string")][:4]})
    return out


def _talents(body: dict) -> dict:
    groups = []
    for g in body.get("specialization_groups") or []:
        specs = []
        for sp in g.get("specializations") or []:
            talents = []
            for t in sp.get("talents") or []:
                name = _label(t.get("talent")) or _label(((t.get("spell_tooltip") or {}).get("spell")))
                if name:
                    talents.append({"name": name, "rank": t.get("talent_rank")})
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
        out["hunter_pets"] = [{"name": p.get("name"), "level": p.get("level"),
                               "family": _label(p.get("creature_family"))}
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
    for g in GROUP_KEYS:
        v = form.get(f"share_{g}") or "private"
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
