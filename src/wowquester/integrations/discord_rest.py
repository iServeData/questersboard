"""Minimal Discord REST client used by the worker (posts, edits, DMs) and web (role lookups).

Errors are classified so the outbox can decide: retry later (RateLimited, Transient),
repost (Gone: message/channel deleted), or stop and alert (Forbidden: missing permission).
Allowed mentions are always empty: WoWQuester never pings @everyone/roles/users by text.
"""

from __future__ import annotations

import logging

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

    def dm(self, user_id: str, payload: dict) -> str:
        ch = self._call("POST", "/users/@me/channels", {"recipient_id": user_id})
        return self.post(ch["id"], payload)

    def member(self, guild_id: str, user_id: str) -> tuple[bool, frozenset[str], str]:
        """(in_guild, role_ids, display_name). Used to re-check roles for web and DM actions."""
        try:
            m = self._call("GET", f"/guilds/{guild_id}/members/{user_id}")
        except Gone:
            return False, frozenset(), ""
        name = m.get("nick") or (m.get("user") or {}).get("global_name") or (m.get("user") or {}).get("username", "")
        return True, frozenset(m.get("roles", [])), name

    def search_members(self, guild_id: str, query: str, limit: int = 5) -> list[dict]:
        """Guild member search by name prefix. UNVERIFIED live: Discord may require the Server Members
        intent for this endpoint; when it fails the caller treats the name as not found."""
        rows = self._call("GET", f"/guilds/{guild_id}/members/search?query={quote(query[:32])}&limit={limit}")
        return [{"id": str(r["user"]["id"]), "username": r["user"].get("username", ""),
                 "global_name": r["user"].get("global_name") or ""} for r in rows or [] if r.get("user")]

    def put_commands(self, application_id: str, guild_id: str, commands: list[dict]) -> list[dict]:
        """Registers slash commands. Only called by `wowquester-admin sync-commands --yes` (approval required)."""
        return self._call("PUT", f"/applications/{application_id}/guilds/{guild_id}/commands", commands)
