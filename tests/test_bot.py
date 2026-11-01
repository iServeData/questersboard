"""Bot wiring without Discord: command payload, persistent custom_ids, actor building (FIXTURE interactions)."""
from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

from conftest import ROLE, fake_id, make_cfg

from wowquester.bot import client as bc
from wowquester.bot.render import cid
from wowquester.config import ConfigHolder


def test_command_payload_shape():
    cfg = make_cfg()
    p = bc.command_payload(cfg)
    names = {c["name"] for c in p}
    assert names == {"quest", "character"}
    quest = next(c for c in p if c["name"] == "quest")
    assert {o["name"] for o in quest["options"]} == {"list", "my", "join", "leave", "create"}
    blob = json.dumps(p)
    assert "Example Community" not in blob and not re.search(r"\d{17,20}", blob)


def test_persistent_custom_id_matches_render():
    for action in ("join", "tentative", "leave", "manage"):
        m = re.fullmatch(bc.CID, cid(action, 42))
        assert m and m["action"] == action and m["event_id"] == "42"
    assert re.fullmatch(bc.CID, "wq:ev:delete:42") is None


def _inter(guild_id, roles):
    user = SimpleNamespace(id=int(fake_id(10)), display_name="Tester",
                           roles=[SimpleNamespace(id=int(r)) for r in roles])
    return SimpleNamespace(user=user, guild_id=guild_id, channel_id=int(fake_id(55)))


def test_actor_from_guild_payload_and_foreign_guild():
    bc.Ctx.holder = ConfigHolder(make_cfg())
    a = asyncio.run(bc.build_actor(_inter(int(fake_id(1)), [ROLE["event_creator"]]), "slash"))
    assert a.in_guild and ROLE["event_creator"] in a.role_ids and a.channel_id == fake_id(55)
    b = asyncio.run(bc.build_actor(_inter(int(fake_id(999)), [ROLE["admin"]]), "slash"))
    assert not b.in_guild and not b.role_ids          # another server's roles never count


def test_dm_actor_uses_rest_lookup():
    class Rest:  # FIXTURE
        def member(self, g, u):
            return True, frozenset({ROLE["chronicle_reviewer"]}), "Nick"
    bc.Ctx.holder = ConfigHolder(make_cfg())
    bc.Ctx.rest = Rest()
    a = asyncio.run(bc.build_actor(_inter(None, []), "dm"))
    assert a.source == "dm" and ROLE["chronicle_reviewer"] in a.role_ids
    bc.Ctx.rest = None
    a = asyncio.run(bc.build_actor(_inter(None, []), "dm"))
    assert not a.in_guild
