"""Minimal Discord REST client used by the worker (posts, edits, DMs) and web (role lookups).

Errors are classified so the outbox can decide: retry later (RateLimited, Transient),
repost (Gone: message/channel deleted), or stop and alert (Forbidden: missing permission).
Allowed mentions are always empty: WoWQuester never pings @everyone/roles/users by text.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
from urllib.parse import quote

from ..secrets import get_secret

log = logging.getLogger("wowquester.discord")
API = "https://discord.com/api/v10"
NO_PINGS = {"parse": []}


class DiscordError(Exception):
    pass


class RateLimited(DiscordError):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited for {retry_after:.1f}s")
        self.retry_after = retry_after


class Transient(DiscordError):
    pass


class MemberInfo(tuple):
    """(in_guild, role_ids, display_name) that unpacks as before, with the server join time on .joined_at."""
    def __new__(cls, in_guild: bool, roles: frozenset, name: str, joined_at: datetime | None = None):
        t = super().__new__(cls, (in_guild, roles, name))
        t.joined_at = joined_at
        return t


def _when(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


class Gone(DiscordError):
    pass


class Forbidden(DiscordError):
    pass


class DiscordRest:
    def __init__(self, http: httpx.Client | None = None, token: str | None = None, base: str = API):
        tok = token or get_secret("discord-bot-token").reveal()
        self.base = base
        self.http = http or httpx.Client(timeout=15)
        self.headers = {"Authorization": f"Bot {tok}", "User-Agent": "wowquester (self-hosted, 0.1)"}

    def _call(self, method: str, path: str, json: dict | None = None) -> dict | None:
        try:
            r = self.http.request(method, self.base + path, json=json, headers=self.headers)
        except httpx.HTTPError as exc:
            raise Transient(type(exc).__name__) from None
        if r.status_code == 429:
            try:
                ra = float(r.json().get("retry_after", 5))
            except Exception:
                ra = float(r.headers.get("Retry-After", 5))
            raise RateLimited(ra)
        if r.status_code in (404,):
            raise Gone(path.split("/")[1])
        if r.status_code in (401, 403):
            code = ""
            try:
                code = str(r.json().get("code", ""))
            except Exception:
                pass
            raise Forbidden(f"Discord refused ({r.status_code} {code}). Check the bot's channel permissions.")
        if r.status_code >= 500:
            raise Transient(f"http {r.status_code}")
        if r.status_code >= 400:
            raise DiscordError(f"http {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    def post(self, channel_id: str, payload: dict) -> str:
        body = dict(payload, allowed_mentions=NO_PINGS)
        return self._call("POST", f"/channels/{channel_id}/messages", body)["id"]

    def edit(self, channel_id: str, message_id: str, payload: dict) -> None:
        body = dict(payload, allowed_mentions=NO_PINGS)
        self._call("PATCH", f"/channels/{channel_id}/messages/{message_id}", body)

    def delete(self, channel_id: str, message_id: str) -> None:
        try:
            self._call("DELETE", f"/channels/{channel_id}/messages/{message_id}")
        except Gone:
            pass

    # Event chat threads. UNVERIFIED live: needs Create Public Threads (or Create Private Threads),
    # Send Messages in Threads and Manage Threads on the events channel.
    def start_thread(self, channel_id: str, message_id: str | None, name: str, private: bool) -> str:
        """A public thread attached to the event post, or a private one (invite-only) in the same channel."""
        if private or not message_id:
            body = {"name": name[:100], "type": 12 if private else 11, "invitable": False,
                    "auto_archive_duration": 10080}
            return self._call("POST", f"/channels/{channel_id}/threads", body)["id"]
        return self._call("POST", f"/channels/{channel_id}/messages/{message_id}/threads",
                          {"name": name[:100], "auto_archive_duration": 10080})["id"]

    def add_thread_member(self, thread_id: str, user_id: str) -> None:
        self._call("PUT", f"/channels/{thread_id}/thread-members/{user_id}")

    def remove_thread_member(self, thread_id: str, user_id: str) -> None:
        try:
            self._call("DELETE", f"/channels/{thread_id}/thread-members/{user_id}")
        except Gone:
            pass

    def close_thread(self, thread_id: str) -> None:
        """Lock and archive: people can still read it, nobody can post."""
        try:
            self._call("PATCH", f"/channels/{thread_id}", {"archived": True, "locked": True})
        except Gone:
            pass

    # Event voice channels ([voice], FU-1). UNVERIFIED live: needs Manage Channels, Manage Permissions,
    # Move Members, View Channel, Connect, Speak, Video, Use Voice Activity, Send Messages and Read Message
    # History for the bot on the voice category (category only; no server-wide Manage Roles). Channels are
    # created bare in the category and locked afterwards: see voice.lock_ops.
    def me(self) -> str:
        if not getattr(self, "_me", None):
            self._me = str(self._call("GET", "/users/@me")["id"])
        return self._me

    def create_voice_channel(self, guild_id: str, category_id: str, name: str, user_limit: int) -> dict:
        """Created without permission_overwrites, so Discord copies the category's (the bot's category-only
        Manage Permissions comes with them). The caller locks it straight after (voice.lock_ops)."""
        body = {"name": name[:100], "type": 2, "parent_id": category_id, "user_limit": user_limit}
        return self._call("POST", f"/guilds/{guild_id}/channels", body)

    def channel(self, channel_id: str) -> dict:
        return self._call("GET", f"/channels/{channel_id}")

    def set_overwrite(self, channel_id: str, target_id: str, kind: int, allow: int, deny: int = 0) -> None:
        """kind 0 = role, 1 = member."""
        self._call("PUT", f"/channels/{channel_id}/permissions/{target_id}",
                   {"type": kind, "allow": str(allow), "deny": str(deny)})

    def delete_overwrite(self, channel_id: str, target_id: str) -> None:
        try:
            self._call("DELETE", f"/channels/{channel_id}/permissions/{target_id}")
        except Gone:
            pass

    def delete_channel(self, channel_id: str) -> None:
        try:
            self._call("DELETE", f"/channels/{channel_id}")
        except Gone:
            pass

    def voice_channel_of(self, guild_id: str, user_id: str) -> str | None:
        """The voice channel a member is in right now, or None."""
        try:
            v = self._call("GET", f"/guilds/{guild_id}/voice-states/{user_id}")
        except Gone:
            return None
        return str(v["channel_id"]) if v and v.get("channel_id") else None

    def add_guild_member(self, guild_id: str, user_id: str, access_token: str) -> bool:
        """Guest voice links: add someone who signed in (scope guilds.join) to the server. True if added,
        False if they were already in it. Needs Create Invite for the bot. UNVERIFIED live."""
        r = self._call("PUT", f"/guilds/{guild_id}/members/{user_id}", {"access_token": access_token})
        return bool(r)

    def disconnect_voice(self, guild_id: str, user_id: str) -> None:
        try:
            self._call("PATCH", f"/guilds/{guild_id}/members/{user_id}", {"channel_id": None})
        except Gone:
            pass

    def dm(self, user_id: str, payload: dict) -> str:
        ch = self._call("POST", "/users/@me/channels", {"recipient_id": user_id})
        return self.post(ch["id"], payload)

    def member(self, guild_id: str, user_id: str) -> "MemberInfo":
        """(in_guild, role_ids, display_name), plus .joined_at. Used to re-check roles for web and DM actions."""
        try:
            m = self._call("GET", f"/guilds/{guild_id}/members/{user_id}")
        except Gone:
            return MemberInfo(False, frozenset(), "")
        name = m.get("nick") or (m.get("user") or {}).get("global_name") or (m.get("user") or {}).get("username", "")
        return MemberInfo(True, frozenset(m.get("roles", [])), name, _when(m.get("joined_at")))

    def search_members(self, guild_id: str, query: str, limit: int = 5) -> list[dict]:
        """Guild member search by name prefix. UNVERIFIED live: Discord may require the Server Members
        intent for this endpoint; when it fails the caller treats the name as not found."""
        rows = self._call("GET", f"/guilds/{guild_id}/members/search?query={quote(query[:32])}&limit={limit}")
        return [{"id": str(r["user"]["id"]), "username": r["user"].get("username", ""),
                 "global_name": r["user"].get("global_name") or ""} for r in rows or [] if r.get("user")]

    def put_commands(self, application_id: str, guild_id: str, commands: list[dict]) -> list[dict]:
        """Registers slash commands. Only called by `wowquester-admin sync-commands --yes` (approval required)."""
        return self._call("PUT", f"/applications/{application_id}/guilds/{guild_id}/commands", commands)
