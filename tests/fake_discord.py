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

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.replace("/api/v10", "")
        self.calls.append((req.method, path))
        if self.fail_next:
            code = self.fail_next.pop(0)
            return httpx.Response(code, json={"retry_after": 0.01, "message": "fixture"})
        parts = path.strip("/").split("/")
        body = json.loads(req.content or b"{}")
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
        if parts[0] == "guilds" and parts[2] == "members":
            return httpx.Response(200, json={"roles": [], "nick": "Nick", "user": {"username": "u"}})
        return httpx.Response(404, json={})

    def client(self):
        from wowquester.integrations.discord_rest import DiscordRest
        return DiscordRest(http=httpx.Client(transport=httpx.MockTransport(self.handler)), token="fixture-token")
