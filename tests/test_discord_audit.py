"""discord-permissions report from FIXTURE guild data (no live Discord)."""
from __future__ import annotations

from conftest import ROLE, fake_id, make_cfg

from wowquester.integrations import discord_audit as da

G = fake_id(1)
CAT, EVENTS, CHAT, BOTCH, REVIEW = (fake_id(n) for n in (501, 502, 503, 504, 505))
BOTROLE, BOTUSER = fake_id(600), fake_id(601)
V, S, H, E_, U = da.VIEW, da.SEND, da.HISTORY, da.EMBED, da.USE_COMMANDS


def _cfg(**discord):
    return make_cfg(discord={"application_id": fake_id(2), "event_channel_id": EVENTS, "review_channel_id": REVIEW,
                             "allowed_command_channel_ids": [BOTCH], **discord})


def _guild(everyone=V | S | H | E_ | U, review_ows=None, bot_ows=None):
    roles = [{"id": G, "name": "@everyone", "permissions": str(everyone)},
             {"id": BOTROLE, "name": "WoWQuester", "permissions": str(V | S | H | E_)}]
    roles += [{"id": rid, "name": cap, "permissions": "0"} for cap, rid in ROLE.items()]
    ow = lambda i, allow=0, deny=0, t=0: {"id": i, "type": t, "allow": str(allow), "deny": str(deny)}  # noqa: E731
    chans = [
        {"id": CAT, "type": 4, "name": "WoW Now and Forever", "permission_overwrites": []},
        {"id": EVENTS, "type": 5, "parent_id": CAT, "position": 0, "name": "event-board",
         "permission_overwrites": [ow(G, deny=S), ow(BOTROLE, allow=S | E_)]},
        {"id": CHAT, "type": 0, "parent_id": CAT, "position": 1, "name": "world_of_warcraft", "permission_overwrites": []},
        {"id": BOTCH, "type": 0, "parent_id": CAT, "position": 2, "name": "bot-commands",
         "permission_overwrites": bot_ows if bot_ows is not None else []},
        {"id": REVIEW, "type": 0, "parent_id": CAT, "position": 3, "name": "wow-chronicler-moderation",
         "permission_overwrites": review_ows if review_ows is not None else
         [ow(G, deny=V), ow(ROLE["chronicle_reviewer"], allow=V | S | H), ow(ROLE["admin"], allow=V | S | H),
          ow(ROLE["chronicle_editor"], allow=V | S | H), ow(BOTROLE, allow=V | S | H | E_)]},
    ]
    return roles, chans, {"user": {"id": BOTUSER}, "roles": [BOTROLE]}


def test_effective_permissions_follow_discords_order():
    roles, chans, _ = _guild()
    rmap = {r["id"]: r for r in roles}
    review = chans[4]
    assert da.effective(G, rmap, review, da.Subject("x", frozenset())) == 0
    assert da.effective(G, rmap, review, da.Subject("r", frozenset({ROLE["chronicle_reviewer"]}))) & H
    ev = da.effective(G, rmap, chans[1], da.Subject("x", frozenset()))
    assert ev & V and not ev & S and not ev & E_          # no Send also means no Embed
    # member overwrite beats role overwrite
    chans[3]["permission_overwrites"] = [{"id": BOTUSER, "type": 1, "allow": "0", "deny": str(S)}]
    assert not da.effective(G, rmap, chans[3], da.Subject("b", frozenset({BOTROLE}), BOTUSER)) & S


def test_unregistered_commands_are_reported_first():
    roles, chans, bot = _guild()
    rep = da.build(_cfg(), G, roles, chans, bot, [], [])
    t = rep.text()
    assert "No WoWQuester slash commands are registered" in t and rep.fixes == 1
    assert "can use / commands here" in t and "can't see it (private)" in t and "can review here" in t
    assert 'Category "WoW Now and Forever" (4 channels)' in t


def test_missing_use_commands_and_leaky_review_channel_are_fixes():
    roles, chans, bot = _guild(everyone=V | S | H | E_,
                               review_ows=[{"id": BOTROLE, "type": 0, "allow": str(V), "deny": str(S | H)}])
    rep = da.build(_cfg(), G, roles, chans, bot, [{"id": "1", "name": "quest"}],
                   [{"id": fake_id(2), "permissions": [{"id": BOTCH, "type": 3, "permission": False}]}])
    t = rep.text()
    assert "Registered: /quest" in t
    assert "needs Use Application Commands here" in t
    assert "can see this private review channel" in t
    assert "Bot needs Send Messages, Embed Links, Read Message History here to post review notices" in t
    assert "all WoWQuester commands turned OFF for #bot-commands" in t
    assert rep.fixes >= 4


def test_bot_named_channel_outside_allowed_list_gets_a_note():
    roles, chans, bot = _guild()
    rep = da.build(_cfg(allowed_command_channel_ids=[CHAT]), G, roles, chans, bot, [{"id": "1", "name": "quest"}], [])
    assert "isn't in [discord] allowed_command_channel_ids" in rep.text()
