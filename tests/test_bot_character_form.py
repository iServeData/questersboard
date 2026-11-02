"""/character add form: nothing typed is lost when race or class is missing or wrong (FIXTURE interactions)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import pytest
from conftest import ROLE, fake_id, needs_db
from sqlalchemy import select

from wowquester.bot import client as bc
from wowquester.config import ConfigHolder
from wowquester.domain import catalog
from wowquester.models import Character

pytestmark = needs_db


class Resp:  # FIXTURE: records what the bot would send to Discord
    def __init__(self, log):
        self.log, self.done = log, False

    def is_done(self):
        return self.done

    async def send_message(self, **kw):
        self.done = True
        self.log.append(("send", kw.get("content"), kw.get("view")))

    async def edit_message(self, **kw):
        self.done = True
        self.log.append(("edit", kw.get("content"), kw.get("view")))

    async def send_modal(self, modal):
        self.done = True
        self.log.append(("modal", None, modal))


def _inter(log, kind=discord.InteractionType.modal_submit, message=None):
    user = SimpleNamespace(id=int(fake_id(10)), display_name="Tester", roles=[SimpleNamespace(id=int(ROLE["member"]))])
    return SimpleNamespace(user=user, guild_id=int(fake_id(1)), channel_id=int(fake_id(55)), type=kind,
                           message=message, response=Resp(log), followup=None)


@pytest.fixture
def bot(seeded, cfg):
    bc.Ctx.holder, bc.Ctx.db, bc.Ctx.rest = ConfigHolder(cfg), seeded, None
    return seeded


def _draft(**kw):
    return {"edition": "forever", "name": "Grommash", "race_class": "Orc", "place": "PVP", "roles": "Tank, damage", **kw}


def _chars(db):
    with db.tx() as s:
        return [(c.name, c.race_key, c.class_key, c.ruleset) for c in s.scalars(select(Character))]


def test_split_race_class_reads_one_line(seeded):
    with seeded.tx() as s:
        sp = lambda t, f="horde": catalog.split_race_class(s, "forever", f, t)  # noqa: E731
        assert sp("Orc Warrior") == ("Orc", "Warrior")
        assert sp("warrior orc") == ("orc", "warrior")
        assert sp("Night Elf Druid", "alliance") == ("Night Elf", "Druid")
        assert sp("Skyborne Druid") == ("Skyborne", "Druid")
        assert sp("Orc") == ("Orc", "")
        assert sp("warrior") == ("", "warrior")
        assert sp("Vulpera Monk") == ("Vulpera", "Monk")        # unknown words: last word is the class
        assert sp("") == ("", "")


def test_race_only_gets_a_class_picker_then_saves(bot):
    log = []
    asyncio.run(bc.submit_character(_inter(log), _draft()))
    kind, text, view = log[-1]
    assert kind == "send" and "isn't saved yet: pick a class" in text and _chars(bot) == []
    pick = next(i for i in view.children if isinstance(i, bc.PickSelect))
    assert {o.value for o in pick.options} == {"warrior", "hunter", "rogue", "shaman", "warlock", "mage"}
    log2 = []                                                    # the player chooses Warrior in the picker
    asyncio.run(bc.submit_character(_inter(log2, discord.InteractionType.component, message=object()),
                                    {**pick.draft, "klass": "warrior"}))
    assert log2[-1][0] == "edit" and "Added **Grommash**" in log2[-1][1] and log2[-1][2] is None
    assert _chars(bot) == [("Grommash", "orc", "warrior", "pvp")]


def test_class_only_gets_a_race_picker(bot):
    log = []
    asyncio.run(bc.submit_character(_inter(log), _draft(race_class="Druid")))
    _, text, view = log[-1]
    assert "pick a race" in text
    pick = next(i for i in view.children if isinstance(i, bc.PickSelect))
    assert {o.value for o in pick.options} == {"orc", "tauren", "troll", "undead", "skyborne"}  # Horde only


def test_impossible_pair_offers_the_classes_it_can_be(bot):
    log = []
    asyncio.run(bc.submit_character(_inter(log), _draft(race_class="Tauren Mage")))
    _, text, view = log[-1]
    assert "Tauren can't be a Mage" in text
    pick = next(i for i in view.children if isinstance(i, bc.PickSelect))
    assert {o.value for o in pick.options} == {"warrior", "hunter", "shaman", "druid"}


def test_other_problems_keep_the_form_filled_in(bot):
    log = []
    asyncio.run(bc.submit_character(_inter(log), _draft(name="Grom1", race_class="Orc Warrior")))
    kind, text, view = log[-1]
    assert text.startswith("Not saved:") and "letters only" in text and _chars(bot) == []
    btn = next(i for i in view.children if isinstance(i, bc.EditFormButton))
    log2 = []
    asyncio.run(btn.callback(_inter(log2, discord.InteractionType.component, message=object())))
    modal = log2[-1][2]
    assert isinstance(modal, bc.CharacterModal)
    assert (modal.name.default, modal.race_class.default, modal.place.default, modal.roles.default) == \
        ("Grom1", "Orc Warrior", "PVP", "Tank, damage")


def test_full_line_saves_straight_away(bot):
    log = []
    asyncio.run(bc.submit_character(_inter(log), _draft(name="Test", race_class="Orc Warrior")))
    assert log[-1][0] == "send" and "Added **Test**" in log[-1][1]
    assert _chars(bot) == [("Test", "orc", "warrior", "pvp")]
