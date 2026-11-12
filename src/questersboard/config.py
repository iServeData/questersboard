"""Configuration: one TOML file shared by the web app, bot and worker.

No secrets live here. Validation is strict: an invalid file stops the service at
startup (exit 78), and a live reload is only applied when the whole new file
validates. Error messages name the setting, never a secret value.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .envvars import getenv

log = logging.getLogger(__name__)

DEFAULT_PATH = "/etc/questersboard/config.toml"
SNOWFLAKE = re.compile(r"^\d{15,22}$")
KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# How ruleset keys are shown; an edition's ruleset_labels table overrides these.
RULESET_LABELS = {"normal": "Normal", "pvp": "PvP", "rp": "RP", "rp_pvp": "RP-PvP", "hardcore": "Hardcore"}

# Largest group per activity type (WoW group sizes). Override in [signups.party_size_max]; others cap at 80.
DEFAULT_PARTY_SIZE = {"quest": 5, "dungeon": 5, "arena": 5, "raid": 40, "battleground": 40}
DEFAULT_FACTION_COLORS = {"horde": "#C41E3A", "alliance": "#1F6FD8", "any": "#C9A227"}
# The bot's own Discord emojis shown next to role names on event posts (questersboard-admin role-emojis).
DEFAULT_ROLE_ICONS = {"tank": "qb_tank", "healer": "qb_healer", "damage": "qb_damage"}
EMOJI_NAME = re.compile(r"^[A-Za-z0-9_]{2,32}$")
COVER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,60}\.(webp|png|jpg|jpeg)$")

LIVE_SECTIONS = ("roles", "publishing", "scheduling", "signups", "reminders", "chronicle", "rate_limits", "status",
                 "event_posts")
API_STATUSES = ("verified", "unverified", "unsupported")
API_CAPS = ("reference", "character", "quests", "locations", "ownership")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Edition:
    key: str
    name: str
    uses_realms: bool
    rulesets: tuple[str, ...]
    default_ruleset: str
    regions: tuple[str, ...]
    default_region: str
    factions: tuple[str, ...]
    default_faction: str
    max_level: int
    catalog: str
    static_namespace: str
    dynamic_namespace: str
    profile_namespace: str
    ruleset_realm_slugs: dict[str, str]
    api_status: dict[str, str]
    ruleset_labels: dict[str, str] = field(default_factory=dict)
    difficulties: tuple[str, ...] = ("Normal",)
    # Realmless editions: Blizzard's numeric ruleset IDs mapped to QuestersBoard ruleset keys
    # (Battle.net sync reads a character's ruleset as an ID or a name).
    ruleset_api_ids: dict[str, int] = field(default_factory=dict)
    # Optional plain links from item and talent tooltips to a database site, e.g.
    # "https://www.wowhead.com/forever/item={id}". Empty = no link. Links only: no third-party script.
    item_link_template: str = ""
    spell_link_template: str = ""
    # The weapon row under the gear columns, left to right; every listed slot is always shown (empty
    # ones too) so every character's panel has the same shape. Retail has no ranged or ammo slot.
    gear_bottom_slots: tuple[str, ...] = ("MAIN_HAND", "OFF_HAND", "RANGED")
    # Classic-style talent trees (one grid per specialization) drawn from Blizzard's talent-tree pages;
    # the number of columns in each tree, or 0 to keep the plain talent list.
    talent_tree_columns: int = 0

    def item_link(self, item_id) -> str:
        return self.item_link_template.replace("{id}", str(int(item_id))) if self.item_link_template and \
            isinstance(item_id, int) else ""

    def spell_link(self, spell_id) -> str:
        return self.spell_link_template.replace("{id}", str(int(spell_id))) if self.spell_link_template and \
            isinstance(spell_id, int) else ""

    def ruleset_label(self, ruleset: str | None) -> str:
        if not ruleset:
            return ""
        return self.ruleset_labels.get(ruleset) or RULESET_LABELS.get(ruleset) or ruleset.replace("_", " ").title()

    def namespace(self, kind: str, region: str) -> str | None:
        tpl = {"static": self.static_namespace, "dynamic": self.dynamic_namespace,
               "profile": self.profile_namespace}[kind]
        if not tpl:
            return None
        return tpl.replace("{region}", region)

    def capability(self, cap: str) -> str:
        return self.api_status.get(cap, "unsupported")


@dataclass(frozen=True)
class RolePreset:
    key: str
    label: str


@dataclass(frozen=True)
class Activity:
    key: str
    type: str
    label: str
    min_minutes: int
    min_size: int
    max_size: int
    roles: dict[str, int]
    tags: tuple[str, ...]


@dataclass
class Config:
    raw: dict[str, Any]
    path: str = ""
    section_hashes: dict[str, str] = field(default_factory=dict)

    # convenience accessors -------------------------------------------------
    def __getitem__(self, k: str) -> Any:
        return self.raw[k]

    @property
    def guild_id(self) -> str:
        return self.raw["community"]["guild_id"]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.raw["community"]["default_timezone"])

    @property
    def max_simultaneous(self) -> int:
        return int(self.raw["scheduling"]["max_simultaneous_wow_events"])

    def role_ids(self, capability: str) -> frozenset[str]:
        return frozenset(self.raw["roles"].get(capability, []))

    def voice_protected_roles(self) -> frozenset[str]:
        """Roles hosts can't kick or ban from event voice: [voice] protected_role_ids, the voice staff roles
        and the [roles] admin roles. (The server owner and bots are always protected.)"""
        return frozenset(self.raw["voice"]["protected_role_ids"]) | self.voice_staff_roles() | self.role_ids("admin")

    def voice_staff_roles(self) -> frozenset[str]:
        """Server roles that may join every event voice channel without signing up: [voice] staff_role_ids,
        plus the [roles] event_moderator roles when [voice] moderators_can_join is on."""
        v = self.raw["voice"]
        return frozenset(v["staff_role_ids"]) | (self.role_ids("event_moderator") if v["moderators_can_join"]
                                                 else frozenset())

    @property
    def editions(self) -> dict[str, Edition]:
        return self.raw["_editions"]

    def edition(self, key: str) -> Edition:
        try:
            return self.editions[key]
        except KeyError:
            raise ConfigError(f"unknown edition {key!r}") from None

    @property
    def role_presets(self) -> tuple[RolePreset, ...]:
        return self.raw["_role_presets"]

    @property
    def activities(self) -> tuple[Activity, ...]:
        return self.raw["_activities"]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _req(d: dict, key: str, typ, where: str):
    if key not in d:
        raise ConfigError(f"[{where}] {key} is required")
    v = d[key]
    if typ is int and isinstance(v, bool):
        raise ConfigError(f"[{where}] {key} must be an integer")
    if not isinstance(v, typ):
        raise ConfigError(f"[{where}] {key} has the wrong type (expected {getattr(typ, '__name__', typ)})")
    return v


def _snowflake(v: Any, where: str, allow_empty: bool = True) -> str:
    if not isinstance(v, str):
        raise ConfigError(f"{where} must be a quoted string (Discord IDs are strings)")
    if v == "" and allow_empty:
        return v
    if not SNOWFLAKE.match(v):
        raise ConfigError(f"{where} is not a Discord ID")
    return v


def _snowflakes(v: Any, where: str) -> list[str]:
    if not isinstance(v, list):
        raise ConfigError(f"{where} must be a list of quoted Discord IDs")
    return [_snowflake(x, f"{where}[]", allow_empty=False) for x in v]


def _positive(v: Any, where: str, minimum: int = 1) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise ConfigError(f"{where} must be an integer >= {minimum}")
    return v


def _ruleset_ids(v, rulesets, where) -> dict[str, int]:
    if not isinstance(v, dict):
        raise ConfigError(f"{where} ruleset_api_ids must be a table like {{ pvp = 1 }}")
    out = {}
    for k, n in v.items():
        if k not in rulesets:
            raise ConfigError(f"{where} ruleset_api_ids.{k} isn't one of this edition's rulesets")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ConfigError(f"{where} ruleset_api_ids.{k} must be a positive number")
        out[str(k)] = n
    return out


def validate(raw: dict[str, Any]) -> dict[str, Any]:
    r = dict(raw)
    for sec in ("community", "discord", "roles", "scheduling", "signups", "reminders", "chronicle",
                "uploads", "ai", "feeds", "calendar", "web", "rate_limits", "database", "blizzard"):
        if not isinstance(r.get(sec), dict):
            raise ConfigError(f"[{sec}] section is missing")

    c = r["community"]
    _req(c, "name", str, "community")
    _snowflake(_req(c, "guild_id", str, "community"), "[community] guild_id")
    try:
        ZoneInfo(_req(c, "default_timezone", str, "community"))
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("[community] default_timezone is not a valid IANA zone") from None
    common = c.setdefault("common_timezones", [])
    if not isinstance(common, list) or not all(isinstance(z, str) for z in common):
        raise ConfigError("[community] common_timezones must be a list of IANA names")
    for z in common:
        try:
            ZoneInfo(z)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"[community] common_timezones: {z!r} is not a valid IANA zone") from None
    if not _req(c, "public_base_url", str, "community").startswith("https://") and \
            not c["public_base_url"].startswith("http://127.0.0.1") and \
            not c["public_base_url"].startswith("http://localhost"):
        raise ConfigError("[community] public_base_url must be https://")
    c.setdefault("member_role_ids", [])
    _snowflakes(c["member_role_ids"], "[community] member_role_ids")

    d = r["discord"]
    _snowflake(d.get("application_id", ""), "[discord] application_id")
    _snowflake(d.get("event_channel_id", ""), "[discord] event_channel_id")
    _snowflakes(d.get("allowed_command_channel_ids", []), "[discord] allowed_command_channel_ids")
    d.setdefault("review_channel_id", "")
    _snowflake(d["review_channel_id"], "[discord] review_channel_id")
    d.setdefault("publish_request_channel_id", "")   # empty: use review_channel_id
    _snowflake(d["publish_request_channel_id"], "[discord] publish_request_channel_id")
    for k in ("dm_enabled", "notify_dm_on_material_change"):
        _req(d, k, bool, "discord")
    _positive(d.get("reconfirm_window_hours", 24), "[discord] reconfirm_window_hours")

    for cap in ("event_creator", "event_moderator", "exclusive_designator", "chronicle_editor",
                "chronicle_reviewer", "trusted_author", "admin"):
        r["roles"].setdefault(cap, [])
        _snowflakes(r["roles"][cap], f"[roles] {cap}")
    unknown = set(r["roles"]) - {"event_creator", "event_moderator", "exclusive_designator",
                                 "chronicle_editor", "chronicle_reviewer", "trusted_author", "admin"}
    if unknown:
        raise ConfigError(f"[roles] unknown capability {sorted(unknown)[0]!r}")

    s = r["scheduling"]
    _positive(_req(s, "max_simultaneous_wow_events", int, "scheduling"),
              "[scheduling] max_simultaneous_wow_events")
    _positive(s.get("min_duration_minutes", 15), "[scheduling] min_duration_minutes")
    _positive(s.get("max_duration_hours", 12), "[scheduling] max_duration_hours")
    _positive(s.get("max_days_ahead", 180), "[scheduling] max_days_ahead")
    _positive(s.get("overrun_grace_minutes", 30), "[scheduling] overrun_grace_minutes", 0)
    _positive(s.get("max_recurrence_occurrences", 26), "[scheduling] max_recurrence_occurrences")
    s.setdefault("late_join_default", False)
    s.setdefault("reschedule_requires_reconfirm", True)
    _positive(s.setdefault("end_change_notice_only_minutes", 0),
              "[scheduling] end_change_notice_only_minutes", 0)

    sg = r["signups"]
    if sg.get("default_policy") not in ("open", "approval"):
        raise ConfigError("[signups] default_policy must be open or approval")
    sg.setdefault("allow_tentative", True)
    _positive(sg.get("max_custom_roles", 10), "[signups] max_custom_roles")
    ps = {**DEFAULT_PARTY_SIZE, **(sg.get("party_size_max") or {})}
    for k, v in ps.items():
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 80:
            raise ConfigError(f"[signups.party_size_max] {k} must be a whole number from 1 to 80")
    sg["party_size_max"] = ps

    offs = r["reminders"].get("offsets_minutes", [])
    if not isinstance(offs, list) or any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in offs):
        raise ConfigError("[reminders] offsets_minutes must be a list of positive integers")

    ch = r["chronicle"]
    if not str(ch.get("public_path", "")).startswith("/"):
        raise ConfigError("[chronicle] public_path must start with /")
    if ch.get("neutral_description_style", "role") not in ("role", "role_class"):
        raise ConfigError("[chronicle] neutral_description_style must be role or role_class")
    _positive(ch.get("max_body_chars", 8000), "[chronicle] max_body_chars", 200)
    # Search and link previews for the public pages (description, canonical, Open Graph, sitemap).
    ch.setdefault("description", "Stories from community WoW events")
    ch.setdefault("social_image", "")
    ch.setdefault("social_image_alt", "")
    ch.setdefault("social_image_width", 0)
    ch.setdefault("social_image_height", 0)
    ch.setdefault("sitemap", True)
    if not isinstance(ch["description"], str) or not ch["description"].strip() or len(ch["description"]) > 300:
        raise ConfigError("[chronicle] description must be 1-300 characters")
    if ch["social_image"] and not re.match(r"^(/|https://)", str(ch["social_image"])):
        raise ConfigError("[chronicle] social_image must start with / or https://")
    for k in ("social_image_width", "social_image_height"):
        if isinstance(ch[k], bool) or not isinstance(ch[k], int) or not 0 <= ch[k] <= 10000:
            raise ConfigError(f"[chronicle] {k} must be a whole number from 0 to 10000 (0 = not stated)")
    if not isinstance(ch["sitemap"], bool):
        raise ConfigError("[chronicle] sitemap must be true or false")

    up = r["uploads"]
    _positive(up.get("max_bytes", 5_000_000), "[uploads] max_bytes", 1024)
    bad = set(up.get("allowed_types", [])) - {"image/png", "image/jpeg", "image/webp"}
    if bad:
        raise ConfigError("[uploads] allowed_types may only contain png, jpeg, webp images")

    ai = r["ai"]
    _req(ai, "enabled", bool, "ai")
    if ai.get("provider") not in ("openai_compatible", "none"):
        raise ConfigError("[ai] provider must be openai_compatible or none")
    if ai["enabled"] and (not ai.get("model") or not str(ai.get("base_url", "")).startswith("https://")):
        raise ConfigError("[ai] enabled requires model and an https:// base_url")

    w = r["web"]
    if not re.match(r"^(fd:\d+|unix:/\S+|127\.0\.0\.1:\d{2,5}|\[::1\]:\d{2,5})$", str(w.get("bind", ""))):
        raise ConfigError("[web] bind must be fd:N, unix:/path, or a loopback host:port")
    if not str(w.get("member_path", "")).startswith("/"):
        raise ConfigError("[web] member_path must start with /")
    _positive(w.get("session_hours", 72), "[web] session_hours")
    lpa = w.get("link_preview_agents")
    if lpa is not None and (not isinstance(lpa, list) or not all(isinstance(x, str) and x.strip() for x in lpa)):
        raise ConfigError("[web] link_preview_agents must be a list of user-agent names, e.g. [\"Discordbot\"]")

    ret = r.setdefault("retention", {})
    for k, dflt in (("outbox_done_days", 14), ("api_cache_days", 30), ("audit_days", 400),
                    ("withdrawn_signup_days", 365), ("session_days_after_expiry", 1)):
        ret.setdefault(k, dflt)
        _positive(ret[k], f"[retention] {k}")

    lg = r.setdefault("legal", {})
    lg.setdefault("contact", "Message an organiser in the community Discord server.")
    lg.setdefault("effective_date", "")
    lg.setdefault("backup_keep_days", 30)
    lg.setdefault("operator", "")
    for k in ("contact", "effective_date", "operator"):
        if not isinstance(lg[k], str):
            raise ConfigError(f"[legal] {k} must be a string")
    if lg["effective_date"] and not re.match(r"^\d{4}-\d{2}-\d{2}$", lg["effective_date"]):
        raise ConfigError("[legal] effective_date must be YYYY-MM-DD")
    _positive(lg["backup_keep_days"], "[legal] backup_keep_days")
    if not lg["contact"].strip():
        raise ConfigError("[legal] contact must say how members reach the operators")

    stc = r.setdefault("status", {})
    for k, dflt, lo in (("starting_soon_minutes", 15, 0), ("heartbeat_seconds", 30, 5),
                        ("stale_after_seconds", 120, 15), ("voice_stale_after_seconds", 180, 15),
                        ("cache_seconds", 15, 0)):
        stc.setdefault(k, dflt)
        _positive(stc[k], f"[status] {k}", lo)
    if stc["stale_after_seconds"] < 2 * stc["heartbeat_seconds"]:
        raise ConfigError("[status] stale_after_seconds must be at least twice heartbeat_seconds")
    unknown = set(stc) - {"starting_soon_minutes", "heartbeat_seconds", "stale_after_seconds",
                          "voice_stale_after_seconds", "cache_seconds"}
    if unknown:
        raise ConfigError(f"[status] unknown setting {sorted(unknown)[0]!r}")

    ep = r.setdefault("event_posts", {})
    colors = {**DEFAULT_FACTION_COLORS, **(ep.get("faction_colors") or {})}
    for k, v in colors.items():
        if k not in ("horde", "alliance", "any") or not re.match(r"^#[0-9A-Fa-f]{6}$", str(v)):
            raise ConfigError("[event_posts] faction_colors must map horde, alliance, any to #RRGGBB colours")
    ep["faction_colors"] = colors
    ep.setdefault("default_cover", "")
    ep.setdefault("activity_covers", {})
    for k, v in [("default_cover", ep["default_cover"]), *ep["activity_covers"].items()]:
        if v and not COVER_NAME.match(str(v)):
            raise ConfigError(f"[event_posts] {k}: cover names are files in the covers folder, "
                              "like default_cover.webp (letters, digits, - and _ ; png, jpg or webp)")
    if not isinstance(ep.setdefault("field_spacing", True), bool):
        raise ConfigError("[event_posts] field_spacing must be true or false")
    icons = ep.setdefault("role_icons", dict(DEFAULT_ROLE_ICONS))
    if not isinstance(icons, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and EMOJI_NAME.match(v) for k, v in icons.items()):
        raise ConfigError("[event_posts] role_icons maps a role key to the bot's emoji name, e.g. "
                          "{ tank = \"qb_tank\" } (2-32 letters, digits or _); {} turns the icons off")
    if ep.setdefault("role_icon_position", "heading") not in ("line", "heading"):
        raise ConfigError("[event_posts] role_icon_position must be \"line\" or \"heading\"")
    th = r.setdefault("event_threads", {})
    th.setdefault("enabled", False)
    th.setdefault("private", False)
    th.setdefault("reminders_in_thread", True)
    th.setdefault("name_suffix", " · chat")
    _positive(th.setdefault("archive_after_hours", 24), "[event_threads] archive_after_hours", 0)
    for k in ("enabled", "private", "reminders_in_thread"):
        if not isinstance(th[k], bool):
            raise ConfigError(f"[event_threads] {k} must be true or false")
    if not isinstance(th["name_suffix"], str) or len(th["name_suffix"]) > 20:
        raise ConfigError("[event_threads] name_suffix must be text of 20 characters or fewer")

    vc = r.setdefault("voice", {})
    for k, dflt in (("enabled", False), ("moderators_can_join", True), ("remove_on_leave", False),
                    ("guest_invites", True)):
        if not isinstance(vc.setdefault(k, dflt), bool):
            raise ConfigError(f"[voice] {k} must be true or false")
    vc["category_id"] = _snowflake(vc.get("category_id", ""), "[voice] category_id")
    if vc["enabled"] and not vc["category_id"]:
        raise ConfigError("[voice] category_id is required when voice channels are enabled")
    vc.setdefault("name_template", "{title}")
    if not isinstance(vc["name_template"], str) or not vc["name_template"].strip() or \
            len(vc["name_template"]) > 100:
        raise ConfigError("[voice] name_template must be text of 100 characters or fewer, e.g. \"{title}\"")
    try:
        vc["name_template"].format(title="t", id=1)
    except (KeyError, IndexError, ValueError):
        raise ConfigError("[voice] name_template can only use {title} and {id}") from None
    _positive(vc.setdefault("user_limit", 0), "[voice] user_limit", 0)
    if vc["user_limit"] > 99:
        raise ConfigError("[voice] user_limit must be 0 (no cap) to 99")
    _positive(vc.setdefault("empty_teardown_minutes", 5), "[voice] empty_teardown_minutes", 1)
    vc["staff_role_ids"] = _snowflakes(vc.setdefault("staff_role_ids", []), "[voice] staff_role_ids")
    vc.setdefault("member_states", ["confirmed", "tentative"])
    if not isinstance(vc["member_states"], list) or not vc["member_states"] or \
            set(vc["member_states"]) - {"confirmed", "tentative", "waitlisted", "pending"}:
        raise ConfigError("[voice] member_states must list signup states from: confirmed, tentative, "
                          "waitlisted, pending")
    vc["protected_role_ids"] = _snowflakes(vc.setdefault("protected_role_ids", []), "[voice] protected_role_ids")
    _positive(vc.setdefault("max_guests_per_invite", 5), "[voice] max_guests_per_invite", 1)
    if vc["max_guests_per_invite"] > 25:
        raise ConfigError("[voice] max_guests_per_invite must be 1 to 25")
    if vc.setdefault("invite_dm", "off") not in ("off", "hosts", "everyone"):
        raise ConfigError("[voice] invite_dm must be \"off\", \"hosts\" or \"everyone\"")
    tpl = vc.setdefault("guest_link_template", "")
    if not isinstance(tpl, str) or (tpl and (not tpl.startswith("https://") or tpl.count("{code}") != 1)):
        raise ConfigError("[voice] guest_link_template must be empty or an https:// address containing {code} "
                          "once, e.g. \"https://short.example.org/v/{code}\"")
    unknown = set(vc) - {"enabled", "category_id", "name_template", "user_limit", "empty_teardown_minutes",
                         "staff_role_ids", "moderators_can_join", "remove_on_leave", "member_states",
                         "guest_invites", "protected_role_ids", "max_guests_per_invite", "invite_dm",
                         "guest_link_template"}
    if unknown:
        raise ConfigError(f"[voice] unknown setting {sorted(unknown)[0]!r}")

    # Who may publish events. open_to_all: every member may, except during probation (newer to the server
    # than probation_days), when their drafts go to the moderators. The event_creator role always may.
    pb = r.setdefault("publishing", {})
    pb.setdefault("open_to_all", True)
    _req(pb, "open_to_all", bool, "publishing")
    _positive(pb.setdefault("probation_days", 7), "[publishing] probation_days", 0)
    if set(pb) - {"open_to_all", "probation_days"}:
        raise ConfigError(f"[publishing] unknown setting {sorted(set(pb) - {'open_to_all', 'probation_days'})[0]!r}")

    b = r.setdefault("branding", {})
    b.setdefault("site_name", c["name"])
    b.setdefault("home_url", "/")
    b.setdefault("game_page_url", "")
    b.setdefault("stylesheet", "")
    b.setdefault("body_class", "theme-wow")
    b.setdefault("discord_invite_url", "")
    b.setdefault("nav", [])
    if not isinstance(b["nav"], list) or any(
            not isinstance(n, dict) or not str(n.get("label", "")) or
            not re.match(r"^(/|https://)", str(n.get("url", ""))) for n in b["nav"]):
        raise ConfigError("[branding] nav must be a list of { label, url } with / or https:// urls")
    for k in ("home_url", "game_page_url", "stylesheet", "discord_invite_url"):
        if b[k] and not re.match(r"^(/|https://)", str(b[k])):
            raise ConfigError(f"[branding] {k} must start with / or https://")
    if not re.match(r"^[a-z0-9_ -]{0,64}$", str(b["body_class"])):
        raise ConfigError("[branding] body_class may only contain lowercase letters, digits, - and _")
    # Footer: wordmark (bold middle part), disclaimer lines, credit lines and footer links. Plain text only.
    b.setdefault("wordmark", [b["site_name"]])
    if (not isinstance(b["wordmark"], list) or not 1 <= len(b["wordmark"]) <= 3
            or any(not isinstance(x, str) for x in b["wordmark"])):
        raise ConfigError("[branding] wordmark must be a list of 1 to 3 strings; the middle one is bold")
    b["wordmark"] = (list(b["wordmark"]) + ["", ""])[:3] if len(b["wordmark"]) > 1 else ["", b["wordmark"][0], ""]
    for k in ("footer_notes", "footer_credits"):
        b.setdefault(k, [])
        if not isinstance(b[k], list):
            raise ConfigError(f"[branding] {k} must be a list")
        lines = []
        for x in b[k]:
            if isinstance(x, str):
                x = {"text": x}
            if (not isinstance(x, dict) or set(x) - {"text", "link", "url", "after"}
                    or any(not isinstance(v, str) for v in x.values())
                    or (x.get("url") and not re.match(r"^(/|https://)", x["url"]))
                    or bool(x.get("link")) != bool(x.get("url"))):
                raise ConfigError(f"[branding] {k}: each line is a string or {{ text, link, url, after }} "
                                  "with a / or https:// url")
            lines.append({"text": x.get("text", ""), "link": x.get("link", ""), "url": x.get("url", ""),
                          "after": x.get("after", "")})
        b[k] = lines
    # Submenu under the main menu. Lead group (e.g. "WoW" Schedule) and the product label mirror the
    # community site's own submenu; the QuestersBoard dropdowns after them are built from the app's pages.
    gp = str(b["game_page_url"]).rstrip("/") + "/" if b["game_page_url"] else ""
    b.setdefault("subnav_lead_label", "WoW" if gp else "")
    b.setdefault("subnav_lead", [{"label": "Schedule", "url": gp + "#schedule"}] if gp else [])
    b.setdefault("subnav_label", "QuestersBoard")
    b.setdefault("subnav_label_url", gp + "#questersboard" if gp else "")
    for k in ("subnav_lead_label", "subnav_label"):
        if not isinstance(b[k], str) or len(b[k]) > 40:
            raise ConfigError(f"[branding] {k} must be text of up to 40 characters")
    if b["subnav_label_url"] and not re.match(r"^(/|https://)", str(b["subnav_label_url"])):
        raise ConfigError("[branding] subnav_label_url must start with / or https://")
    if not isinstance(b["subnav_lead"], list) or len(b["subnav_lead"]) > 6 or any(
            not isinstance(n, dict) or not str(n.get("label", "")) or
            not re.match(r"^(/|https://)", str(n.get("url", ""))) for n in b["subnav_lead"]):
        raise ConfigError("[branding] subnav_lead must be a list of up to 6 { label, url } with / or https:// urls")
    b.setdefault("footer_nav", [])
    if not isinstance(b["footer_nav"], list) or any(
            not isinstance(n, dict) or not str(n.get("label", "")) or
            not re.match(r"^(/|https://)", str(n.get("url", ""))) for n in b["footer_nav"]):
        raise ConfigError("[branding] footer_nav must be a list of { label, url } with / or https:// urls")

    db = r["database"]
    db.setdefault("admin_url", "")
    for k in ("app_url", "worker_url", "admin_url"):
        if k == "admin_url" and not db[k]:
            continue
        u = _req(db, k, str, "database")
        if not u.startswith("postgresql"):
            raise ConfigError(f"[database] {k} must be a postgresql URL")
        if re.search(r"://[^/@]*:[^/@]*@", u):
            raise ConfigError(f"[database] {k} must not contain a password; use credentials")

    bz = r["blizzard"]
    _req(bz, "enabled", bool, "blizzard")
    _positive(bz.get("max_retries", 3), "[blizzard] max_retries", 0)
    _positive(bz.get("battlenet_import_minutes", 60), "[blizzard] battlenet_import_minutes")
    ttl = _positive(bz.get("data_ttl_days", 30), "[blizzard] data_ttl_days")
    if ttl > 30:
        raise ConfigError("[blizzard] data_ttl_days can't be more than 30 (Blizzard's API terms)")
    if bz.get("battlenet_import_minutes", 60) > ttl * 24 * 60:
        raise ConfigError("[blizzard] battlenet_import_minutes must fit within data_ttl_days")
    _positive(bz.get("battlenet_profile_lookups", 25), "[blizzard] battlenet_profile_lookups", 0)
    if not isinstance(bz.get("detail_icons", True), bool):
        raise ConfigError("[blizzard] detail_icons must be true or false")
    _positive(bz.get("icon_lookups", 120), "[blizzard] icon_lookups", 0)
    if _positive(bz.get("recipe_lookups", 80), "[blizzard] recipe_lookups", 0) > 500:
        raise ConfigError("[blizzard] recipe_lookups must be 500 or less")
    if _positive(bz.get("refresh_all_max", 10), "[blizzard] refresh_all_max") > 50:
        raise ConfigError("[blizzard] refresh_all_max must be 50 or less")
    if bz.get("picker_default_share", "guild") not in ("private", "guild", "members"):
        raise ConfigError('[blizzard] picker_default_share must be "private", "guild" or "members"')
    rl = bz.get("region_labels", {})
    if not isinstance(rl, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                           for k, v in rl.items()):
        raise ConfigError("[blizzard] region_labels must map region keys to names, e.g. { us = \"Americas\" }")
    _positive(bz.get("icon_cache_entries", 2000), "[blizzard] icon_cache_entries", 0)
    if _positive(bz.get("known_recipes_max", 500), "[blizzard] known_recipes_max", 0) > 2000:
        raise ConfigError("[blizzard] known_recipes_max must be 2000 or less")
    pause = bz.get("operator_refresh_pause_seconds", 1)
    if isinstance(pause, bool) or not isinstance(pause, (int, float)) or not 0 <= pause <= 10:
        raise ConfigError("[blizzard] operator_refresh_pause_seconds must be a number from 0 to 10")
    ich = bz.get("icon_cache_hours", 12)
    if not isinstance(ich, (int, float)) or isinstance(ich, bool) or not 0 <= ich <= 24:
        raise ConfigError("[blizzard] icon_cache_hours must be between 0 and 24")
    hosts = bz.get("render_image_hosts", ["render.worldofwarcraft.com"])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and re.fullmatch(r"[a-z0-9.-]{3,100}", h)
                                              for h in hosts):
        raise ConfigError("[blizzard] render_image_hosts must be a list of host names")

    # editions
    eds = r.get("editions")
    if not isinstance(eds, list) or not eds:
        raise ConfigError("[[editions]] at least one edition is required")
    parsed: dict[str, Edition] = {}
    for i, e in enumerate(eds):
        where = f"editions[{i}]"
        key = _req(e, "key", str, where)
        if not KEY.match(key) or key in parsed:
            raise ConfigError(f"{where} key must be unique lowercase")
        uses_realms = _req(e, "uses_realms", bool, where)
        rulesets = tuple(e.get("rulesets", []))
        if any(not KEY.match(str(x)) for x in rulesets):
            raise ConfigError(f"{where} rulesets must be lowercase keys like rp_pvp")
        if not uses_realms and not rulesets:
            raise ConfigError(f"{where} realmless editions need rulesets")
        regions = tuple(e.get("regions", []))
        if not regions or any(x not in ("us", "eu", "kr", "tw") for x in regions):
            raise ConfigError(f"{where} regions must be some of us, eu, kr, tw")
        status = dict(e.get("api_status", {}))
        for cap in API_CAPS:
            status.setdefault(cap, "unsupported")
            if status[cap] not in API_STATUSES:
                raise ConfigError(f"{where} api_status.{cap} must be one of {API_STATUSES}")
        for kind in ("static_namespace", "dynamic_namespace", "profile_namespace"):
            ns = e.get(kind, "")
            if ns and ("{region}" not in ns or not re.match(r"^[a-z0-9{}._-]+$", ns)):
                raise ConfigError(f"{where} {kind} must be a template containing {{region}}")
            if ns.startswith(("static-classic-ptr", "profile-classic-ptr")) or "-ptr" in ns:
                raise ConfigError(f"{where} {kind}: PTR namespaces are not allowed in production config")
        parsed[key] = Edition(
            key=key, name=_req(e, "name", str, where), uses_realms=uses_realms, rulesets=rulesets,
            default_ruleset=e.get("default_ruleset", rulesets[0] if rulesets else ""),
            regions=regions, default_region=e.get("default_region", regions[0]),
            factions=tuple(e.get("factions", ["horde", "alliance"])),
            default_faction=e.get("default_faction", "horde"),
            max_level=_positive(e.get("max_level", 60), f"{where} max_level"),
            catalog=e.get("catalog", ""),
            static_namespace=e.get("static_namespace", ""), dynamic_namespace=e.get("dynamic_namespace", ""),
            profile_namespace=e.get("profile_namespace", ""),
            ruleset_realm_slugs=dict(e.get("ruleset_realm_slugs", {})), api_status=status,
            ruleset_labels={str(k): str(v)[:40] for k, v in dict(e.get("ruleset_labels", {})).items()},
            difficulties=_difficulties(e.get("difficulties", ["Normal"]), where),
            ruleset_api_ids=_ruleset_ids(e.get("ruleset_api_ids", {}), rulesets, where),
            item_link_template=_link_template(e.get("item_link_template", ""), f"{where} item_link_template"),
            spell_link_template=_link_template(e.get("spell_link_template", ""), f"{where} spell_link_template"),
            gear_bottom_slots=_gear_slots(e.get("gear_bottom_slots", ["MAIN_HAND", "OFF_HAND", "RANGED"]),
                                          f"{where} gear_bottom_slots"),
            talent_tree_columns=_tree_columns(e.get("talent_tree_columns", 0), f"{where} talent_tree_columns"))
    r["_editions"] = parsed

    presets = []
    for i, p in enumerate(r.get("role_presets", [])):
        k = _req(p, "key", str, f"role_presets[{i}]")
        if not KEY.match(k):
            raise ConfigError(f"role_presets[{i}] key must be lowercase")
        presets.append(RolePreset(k, _req(p, "label", str, f"role_presets[{i}]")))
    r["_role_presets"] = tuple(presets)

    acts = []
    for i, a in enumerate(r.get("activities", [])):
        where = f"activities[{i}]"
        acts.append(Activity(
            key=_req(a, "key", str, where), type=_req(a, "type", str, where), label=_req(a, "label", str, where),
            min_minutes=_positive(a.get("min_minutes", 30), f"{where} min_minutes"),
            min_size=_positive(a.get("min_size", 1), f"{where} min_size"),
            max_size=_positive(a.get("max_size", 40), f"{where} max_size"),
            roles={k: _positive(v, f"{where} roles.{k}") for k, v in a.get("roles", {}).items()},
            tags=tuple(a.get("tags", []))))
    r["_activities"] = tuple(acts)
    _item_links(r)
    return r


def _item_links(r: dict) -> None:
    """[item_links]: item cards for names in [brackets] in chosen channels, posted through a webhook.
    Off by default; turning it on needs the Message Content intent and a restart."""
    il = r.setdefault("item_links", {})
    for k, dflt in (("enabled", False), ("say_not_found", True), ("ignore_bots", False)):
        if not isinstance(il.setdefault(k, dflt), bool):
            raise ConfigError(f"[item_links] {k} must be true or false")
    il["watch_channels"] = _snowflakes(il.setdefault("watch_channels", []), "[item_links] watch_channels")
    il["post_channel"] = _snowflake(il.setdefault("post_channel", ""), "[item_links] post_channel")
    eds = r["_editions"]
    il.setdefault("edition", next(iter(eds), ""))
    if il["edition"] not in eds:
        raise ConfigError("[item_links] edition must be one of the [[editions]] keys: " + ", ".join(eds))
    ed = eds[il["edition"]]
    il.setdefault("region", ed.default_region)
    if il["region"] not in ed.regions:
        raise ConfigError(f"[item_links] region must be one of {', '.join(ed.regions)} for {ed.name}")
    for k, dflt, lo, hi in (("max_per_message", 5, 1, 10), ("cooldown_minutes", 10, 0, 1440),
                            ("search_pages", 10, 1, 25)):
        _positive(il.setdefault(k, dflt), f"[item_links] {k}", lo)
        if il[k] > hi:
            raise ConfigError(f"[item_links] {k} must be {lo} to {hi}")
    name = il.setdefault("webhook_name", "QuestersBoard Items")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or \
            any(w in name.lower() for w in ("discord", "clyde")):
        raise ConfigError("[item_links] webhook_name must be 1 to 80 characters and can't contain "
                          "\"discord\" or \"clyde\" (Discord refuses those)")
    av = il.setdefault("avatar_url", "")
    if not isinstance(av, str) or (av and (not av.startswith("https://") or len(av) > 300)):
        raise ConfigError("[item_links] avatar_url must be empty (the bot's own picture) or an https:// address")
    fb = il.setdefault("fallbacks", [k for k in ("classic_era",) if k in eds and k != il["edition"]])
    if not isinstance(fb, list) or len(fb) > 4 or len(set(fb)) != len(fb) or il["edition"] in fb or \
            any(k not in eds for k in fb):
        raise ConfigError("[item_links] fallbacks must list up to 4 other [[editions]] keys, searched in order "
                          "when the main edition has no item of that name")
    labels = {"forever": "WoW: Forever", "classic_era": "WoW Classic Era", "retail": "WoW",
              **il.setdefault("source_labels", {})}
    if not all(isinstance(v, str) and 1 <= len(v) <= 40 for v in labels.values()):
        raise ConfigError("[item_links] source_labels must map edition keys to short names (1 to 40 characters)")
    il["source_labels"] = labels
    for k, dflt in (("footer_text", "Source: Blizzard · {source} API"),
                    ("footer_fallback_text", "Source: Blizzard · {source} API (not in {main} yet; may differ)")):
        v = il.setdefault(k, dflt)
        if not isinstance(v, str) or "{source}" not in v or not 8 <= len(v) <= 200:
            raise ConfigError(f"[item_links] {k} must be 8 to 200 characters and contain {{source}}")
    nf = il.setdefault("not_found_text", "No item called **{name}** found in Blizzard's public game data.")
    if not isinstance(nf, str) or "{name}" not in nf or not 8 <= len(nf) <= 200:
        raise ConfigError("[item_links] not_found_text must be 8 to 200 characters and contain {name}")
    profs = {"Alchemy": ["Recipe"], "Blacksmithing": ["Plans"], "Cooking": ["Recipe"], "Enchanting": ["Formula"],
             "Engineering": ["Schematic"], "First Aid": ["Manual"], "Leatherworking": ["Pattern"],
             "Tailoring": ["Pattern"], "Jewelcrafting": ["Design"], "Inscription": ["Technique"], "Mining": [],
             **il.setdefault("professions", {})}
    if not all(isinstance(k, str) and 2 <= len(k) <= 30 and ":" not in k and isinstance(v, list) and len(v) <= 4
               and all(isinstance(x, str) and 2 <= len(x) <= 30 for x in v) for k, v in profs.items()):
        raise ConfigError("[item_links] professions must map profession names (as in the game's links, e.g. "
                          "\"Leatherworking\") to lists of recipe-item prefixes (e.g. [\"Pattern\"])")
    il["professions"] = profs
    ct = il.setdefault("crafted_text", "Crafted with {profession}")
    if not isinstance(ct, str) or "{profession}" not in ct or not 8 <= len(ct) <= 100:
        raise ConfigError("[item_links] crafted_text must be 8 to 100 characters and contain {profession}")
    if il["enabled"]:
        if not il["watch_channels"]:
            raise ConfigError("[item_links] watch_channels must list at least one channel when item links are on")
        if not r["blizzard"].get("enabled"):
            raise ConfigError("[item_links] needs [blizzard] enabled = true: item details come from Blizzard")
        for k in [il["edition"], *fb]:
            e = eds[k]
            if not e.namespace("static", il["region"] if il["region"] in e.regions else e.default_region):
                raise ConfigError(f"[item_links] {e.name} has no static_namespace, so items can't be looked up")
    unknown = set(il) - {"enabled", "watch_channels", "post_channel", "edition", "region", "max_per_message",
                         "cooldown_minutes", "search_pages", "say_not_found", "ignore_bots", "webhook_name",
                         "avatar_url", "not_found_text", "fallbacks", "source_labels",
                         "footer_text", "footer_fallback_text", "professions", "crafted_text"}
    if unknown:
        raise ConfigError(f"[item_links] unknown setting(s): {', '.join(sorted(unknown))}")


def _link_template(v: Any, where: str) -> str:
    if v in ("", None):
        return ""
    if not isinstance(v, str) or not re.fullmatch(r"https://[a-z0-9.-]+/[A-Za-z0-9/_.=?&-]*\{id\}[A-Za-z0-9/_.=?&-]*", v) \
            or len(v) > 200:
        raise ConfigError(f"{where} must be an https:// address containing {{id}}")
    return v


def _tree_columns(v: Any, where: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not (v == 0 or 2 <= v <= 8):
        raise ConfigError(f"{where} must be 0 (plain talent list) or 2 to 8 columns")
    return v


def _gear_slots(v: Any, where: str) -> tuple[str, ...]:
    if not isinstance(v, list) or not 1 <= len(v) <= 6 or len(set(v)) != len(v) or \
            not all(isinstance(x, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,23}", x) for x in v):
        raise ConfigError(f"{where} must list 1 to 6 different Blizzard slot types, e.g. [\"MAIN_HAND\", \"OFF_HAND\"]")
    return tuple(v)


def _difficulties(v: Any, where: str) -> tuple[str, ...]:
    # An empty list means the edition has a single difficulty (WoW: Forever): the form hides the choice.
    if not isinstance(v, list) or len(v) > 25 or any(not isinstance(x, str) or not 1 <= len(x) <= 32 for x in v):
        raise ConfigError(f"{where} difficulties must be a list of up to 25 names (1-32 characters each), "
                          "or [] for none")
    if len({x.casefold() for x in v}) != len(v):
        raise ConfigError(f"{where} difficulties has a name twice")
    return tuple(v)


def _hashes(raw: dict[str, Any]) -> dict[str, str]:
    out = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        out[k] = hashlib.sha256(repr(v).encode()).hexdigest()[:16]
    return out


def load(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or getenv("CONFIG", DEFAULT_PATH))
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {p}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"configuration is not valid TOML: {exc}") from None
    raw = validate(data)
    return Config(raw=raw, path=str(p), section_hashes=_hashes(data))


class ConfigHolder:
    """Holds the current Config and re-reads [live] sections when the file changes."""

    def __init__(self, cfg: Config, on_change=None, interval: float = 30.0):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._mtime = self._stat()
        self._on_change = on_change
        self.interval = interval

    def _stat(self) -> float:
        try:
            return os.stat(self._cfg.path).st_mtime if self._cfg.path else 0.0
        except OSError:
            return 0.0

    @property
    def cfg(self) -> Config:
        return self._cfg

    def check(self) -> list[str]:
        """Return the changed live sections (applied) — restart-only changes are logged, not applied."""
        m = self._stat()
        if m == self._mtime:
            return []
        self._mtime = m
        try:
            new = load(self._cfg.path)
        except ConfigError as exc:
            log.error("config change ignored: %s", exc)
            return []
        changed = [k for k, h in new.section_hashes.items() if self._cfg.section_hashes.get(k) != h]
        live = [k for k in changed if k in LIVE_SECTIONS]
        restart = [k for k in changed if k not in LIVE_SECTIONS]
        if restart:
            log.warning("config sections changed that need a restart: %s", ", ".join(restart))
        if live:
            with self._lock:
                merged = dict(self._cfg.raw)
                for k in live:
                    merged[k] = new.raw[k]
                hashes = dict(self._cfg.section_hashes)
                hashes.update({k: new.section_hashes[k] for k in live})
                self._cfg = Config(raw=merged, path=self._cfg.path, section_hashes=hashes)
            log.info("config live sections reloaded: %s", ", ".join(live))
            if self._on_change:
                self._on_change(live, self._cfg)
        return live

    def run_forever(self):  # pragma: no cover - thread loop
        while True:
            time.sleep(self.interval)
            self.check()
