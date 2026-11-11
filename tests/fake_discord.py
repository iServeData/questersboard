"""FIXTURE: an in-memory stand-in for Discord's REST API. Not the real service."""
from __future__ import annotations

import itertools
import json

import httpx


class FakeDiscord:
    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.dms: list[tuple[str, str]] = []
        self.dm_flags: list[int] = []
        self.directory: list[dict] = []          # FIXTURE guild members for /members/search
        self.calls: list[tuple[str, str]] = []
        self.fail_next: list[int] = []
        self.forbid_channels: set[str] = set()
        self.ids = itertools.count(5000)
        self.threads: dict[str, dict] = {}       # FIXTURE thread id -> {name, type, parent, members, closed}
        self.voice: dict[str, dict] = {}         # FIXTURE voice channel id -> {name, parent, limit, overwrites}
        self.voice_states: dict[str, str] = {}   # FIXTURE user id -> voice channel they are in
        self.member_roles: dict[str, list] = {}  # FIXTURE user id -> role ids
        self.disconnects: list[str] = []
        self.app_emojis: dict[str, str] = {}     # FIXTURE the application's own emojis, name -> id
        self.emoji_uploads: list[tuple[str, str]] = []
        self.bot_id = "b" + "1" * 17               # FIXTURE bot user id (not a real snowflake)
        self.bot_role = "r" + "2" * 17             # FIXTURE the bot's own role
        self.member_roles[self.bot_id] = [self.bot_role]
        self.other_role = "r" + "3" * 17           # FIXTURE a role the category shows its channels to
        # FIXTURE category permissions, as set in Discord: the bot's role may manage channels there
        # (category only), and another role can see them. New bare channels copy these.
        self.category_overwrites = [
            {"id": self.bot_role, "type": 0, "allow": str((1 << 4) | (1 << 28) | (1 << 24) | (1 << 10) | (1 << 20)
                                                         | (1 << 21) | (1 << 9) | (1 << 25) | (1 << 11) | (1 << 16)),
             "deny": "0"},
            {"id": self.other_role, "type": 0, "allow": str(1 << 10), "deny": "0"}]
        self.bot_guild_perms = (1 << 10) | (1 << 20) | (1 << 11) | (1 << 16)   # FIXTURE server-wide: basics only

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.replace("/api/v10", "")
        self.calls.append((req.method, path))
        if self.fail_next:
            code = self.fail_next.pop(0)
            return httpx.Response(code, json={"retry_after": 0.01, "message": "fixture"})
        parts = path.strip("/").split("/")
        body = json.loads(req.content or b"{}")
        if parts[0] == "applications" and parts[2:] == ["emojis"]:
            if req.method == "POST":
                assert body["image"].startswith("data:image/png;base64,") and body["name"] not in self.app_emojis
                eid = "e" + str(next(self.ids))
                self.app_emojis[body["name"]] = eid
                self.emoji_uploads.append((parts[1], body["name"]))
                return httpx.Response(201, json={"id": eid, "name": body["name"]})
            return httpx.Response(200, json={"items": [{"id": i, "name": n} for n, i in self.app_emojis.items()]})
        if req.method == "GET" and parts == ["users", "@me"]:
            return httpx.Response(200, json={"id": self.bot_id})
        if parts[0] == "guilds" and parts[2:] == ["channels"] and req.method == "POST":
            given = body.get("permission_overwrites")
            if given:
                # Discord: only permissions the bot has server-wide, and Manage Permissions only for admins
                for o in given:
                    bits = int(o.get("allow") or 0) | int(o.get("deny") or 0)
                    if bits & (1 << 28) or bits & ~self.bot_guild_perms:
                        return httpx.Response(403, json={"code": 50013})
            ows = given if given else [dict(o) for o in self.category_overwrites]
            vid = "v" + str(next(self.ids))
            self.voice[vid] = {"name": body["name"], "type": body["type"], "parent": body["parent_id"],
                               "limit": body["user_limit"],
                               "overwrites": {o["id"]: (o["type"], int(o["allow"]), int(o["deny"])) for o in ows}}
            return httpx.Response(201, json={"id": vid, "permission_overwrites": ows})
        if parts[0] == "guilds" and parts[2:3] == ["voice-states"]:
            ch = self.voice_states.get(parts[3])
            return httpx.Response(200, json={"channel_id": ch}) if ch else httpx.Response(404, json={"code": 10065})
        if parts[0] == "guilds" and parts[2:3] == ["members"] and req.method == "PATCH":
            if body.get("channel_id", "x") is None:
                self.voice_states.pop(parts[3], None)
                self.disconnects.append(parts[3])
            return httpx.Response(200, json={})
        if parts[0] == "channels" and parts[1] in self.voice:
            v = self.voice[parts[1]]
            if parts[2:3] == ["permissions"]:
                mine = [v["overwrites"].get(r) for r in [self.bot_id, *self.member_roles[self.bot_id]]]
                if not any(o and o[1] & (1 << 28) for o in mine):
                    return httpx.Response(403, json={"code": 50013})     # no Manage Permissions here
                if req.method == "PUT":
                    v["overwrites"][parts[3]] = (body["type"], int(body["allow"]), int(body["deny"]))
                else:
                    v["overwrites"].pop(parts[3], None)
                return httpx.Response(204)
            if req.method == "GET" and len(parts) == 2:
                return httpx.Response(200, json={"id": parts[1], "permission_overwrites": [
                    {"id": k, "type": t, "allow": str(a), "deny": str(d)} for k, (t, a, d) in v["overwrites"].items()]})
            if req.method == "DELETE" and len(parts) == 2:
                del self.voice[parts[1]]
                return httpx.Response(200, json={"id": parts[1]})
        if parts[0] == "channels" and parts[1].startswith("v") and parts[1][1:].isdigit():
            return httpx.Response(404, json={"code": 10003})     # a voice channel that was deleted
        if req.method == "POST" and parts == ["users", "@me", "channels"]:
            return httpx.Response(200, json={"id": "dm-" + body["recipient_id"]})
        if parts[0] == "channels":
            ch = parts[1]
            if ch in self.forbid_channels:
                return httpx.Response(403, json={"code": 50013})
            if req.method == "POST" and (parts[2:3] == ["threads"] or parts[4:5] == ["threads"]):
                tid = "t" + str(next(self.ids))
                self.threads[tid] = {"name": body["name"], "type": body.get("type", 11), "parent": ch,
                                     "from_message": parts[3] if len(parts) > 4 else None,
                                     "members": set(), "closed": False, "posts": []}
                return httpx.Response(200, json={"id": tid})
            if parts[2:3] == ["thread-members"] and ch in self.threads:
                (self.threads[ch]["members"].add if req.method == "PUT"
                 else self.threads[ch]["members"].discard)(parts[3])
                return httpx.Response(204)
            if req.method == "PATCH" and len(parts) == 2:
                if ch not in self.threads:
                    return httpx.Response(404, json={"code": 10003})
                self.threads[ch]["closed"] = bool(body.get("archived") and body.get("locked"))
                return httpx.Response(200, json={"id": ch})
            if req.method == "POST" and ch in self.threads:
                assert body.get("allowed_mentions") == {"parse": []}
                self.threads[ch]["posts"].append(body["content"])
                return httpx.Response(200, json={"id": str(next(self.ids))})
            if req.method == "POST":
                assert body.get("allowed_mentions") == {"parse": []}
                if ch.startswith("dm-"):
                    self.dms.append((ch[3:], body["content"]))
                    self.dm_flags.append(body.get("flags", 0))
                    return httpx.Response(200, json={"id": str(next(self.ids))})
                mid = str(next(self.ids))
                self.messages[mid] = body
                return httpx.Response(200, json={"id": mid})
            if req.method == "PATCH":
                mid = parts[3]
                if mid not in self.messages:
                    return httpx.Response(404, json={"code": 10008})
                self.messages[mid] = body
                return httpx.Response(200, json={"id": mid})
        if parts[0] == "guilds" and parts[2:] == ["members", "search"]:
            q = req.url.params.get("query", "").lower()
            return httpx.Response(200, json=[{"user": u} for u in self.directory
                                             if u["username"].lower().startswith(q)])
        if parts[0] == "guilds" and parts[2:3] == ["members"] and req.method == "PUT":
            assert body.get("access_token")
            if parts[3] in self.member_roles:
                return httpx.Response(204)
            self.member_roles[parts[3]] = []
            self.added = getattr(self, "added", []) + [parts[3]]
            return httpx.Response(201, json={"user": {"id": parts[3]}, "roles": []})
        if parts[0] == "guilds" and parts[2] == "members":
            return httpx.Response(200, json={"roles": self.member_roles.get(parts[3], []), "nick": "Nick",
                                             "user": {"username": "u"}})
        return httpx.Response(404, json={})

    def client(self):
        from questersboard.integrations.discord_rest import DiscordRest
        return DiscordRest(http=httpx.Client(transport=httpx.MockTransport(self.handler)), token="fixture-token")
