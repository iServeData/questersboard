"""What the account menu and My characters show about a member's Discord and Battle.net state.

QuestersBoard never keeps a Battle.net login: a sync reads the character list once. "Connected" for a
region therefore means "has characters verified through Battle.net there" (or a list from a sync in the
last battlenet_import_minutes). The verified mark lasts until [blizzard] data_ttl_days after the last sync.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..domain.timeutil import now
from ..models import BattlenetLink, Character

DEFAULT_REGION_LABELS = {"us": "Americas & Oceania", "eu": "Europe", "kr": "Korea", "tw": "Taiwan"}


def region_label(cfg: Config, region: str) -> str:
    labels = {**DEFAULT_REGION_LABELS, **(cfg["blizzard"].get("region_labels") or {})}
    return labels.get(region, region.upper())


def sync_enabled(cfg: Config) -> bool:
    bz = cfg["blizzard"]
    return bool(bz.get("enabled") and bz.get("battlenet_link_enabled"))


def sync_regions(cfg: Config) -> list[str]:
    """Regions where at least one edition can be read with a Battle.net login, in config order."""
    if not sync_enabled(cfg):
        return []
    out: list[str] = []
    for ed in cfg.editions.values():
        if ed.capability("ownership") == "unsupported" or not ed.profile_namespace:
            continue
        for rg in ed.regions:
            if rg not in out:
                out.append(rg)
    return out


def ago(when: datetime | None) -> str:
    """'just now', '5 minutes ago', '2 days ago'."""
    if when is None:
        return ""
    secs = max(0, int((now() - when).total_seconds()))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size:
            n = secs // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


def status(s: Session, cfg: Config, member_id: int | None) -> dict:
    """{"enabled", "chars", "regions": [{key, label, connected, verified, synced, synced_ago}], "connected"}."""
    out = {"enabled": sync_enabled(cfg), "chars": 0, "regions": [], "connected": False, "verified": 0}
    if not member_id:
        return out
    live = (Character.member_id == member_id, Character.archived_at.is_(None))
    out["chars"] = s.scalar(select(func.count()).select_from(Character).where(*live)) or 0
    regions = sync_regions(cfg)
    if not regions:
        return out
    ver = {rg: (n, last) for rg, n, last in s.execute(
        select(Character.region, func.count(), func.max(Character.api_refreshed_at))
        .where(*live, Character.verification == "ownership_verified").group_by(Character.region))}
    links = {rg: at for rg, at in s.execute(select(BattlenetLink.region, BattlenetLink.linked_at)
                                            .where(BattlenetLink.member_id == member_id))}
    for rg in regions:
        n, last = ver.get(rg, (0, None))
        synced = max((t for t in (last, links.get(rg)) if t is not None), default=None)
        connected = bool(n) or rg in links
        out["regions"].append({"key": rg, "label": region_label(cfg, rg), "connected": connected,
                               "verified": n, "synced": synced, "synced_ago": ago(synced)})
        out["verified"] += n
    out["connected"] = any(r["connected"] for r in out["regions"])
    return out
