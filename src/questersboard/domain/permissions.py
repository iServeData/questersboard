"""One permission policy for every entry point (web, slash command, DM, button, worker).

Capabilities map to Discord role IDs in config [roles]. Roles are re-read for each
privileged action by the entry point (interaction payload, or bot REST lookup for web/DM),
never trusted from a cached browser session.
"""

from __future__ import annotations

from datetime import timedelta

from ..config import Config
from .actor import Actor
from .errors import PermissionDenied
from .timeutil import now


def has(cfg: Config, actor: Actor, capability: str) -> bool:
    if actor.source == "worker":
        return True
    ids = cfg.role_ids(capability)
    if ids & actor.role_ids:
        return True
    return capability == "event_creator" and cfg["publishing"]["open_to_all"] and not on_probation(cfg, actor)


def on_probation(cfg: Config, actor: Actor) -> bool:
    """[publishing] open_to_all: members newer to the server than probation_days need a moderator to publish
    their events. If the join date isn't known, treat them as new (the safe side)."""
    if actor.source == "worker" or cfg.role_ids("event_creator") & actor.role_ids:
        return False
    days = cfg["publishing"]["probation_days"]
    if days == 0:
        return False
    if actor.joined_at is None:
        return True
    return now() - actor.joined_at < timedelta(days=days)


def probation_message(cfg: Config) -> str:
    d = cfg["publishing"]["probation_days"]
    return (f"You joined the server less than {d} day{'s' if d != 1 else ''} ago, so your events need a "
            "moderator's approval first. Send your draft for publishing and a moderator will post it.")


def require_member(cfg: Config, actor: Actor) -> None:
    """Base access: in the configured guild, holding a member role if configured, in an allowed place."""
    if actor.source == "worker":
        return
    if not actor.in_guild:
        raise PermissionDenied("You need to be a member of the community Discord server to use QuestersBoard.")
    req = frozenset(cfg["community"].get("member_role_ids", []))
    if req and not (req & actor.role_ids):
        raise PermissionDenied("Your Discord account does not have the member role QuestersBoard requires.")
    if actor.source == "slash":
        allowed = frozenset(cfg["discord"].get("allowed_command_channel_ids", []))
        if allowed and actor.channel_id not in allowed:
            raise PermissionDenied("QuestersBoard commands work in "
                                   + ", ".join(f"<#{c}>" for c in cfg["discord"]["allowed_command_channel_ids"])
                                   + ". Buttons on event posts work everywhere.")
    if actor.source == "dm" and not cfg["discord"].get("dm_enabled", False):
        raise PermissionDenied("QuestersBoard is not available in direct messages.")


def require_publisher(cfg: Config, actor: Actor, what: str = "Publishing events") -> None:
    """Publishing (and recurring series): the event_creator role, or anyone past probation when open_to_all."""
    require_member(cfg, actor)
    if has(cfg, actor, "event_creator"):
        return
    if cfg["publishing"]["open_to_all"]:
        raise PermissionDenied(probation_message(cfg))
    raise PermissionDenied(f"{what} needs the event-creator role. Send your draft for publishing and a "
                           "moderator will post it.")


def require(cfg: Config, actor: Actor, capability: str, message: str | None = None) -> None:
    require_member(cfg, actor)
    if not has(cfg, actor, capability):
        raise PermissionDenied(message or f"This needs the {capability.replace('_', ' ')} role.")


def is_event_manager(cfg: Config, actor: Actor, creator_id: int, cohost_ids: set[int]) -> bool:
    """Creator, an assigned co-host, or a configured event moderator."""
    if actor.source == "worker":
        return True
    return (actor.member_id is not None and (actor.member_id == creator_id or actor.member_id in cohost_ids)) \
        or has(cfg, actor, "event_moderator")


def require_event_manager(cfg: Config, actor: Actor, creator_id: int, cohost_ids: set[int]) -> None:
    require_member(cfg, actor)
    if not is_event_manager(cfg, actor, creator_id, cohost_ids):
        raise PermissionDenied("Only the event creator, a co-host or an event moderator can do that.")


MATRIX = [
    # (action, who)
    ("View public chronicle, feeds, public calendar", "Anyone"),
    ("Add / edit / archive / remove own characters", "Member (own characters only)"),
    ("Create draft, publish, reschedule own event",
     "event_creator role, or any member past probation when [publishing] open_to_all"),
    ("Mark event exclusive", "exclusive_designator role"),
    ("Assign co-hosts", "Event creator or event_moderator"),
    ("Start / end / cancel event", "Creator, assigned co-host, or event_moderator"),
    ("Approve / reject / move signups", "Creator, assigned co-host, or event_moderator"),
    ("Record attendance", "Creator, assigned co-host, or event_moderator"),
    ("Join / leave / reconfirm", "Member, with own character"),
    ("Copy an event into a new draft", "Any member (publishing the copy needs event_creator)"),
    ("Write / edit chronicle draft", "Entry author (event creator), assigned co-author, or chronicle_editor"),
    ("Assign chronicle co-authors", "Entry author or chronicle_editor"),
    ("Submit for review / resubmit after rejection", "Author, co-author, or chronicle_editor"),
    ("Self-publish own entry or revision", "trusted_author who is the author or a co-author"),
    ("Approve / reject / request changes / unpublish", "chronicle_reviewer (not on own entries unless trusted)"),
    ("Clear screenshots for publication", "chronicle_reviewer, or trusted_author on own entry"),
    ("Remove own name from published stories", "The participant themself (applied immediately)"),
    ("Manage catalog, view audit log", "admin role"),
]
