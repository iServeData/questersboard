"""Who is acting, from where. Built by each entry point from authenticated data only."""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCES = ("web", "slash", "dm", "button", "worker", "cli")


@dataclass(frozen=True)
class Actor:
    discord_id: str
    source: str
    role_ids: frozenset[str] = field(default_factory=frozenset)
    in_guild: bool = True
    channel_id: str | None = None      # guild channel for slash/button; None for web/dm
    member_id: int | None = None       # filled by services.ensure_member
    display_name: str = ""
    username: str = ""                 # unique Discord username, when the login/bot provides it

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError(f"unknown actor source {self.source}")

    def with_member(self, member_id: int) -> "Actor":
        return Actor(self.discord_id, self.source, self.role_ids, self.in_guild, self.channel_id,
                     member_id, self.display_name)


SYSTEM = Actor(discord_id="0", source="worker")
