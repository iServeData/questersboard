"""wowquester-bot: slash commands, persistent buttons, modals and selects.

The bot only handles interactions. Event posts and DMs are sent by the worker from the
outbox, so a bot restart never loses or duplicates a post.

Commands are NOT registered on startup. Registration is a separate, approved step:
`wowquester-admin sync-commands --yes` (see docs/operations.md).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from datetime import datetime, timedelta

import discord
from discord import app_commands
from sqlalchemy import func, select

from ..config import Config, ConfigError, ConfigHolder, load
from ..db import Database, make_engine
from ..domain import catalog, events, permissions, roster, signups, status, voice
from ..domain.actor import Actor
from ..domain.errors import DomainError
from ..domain.timeutil import discord_ts, find_zone, now, resolve_local
from ..models import Event, Member, Signup
from ..secrets import SecretError, get_secret, setup_logging

log = logging.getLogger("wowquester.bot")
CID = r"wq:ev:(?P<action>join|tentative|leave|manage|start|end|endyes|voice|reopen):(?P<event_id>\d+)"


class Ctx:
    """Shared state for handlers (set in main / tests)."""
    holder: ConfigHolder
    db: Database
    rest = None            # discord_rest.DiscordRest for DM role lookups

    @classmethod
    def cfg(cls) -> Config:
        return cls.holder.cfg


def member_url(cfg: Config, *parts: str) -> str:
    return "/".join([cfg["community"]["public_base_url"].rstrip("/") + cfg["web"]["member_path"].rstrip("/"),
                     *parts])


async def build_actor(inter: discord.Interaction, source: str) -> Actor:
    """Roles come from Discord's interaction payload in guilds; in DMs we look the member up."""
    cfg = Ctx.cfg()
    user = inter.user
    if inter.guild_id is not None:
        if str(inter.guild_id) != cfg.guild_id:
            return Actor(str(user.id), source, frozenset(), in_guild=False)
        roles = frozenset(str(r.id) for r in getattr(user, "roles", []) if r.id != inter.guild_id)
        return Actor(str(user.id), source, roles, True, str(inter.channel_id), display_name=user.display_name,
                 username=getattr(user, "name", "") or "")
    if Ctx.rest is None:
        return Actor(str(user.id), "dm", frozenset(), in_guild=False)
    in_guild, roles, name = await asyncio.to_thread(Ctx.rest.member, cfg.guild_id, str(user.id))
    return Actor(str(user.id), "dm", roles, in_guild, None, display_name=name or user.display_name,
                 username=getattr(user, "name", "") or "")


async def run_tx(fn, *a, **kw):
    def work():
        with Ctx.db.tx() as s:
            return fn(s, *a, **kw)
    return await asyncio.to_thread(work)


def _md(text: str) -> str:
    """Event titles inside a masked link: keep brackets and markdown from breaking it."""
    return "".join("\\" + c if c in "[]()*_~`|\\" else c for c in str(text))


async def reply(inter: discord.Interaction, text: str, view: discord.ui.View | None = None) -> None:
    # suppress_embeds: links go through the login proxy, so Discord would otherwise preview its sign-in page
    kw = {"content": text[:1900], "ephemeral": True, "allowed_mentions": discord.AllowedMentions.none(),
          "suppress_embeds": True}
    if view is not None:
        kw["view"] = view
    if inter.response.is_done():
        await inter.followup.send(**kw)
    else:
        await inter.response.send_message(**kw)


async def guarded(inter: discord.Interaction, coro):
    try:
        return await coro
    except DomainError as exc:
        await reply(inter, exc.message)
    except Exception:  # noqa: BLE001
        log.exception("interaction failed")
        await reply(inter, "Something went wrong on our side. Please try again in a moment.")


# --------------------------------------------------------------------------- domain helpers (sync)

def _fitting(s, cfg: Config, actor: Actor, event_id: int):
    actor = roster.ensure_member(s, actor)
    ev = events.get(s, event_id)
    mine = roster.list_characters(s, actor.member_id)
    chars = [c for c in mine if not signups.fit_problems(cfg, ev, c)]
    # Same fill counts as the web signup form, so players can balance the group from Discord too.
    taken = dict(s.execute(select(Signup.role_key, func.count()).where(
        Signup.event_id == ev.id, Signup.state == "confirmed", Signup.role_key.is_not(None))
        .group_by(Signup.role_key)).all())
    roles = [(r.key, f"{r.label} \u00b7 {taken.get(r.key, 0)} of {r.quantity} taken",
              "Full: you'd be waitlisted" if taken.get(r.key, 0) >= r.quantity else
              f"{r.quantity - taken.get(r.key, 0)} open") for r in ev.roles]
    why = {"need": signups.event_requirement(cfg, ev),
           "misfits": [(c.name, signups.fit_problems(cfg, ev, c)) for c in mine if c not in chars]}
    return ev.title, ev.state, [(c.id, f"{c.name} · {_rc(s, c)}") for c in chars], roles, why


def _rc(s, c) -> str:
    """ "Mag'har Orc Shaman" from a character's saved keys, using the catalog's names."""
    return f"{catalog.label(s, c.edition, 'race', c.race_key)} {catalog.label(s, c.edition, 'class', c.class_key)}"


def no_fit_text(cfg: Config, title: str, why: dict) -> str:
    """The reply when none of a player's characters can join: what the event needs and why each one doesn't fit."""
    lines = [f"**{title}** is for {why['need']}."]
    if why["misfits"]:
        lines.append("None of your characters fit:")
        lines += [f"• {name}: {'; '.join(p)}" for name, p in why["misfits"][:8]]
    else:
        lines.append("You haven't added a character yet.")
    # <...> stops Discord adding a link preview (the login page's preview is GitHub's)
    lines.append(f"Add or change one with `/character add` or at <{member_url(cfg, 'characters')}>")
    return "\n".join(lines)


def _join(s, cfg, actor, event_id, character_id, role_key, tentative=False):
    su = signups.join(s, cfg, actor, event_id, character_id, role_key, tentative=tentative)
    return su.state


def _my(s, cfg, actor):
    actor = roster.ensure_member(s, actor)
    rows = s.execute(select(Signup, Event).join(Event, Event.id == Signup.event_id).where(
        Signup.member_id == actor.member_id, Signup.state.in_(signups.ACTIVE),
        Event.state.in_(("scheduled", "in_progress"))).order_by(Event.start_at).limit(15)).all()
    return [(ev.title, ev.start_at, su.state, su.needs_reconfirm, str(ev.public_id)) for su, ev in rows]


def _list(s, cfg):
    rows = s.scalars(select(Event).where(Event.state.in_(("scheduled", "in_progress")))
                     .order_by(Event.start_at).limit(15))
    return [(ev.id, ev.title, ev.start_at, ev.state, str(ev.public_id)) for ev in rows]


def _search(s, cfg, text: str):
    q = select(Event).where(Event.state.in_(("scheduled", "in_progress"))).order_by(Event.start_at).limit(25)
    if text:
        q = q.where(Event.title.ilike(f"%{text.replace('%', '')}%"))
    return [(ev.id, f"{ev.title} · {ev.start_at:%b %d %H:%M} UTC") for ev in s.scalars(q)]


def _organizer(s, cfg: Config, actor: Actor, event_id: int):
    """What the Organizer button offers this person: (public_id, manager?, actions)."""
    actor = roster.ensure_member(s, actor)
    ev = events.get(s, event_id)
    if not voice.is_manager(s, cfg, actor, ev):
        return str(ev.public_id), False, []
    acts = []
    if ev.state == "scheduled":
        acts.append("start")
    if ev.state == "in_progress":
        acts.append("end")
        if cfg["voice"]["enabled"] and not ev.discord_voice_channel_id and ev.voice_closed_at is not None:
            acts.append("reopen")
    return str(ev.public_id), True, acts


def _voice_for(s, cfg: Config, actor: Actor, event_id: int):
    """(state, channel id or None, may join?) for the Join voice button."""
    actor = roster.ensure_member(s, actor)
    ev = events.get(s, event_id)
    return ev.state, ev.discord_voice_channel_id, voice.can_join(s, cfg, actor, ev), ev.title


ORGANIZER_LABELS = {"start": ("Start event", 3), "end": ("End event", 4), "reopen": ("Reopen voice", 1),
                    "endyes": ("Yes, end it", 4)}


class OrganizerView(discord.ui.View):
    """Host-only buttons in the private Organizer reply. They use the persistent custom ids, so they
    keep working after a bot restart, and every click re-checks who is pressing it."""

    def __init__(self, event_id: int, actions: list[str], url: str | None = None):
        super().__init__(timeout=None)
        for a in actions:
            label, style = ORGANIZER_LABELS[a]
            self.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle(style),
                                            custom_id=f"wq:ev:{a}:{event_id}"))
        if url:
            self.add_item(discord.ui.Button(label="Manage on the web", url=url))


class LinkView(discord.ui.View):
    def __init__(self, label: str, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label=label, url=url))


async def organizer(inter: discord.Interaction, event_id: int) -> None:
    cfg = Ctx.cfg()
    actor = await build_actor(inter, "button")
    permissions.require_member(cfg, actor)
    pid, manager, acts = await run_tx(_organizer, cfg, actor, event_id)
    url = member_url(cfg, "events", pid)
    if not manager:
        await reply(inter, f"Manage this event on the web: {url}")
        return
    await reply(inter, "Organizer tools. Start opens the event (and its voice channel, when voice is on); "
                       "End closes it." if acts else "Organizer tools.", OrganizerView(event_id, acts, url))


async def organizer_action(inter: discord.Interaction, action: str, event_id: int) -> None:
    cfg = Ctx.cfg()
    actor = await build_actor(inter, "button")
    if action == "end":
        await reply(inter, "End the event now? This closes signups, removes the voice channel and asks "
                           "for the chronicle.", OrganizerView(event_id, ["endyes"]))
        return
    if action == "start":
        await run_tx(lambda s: events.start(s, cfg, actor, event_id))
        extra = " The voice channel opens in a moment; use **Join voice** on the post." if cfg["voice"]["enabled"] else ""
        await reply(inter, "Started." + extra)
    elif action == "endyes":
        await run_tx(lambda s: events.end(s, cfg, actor, event_id))
        await reply(inter, "Ended. You'll get a DM to record attendance and write the chronicle.")
    elif action == "reopen":
        await run_tx(lambda s: voice.reopen(s, cfg, actor, event_id))
        await reply(inter, "Reopening voice. The Join voice button comes back in a moment.")


async def join_voice(inter: discord.Interaction, event_id: int) -> None:
    cfg = Ctx.cfg()
    actor = await build_actor(inter, "button")
    permissions.require_member(cfg, actor)
    state, ch, ok, title = await run_tx(_voice_for, cfg, actor, event_id)
    if state != "in_progress" or not ch:
        await reply(inter, "Voice isn't open for this event right now.")
    elif not ok:
        await reply(inter, f"Voice for **{_md(title)}** is for people signed up. Press **Join** on the post "
                           "and you'll get access straight away.")
    else:
        tip = ("\nPlaying with someone outside the event? `/voice invite` gives you a guest link for them."
               if cfg["voice"]["guest_invites"] else "")
        await reply(inter, f"Voice for **{_md(title)}**: <#{ch}>{tip}", LinkView("Open voice channel", voice.link(cfg, ch)))


# --------------------------------------------------------------------------- views

class CharacterSelect(discord.ui.Select):
    def __init__(self, event_id: int, chars, roles, tentative: bool):
        self.event_id, self.roles, self.tentative = event_id, roles, tentative
        super().__init__(placeholder="Choose your character",
                         options=[discord.SelectOption(label=label[:100], value=str(cid)) for cid, label in chars[:25]])

    async def callback(self, inter: discord.Interaction):
        cid = int(self.values[0])
        if self.roles:
            await inter.response.edit_message(content="Choose a role", view=RoleView(self.event_id, cid, self.roles,
                                                                                  self.tentative))
            return
        await guarded(inter, finish_join(inter, self.event_id, cid, None, self.tentative))


class RoleSelect(discord.ui.Select):
    def __init__(self, event_id: int, cid: int, roles, tentative: bool):
        self.event_id, self.cid, self.tentative = event_id, cid, tentative
        super().__init__(placeholder="Choose a role",
                         options=[discord.SelectOption(label=label[:100], value=key, description=desc[:100])
                                  for key, label, desc in roles[:25]])

    async def callback(self, inter: discord.Interaction):
        await guarded(inter, finish_join(inter, self.event_id, self.cid, self.values[0], self.tentative))


class CharacterView(discord.ui.View):
    def __init__(self, event_id, chars, roles, tentative):
        super().__init__(timeout=300)
        self.add_item(CharacterSelect(event_id, chars, roles, tentative))


class RoleView(discord.ui.View):
    def __init__(self, event_id, cid, roles, tentative):
        super().__init__(timeout=300)
        self.add_item(RoleSelect(event_id, cid, roles, tentative))


STATE_TEXT = {"confirmed": "You're in.", "pending": "Request sent. A host will approve or decline it.",
              "waitlisted": "The event is full, so you're on the waitlist. You'll get a DM if a spot opens.",
              "tentative": "Marked tentative. That doesn't hold a spot; join properly when you're sure."}


async def finish_join(inter, event_id, cid, role_key, tentative):
    actor = await build_actor(inter, "button")
    state = await run_tx(_join, Ctx.cfg(), actor, event_id, cid, role_key, tentative)
    text = STATE_TEXT.get(state, f"Signup {state}.")
    if inter.response.is_done():
        await inter.followup.send(text, ephemeral=True, suppress_embeds=True)
    else:
        await inter.response.edit_message(content=text, view=None)


async def start_join(inter: discord.Interaction, event_id: int, tentative: bool, source: str = "button"):
    cfg = Ctx.cfg()
    actor = await build_actor(inter, source)
    permissions.require_member(cfg, actor)
    title, state, chars, roles, why = await run_tx(_fitting, cfg, actor, event_id)
    if state not in ("scheduled", "in_progress"):
        await reply(inter, "This event isn't open for signups.")
        return
    if not chars:
        await reply(inter, no_fit_text(cfg, title, why))
        return
    if len(chars) == 1 and not roles:
        await guarded(inter, finish_join(inter, event_id, chars[0][0], None, tentative))
        return
    await reply(inter, f"Joining **{title}**", CharacterView(event_id, chars, roles, tentative))


class EventButton(discord.ui.DynamicItem[discord.ui.Button], template=CID):
    """Persistent: the custom_id carries the action and event id, so buttons survive restarts."""

    def __init__(self, action: str, event_id: int):
        super().__init__(discord.ui.Button(custom_id=f"wq:ev:{action}:{event_id}", label=action.title()))
        self.action, self.event_id = action, event_id

    @classmethod
    async def from_custom_id(cls, inter, item, match: re.Match):
        return cls(match["action"], int(match["event_id"]))

    async def callback(self, inter: discord.Interaction):
        cfg = Ctx.cfg()
        if self.action in ("join", "tentative"):
            await guarded(inter, start_join(inter, self.event_id, self.action == "tentative"))
        elif self.action == "leave":
            async def go():
                actor = await build_actor(inter, "button")
                await run_tx(lambda s: signups.leave(s, cfg, actor, self.event_id))
                await reply(inter, "You've left the event.")
            await guarded(inter, go())
        elif self.action == "manage":
            await guarded(inter, organizer(inter, self.event_id))
        elif self.action == "voice":
            await guarded(inter, join_voice(inter, self.event_id))
        else:
            await guarded(inter, organizer_action(inter, self.action, self.event_id))


# --------------------------------------------------------------------------- modals

class CreateModal(discord.ui.Modal, title="Plan a WoW event"):
    name = discord.ui.TextInput(label="Title", min_length=3, max_length=120)
    when = discord.ui.TextInput(label="Start (YYYY-MM-DD HH:MM, your time zone)", placeholder="2026-11-07 19:00")
    tz = discord.ui.TextInput(label="Time zone (city or IANA name)", placeholder="Los Angeles", max_length=64)
    minutes = discord.ui.TextInput(label="Length in minutes", default="120", max_length=4)
    kind = discord.ui.TextInput(label="Type (dungeon, quest, raid, rp, world_pvp…)", default="quest", max_length=24)

    def __init__(self, tz_default: str):
        super().__init__()
        self.tz.default = tz_default      # the host's saved zone (My settings), else the community default

    async def on_submit(self, inter: discord.Interaction):
        cfg = Ctx.cfg()

        async def go():
            actor = await build_actor(inter, "slash" if inter.guild_id else "dm")
            tzname = find_zone(self.tz.value or cfg["community"]["default_timezone"])
            try:
                local = datetime.strptime(self.when.value.strip(), "%Y-%m-%d %H:%M")
                mins = int(self.minutes.value)
            except ValueError:
                await reply(inter, "Use the format YYYY-MM-DD HH:MM and a number of minutes.")
                return
            start = resolve_local(local, tzname)
            ed = next(iter(cfg.editions))
            data = dict(edition=ed, title=self.name.value, activity_type=self.kind.value.strip().lower(),
                        start_at=start, end_at=start + timedelta(minutes=mins), timezone=tzname)
            pid = await run_tx(lambda s: str(events.create_draft(s, cfg, actor, data).public_id))
            how = ("publish" if permissions.has(cfg, actor, "event_creator")
                   else "send it for publishing (a moderator posts it)")
            await reply(inter, f"Draft saved (drafts reserve nothing and only you can see them). Add roles, "
                               f"location and {how} here: {member_url(cfg, 'events', pid, 'edit')}")
        await guarded(inter, go())


class CharacterModal(discord.ui.Modal, title="Add a WoW character"):
    name = discord.ui.TextInput(label="Character name", max_length=24)
    race_class = discord.ui.TextInput(label="Race and class", placeholder="orc warrior", max_length=40)
    place = discord.ui.TextInput(label="Ruleset (Forever) or realm (Retail, Classic)", placeholder="pvp", max_length=64)
    roles = discord.ui.TextInput(label="Roles you can fill", placeholder="tank, damage", required=False)

    def __init__(self, edition: str, draft: dict | None = None):
        super().__init__()
        self.edition = edition
        # Reopened after a problem: put back everything the player typed so nothing has to be retyped.
        for field in ("name", "race_class", "place", "roles"):
            if draft and draft.get(field):
                getattr(self, field).default = draft[field]

    async def on_submit(self, inter: discord.Interaction):
        draft = {"edition": self.edition, "name": self.name.value.strip(), "race_class": self.race_class.value.strip(),
                 "place": self.place.value.strip(), "roles": self.roles.value.strip()}
        await guarded(inter, submit_character(inter, draft))


def _draft_parts(s, cfg: Config, draft: dict):
    """(race text, class text, race choices, class choices) for a draft; choices are (key, label) for the pickers."""
    ed = cfg.edition(draft["edition"])
    faction = draft.get("faction") or ed.default_faction
    race, klass = draft.get("race"), draft.get("klass")
    if race is None or klass is None:
        r, k = catalog.split_race_class(s, ed.key, faction, draft.get("race_class", ""))
        race, klass = (r if race is None else race), (k if klass is None else klass)
    races = [(o.key, o.label) for o in catalog.options(s, ed.key, "race", faction)]
    classes = [(o.key, o.label) for o in catalog.options(s, ed.key, "class")]
    co = catalog.resolve(s, ed.key, "class", klass) if klass else None
    if co is not None and not race:                 # class typed, race missing: offer races that can be it
        fit = catalog.races_for_class(s, ed.key, faction, co.key)
        if fit:
            races = [r for r in races if r[0] in fit]
    ro = catalog.resolve(s, ed.key, "race", race, faction) if race else None
    if ro is not None:
        ok = {o.key.split(":", 1)[1] for o in catalog.options(s, ed.key, "race_class", faction, limit=1000)
              if o.key.startswith(ro.key + ":")}
        if ok:
            classes = [c for c in classes if c[0] in ok]
    return race, klass, races, classes


def _save_character(s, cfg: Config, actor: Actor, draft: dict, race: str, klass: str) -> str:
    ed = cfg.edition(draft["edition"])
    data = dict(edition=ed.key, name=draft["name"], race_key=race, class_key=klass,
                eligible_roles=[r for r in re.split(r"[,\s]+", draft.get("roles", "").lower()) if r])
    place = draft.get("place", "").strip().lower()
    if ed.uses_realms:
        data["realm_slug"] = place
    else:
        key = place.replace("-", "_").replace(" ", "_")
        by_label = {ed.ruleset_label(r).lower(): r for r in ed.rulesets}
        data["ruleset"] = (by_label.get(place) or key) if place else ed.default_ruleset   # "RP-PvP" -> rp_pvp
    return roster.add_character(s, cfg, actor, data).name


async def _answer(inter: discord.Interaction, text: str, view: discord.ui.View | None) -> None:
    """From a picker or button on our own message, update that message; otherwise send a private reply."""
    # A form reopened from our button also carries that message, so it is replaced rather than left stale.
    if inter.message is not None and not inter.response.is_done() and \
            inter.type in (discord.InteractionType.component, discord.InteractionType.modal_submit):
        await inter.response.edit_message(content=text[:1900], view=view, allowed_mentions=discord.AllowedMentions.none())
    else:
        await reply(inter, text, view)


async def submit_character(inter: discord.Interaction, draft: dict) -> None:
    """Save the character, or ask for what's missing. Nothing typed is ever thrown away: a missing race or class
    gets a picker, and any other problem comes with a button that reopens the form already filled in."""
    cfg = Ctx.cfg()
    actor = await build_actor(inter, "slash" if inter.guild_id else "dm")
    race, klass, races, classes = await run_tx(_draft_parts, cfg, draft)
    draft = {**draft, "race": race, "klass": klass}
    who = f"**{draft['name']}**" if draft["name"] else "Your character"
    if not race or not klass:
        missing = "race" if not race else "class"
        text = f"{who} isn't saved yet: pick a {missing} (you wrote \"{draft.get('race_class', '')}\")."
        await _answer(inter, text, FinishCharacterView(draft, missing, races if missing == "race" else classes))
        return
    ck = await run_tx(lambda s: getattr(catalog.resolve(s, cfg.edition(draft["edition"]).key, "class", klass), "key", None))
    if ck is not None and ck not in {k for k, _ in classes}:
        missing = "class"
        text = f"{who} isn't saved yet: {race.title()} can't be a {klass.title()} here. Pick a class it can be."
        await _answer(inter, text, FinishCharacterView(draft, missing, races if missing == "race" else classes))
        return
    try:
        name = await run_tx(_save_character, cfg, actor, draft, race, klass)
    except DomainError as exc:
        await _answer(inter, f"Not saved: {exc.message}", FinishCharacterView(draft, None, []))
        return
    await _answer(inter, f"Added **{name}**. Edit details any time at <{member_url(cfg, 'characters')}>", None)


class PickSelect(discord.ui.Select):
    def __init__(self, draft: dict, missing: str, choices):
        self.draft, self.missing = draft, missing
        super().__init__(placeholder=f"Choose a {missing}",
                         options=[discord.SelectOption(label=label[:100], value=key) for key, label in choices[:25]])

    async def callback(self, inter: discord.Interaction):
        key = "race" if self.missing == "race" else "klass"
        await guarded(inter, submit_character(inter, {**self.draft, key: self.values[0]}))


class EditFormButton(discord.ui.Button):
    def __init__(self, draft: dict):
        super().__init__(label="Edit the form", style=discord.ButtonStyle.secondary)
        self.draft = draft

    async def callback(self, inter: discord.Interaction):
        rc = " ".join(x for x in (self.draft.get("race"), self.draft.get("klass")) if x) or self.draft.get("race_class")
        await inter.response.send_modal(CharacterModal(self.draft["edition"], {**self.draft, "race_class": rc}))


class FinishCharacterView(discord.ui.View):
    def __init__(self, draft: dict, missing: str | None, choices):
        super().__init__(timeout=600)
        if missing and choices:
            self.add_item(PickSelect(draft, missing, choices))
        self.add_item(EditFormButton(draft))


# --------------------------------------------------------------------------- commands

def build_tree(client: discord.Client) -> app_commands.CommandTree:
    tree = app_commands.CommandTree(client)
    quest = app_commands.Group(name="quest", description="WoW events")
    character = app_commands.Group(name="character", description="Your WoW characters")

    async def event_autocomplete(inter: discord.Interaction, current: str):
        rows = await run_tx(_search, Ctx.cfg(), current)
        return [app_commands.Choice(name=label[:100], value=str(eid)) for eid, label in rows]

    @quest.command(name="list", description="Upcoming WoW events")
    async def q_list(inter: discord.Interaction):
        async def go():
            # same access rules as every other command (member, allowed channel, DMs if enabled)
            permissions.require_member(Ctx.cfg(), await build_actor(inter, "slash" if inter.guild_id else "dm"))
            rows = await run_tx(_list, Ctx.cfg())
            if not rows:
                await reply(inter, "No WoW events are scheduled.")
                return
            lines = [f"• **[{_md(t)}](<{member_url(Ctx.cfg(), 'events', pid)}>)** {discord_ts(st, 'F')}"
                     f"{' (in progress)' if state == 'in_progress' else ''}" for _, t, st, state, pid in rows]
            await reply(inter, "\n".join(lines))
        await guarded(inter, go())

    @quest.command(name="my", description="Events you've joined")
    async def q_my(inter: discord.Interaction):
        async def go():
            actor = await build_actor(inter, "slash" if inter.guild_id else "dm")
            permissions.require_member(Ctx.cfg(), actor)
            rows = await run_tx(_my, Ctx.cfg(), actor)
            if not rows:
                await reply(inter, "You haven't joined any upcoming events.")
                return
            await reply(inter, "\n".join(f"• **{t}** {discord_ts(st, 'F')} · {state}"
                                         f"{' · **please reconfirm**' if rc else ''}" for t, st, state, rc, _ in rows))
        await guarded(inter, go())

    @quest.command(name="join", description="Join an event")
    @app_commands.autocomplete(event=event_autocomplete)
    async def q_join(inter: discord.Interaction, event: str, tentative: bool = False):
        await guarded(inter, start_join(inter, int(event), tentative, "slash" if inter.guild_id else "dm"))

    @quest.command(name="leave", description="Leave an event")
    @app_commands.autocomplete(event=event_autocomplete)
    async def q_leave(inter: discord.Interaction, event: str):
        async def go():
            actor = await build_actor(inter, "slash" if inter.guild_id else "dm")
            await run_tx(lambda s: signups.leave(s, Ctx.cfg(), actor, int(event)))
            await reply(inter, "You've left the event.")
        await guarded(inter, go())

    @quest.command(name="create", description="Plan a new event (saved as a draft)")
    async def q_create(inter: discord.Interaction):
        cfg = Ctx.cfg()
        saved = await run_tx(lambda s: s.scalar(select(Member.timezone).where(
            Member.discord_id == str(inter.user.id), Member.erased_at.is_(None))))
        await inter.response.send_modal(CreateModal(saved or cfg["community"]["default_timezone"]))

    async def edition_autocomplete(inter: discord.Interaction, current: str):
        return [app_commands.Choice(name=ed.name, value=k) for k, ed in Ctx.cfg().editions.items()
                if current.lower() in ed.name.lower()][:25]

    @character.command(name="add", description="Add a character")
    @app_commands.autocomplete(edition=edition_autocomplete)
    async def c_add(inter: discord.Interaction, edition: str | None = None):
        cfg = Ctx.cfg()
        ed = edition if edition in cfg.editions else next(iter(cfg.editions))
        await inter.response.send_modal(CharacterModal(ed))

    @character.command(name="list", description="Your characters")
    async def c_list(inter: discord.Interaction):
        async def go():
            actor = await build_actor(inter, "slash" if inter.guild_id else "dm")
            permissions.require_member(Ctx.cfg(), actor)

            def q(s):
                a = roster.ensure_member(s, actor)
                out = []
                for c in roster.list_characters(s, a.member_id):
                    ed = Ctx.cfg().edition(c.edition)
                    out.append((c.name, ed.name, ed.ruleset_label(c.ruleset) if c.ruleset else c.realm_slug or "",
                                _rc(s, c)))
                return out
            rows = await run_tx(q)
            await reply(inter, "\n".join(f"• **{_md(n)}** · {e} {p} · {rc}" for n, e, p, rc in rows)
                        or "No characters yet. Use `/character add`.")
        await guarded(inter, go())

    @character.command(name="edit", description="Edit characters on the web")
    async def c_edit(inter: discord.Interaction):
        await reply(inter, f"Edit your characters here: {member_url(Ctx.cfg(), 'characters')}")

    @character.command(name="remove", description="Remove a character")
    async def c_remove(inter: discord.Interaction, name: str):
        async def go():
            actor = await build_actor(inter, "slash" if inter.guild_id else "dm")

            def q(s):
                a = roster.ensure_member(s, actor)
                ch = next((c for c in roster.list_characters(s, a.member_id) if c.name.lower() == name.lower()), None)
                if ch is None:
                    return "No character with that name."
                return roster.remove_character(s, Ctx.cfg(), a, ch.id)
            res = await run_tx(q)
            await reply(inter, {"deleted": "Removed.", "archived": "Archived (it has event history)."}.get(res, res))
        await guarded(inter, go())

    vgroup = app_commands.Group(name="voice", description="Event voice channel")

    @vgroup.command(name="invite", description="Get a guest link to this event's voice for someone you're playing with")
    async def v_invite(inter: discord.Interaction):
        async def go():
            cfg = Ctx.cfg()
            actor = await build_actor(inter, "slash")

            def q(s):
                eid = voice.event_for_command(s, cfg, actor, str(inter.channel_id), manage=False)
                ev, code = voice.invite_for(s, cfg, actor, eid)
                return ev.title, code
            title, code = await run_tx(q)
            await reply(inter, voice.invite_dm_text(cfg, _Titled(title), code))
        await guarded(inter, go())

    async def _moderate(inter: discord.Interaction, member: discord.Member, action: str, done: str):
        cfg = Ctx.cfg()
        actor = await build_actor(inter, "slash")
        owner = getattr(inter.guild, "owner_id", None)
        target = voice.Target(str(member.id), frozenset(str(r.id) for r in getattr(member, "roles", [])
                                                        if r.id != inter.guild_id),
                              is_owner=owner is not None and member.id == owner, is_bot=bool(member.bot))

        def q(s):
            eid = voice.event_for_command(s, cfg, actor, str(inter.channel_id), manage=True)
            return voice.moderate(s, cfg, actor, eid, target, action).title
        title = await run_tx(q)
        await reply(inter, done.format(name=_md(member.display_name), title=_md(title)))

    @vgroup.command(name="kick", description="Take someone out of your event's voice channel")
    async def v_kick(inter: discord.Interaction, member: discord.Member):
        await guarded(inter, _moderate(inter, member, "kick",
                                       "Took **{name}** out of voice for **{title}**. They can rejoin if they "
                                       "still have access; use `/voice ban` to keep them out."))

    @vgroup.command(name="ban", description="Take someone out of your event's voice and keep them out until it ends")
    async def v_ban(inter: discord.Interaction, member: discord.Member):
        await guarded(inter, _moderate(inter, member, "ban",
                                       "**{name}** is out of voice for **{title}** until the event ends."))

    @vgroup.command(name="unban", description="Let someone back into your event's voice")
    async def v_unban(inter: discord.Interaction, member: discord.Member):
        await guarded(inter, _moderate(inter, member, "unban",
                                       "**{name}** can use voice for **{title}** again if they have access."))

    for cmd in (quest, character):
        cmd.allowed_contexts = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=False)
        cmd.allowed_installs = app_commands.AppInstallationType(guild=True, user=False)
        tree.add_command(cmd)
    vgroup.allowed_contexts = app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False)
    vgroup.allowed_installs = app_commands.AppInstallationType(guild=True, user=False)
    tree.add_command(vgroup)
    return tree


class _Titled:
    """Just enough of an event for voice.invite_dm_text."""
    def __init__(self, title: str):
        self.title = title


class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True          # no privileged intents (no message content, no member list)
        intents.voice_states = True    # non-privileged: only to count people in event voice channels
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = build_tree(self)
        self.started_at = now()

    def gateway_state(self) -> str:
        return "connected" if self.is_ready() and not self.is_closed() and self.ws is not None else "disconnected"

    async def heartbeat(self, state: str | None = None) -> None:
        """Check in for the status card. A DB hiccup is logged, never fatal."""
        try:
            await run_tx(status.beat, "bot", state or self.gateway_state(), self.started_at)
        except Exception:  # noqa: BLE001
            log.warning("status heartbeat failed", exc_info=True)

    async def on_ready(self):
        await self.heartbeat("connected")

    async def on_resumed(self):
        await self.heartbeat("connected")

    async def on_disconnect(self):
        await self.heartbeat("disconnected")

    def voice_counts(self, channel_ids) -> dict[str, int]:
        """People (not bots) in each of our event voice channels, from Discord's voice-state cache."""
        out = {}
        for cid in channel_ids:
            ch = self.get_channel(int(cid))
            if ch is not None:
                out[str(cid)] = sum(1 for m in getattr(ch, "members", []) if not m.bot)
        return out

    async def on_voice_state_update(self, member, before, after):
        ids = {str(c.id) for c in (before.channel, after.channel) if c is not None}
        if not ids or not Ctx.cfg()["voice"]["enabled"]:
            return
        try:
            mine = set(await run_tx(voice.open_channels)) & ids
            if mine:
                await run_tx(voice.record_counts, self.voice_counts(mine))
        except Exception:  # noqa: BLE001
            log.warning("voice count update failed", exc_info=True)

    async def setup_hook(self):
        self.add_dynamic_items(EventButton)

        async def reload_loop():
            while True:
                await asyncio.sleep(Ctx.holder.interval)
                live = Ctx.holder.check()
                if live:
                    from ..worker import log_config
                    await run_tx(lambda s: log_config(s, "bot", Ctx.cfg(), live))
        self.loop.create_task(reload_loop())

        async def voice_loop():
            # Catch up after a restart and keep empty channels' clocks honest.
            while True:
                await asyncio.sleep(60)
                if not Ctx.cfg()["voice"]["enabled"] or not self.is_ready():
                    continue
                try:
                    ids = await run_tx(voice.open_channels)
                    await run_tx(voice.record_counts, self.voice_counts(ids))
                except Exception:  # noqa: BLE001
                    log.warning("voice count refresh failed", exc_info=True)
        self.loop.create_task(voice_loop())

        async def heartbeat_loop():
            while True:
                await self.heartbeat()
                await asyncio.sleep(Ctx.cfg()["status"]["heartbeat_seconds"])
        self.loop.create_task(heartbeat_loop())
        log.info("bot ready: persistent buttons registered; slash commands are synced only by the admin CLI")


def command_payload(cfg: Config) -> list[dict]:
    """The exact JSON `sync-commands` would PUT (shown for approval before any registration)."""
    client = discord.Client(intents=discord.Intents.none())
    tree = build_tree(client)
    return [c.to_dict(tree) for c in tree.get_commands()]


def main() -> int:  # pragma: no cover - service entry point
    setup_logging()
    try:
        cfg = load()
    except ConfigError as exc:
        log.error("invalid configuration: %s", exc)
        return 78
    try:
        token = get_secret("discord-bot-token").reveal()
    except SecretError as exc:
        log.error("%s", exc)
        return 78
    from ..integrations import discord_rest as dr
    Ctx.holder = ConfigHolder(cfg)
    Ctx.db = Database(make_engine(cfg["database"]["app_url"], "db-app-password", 3, "wowquester-bot"))
    Ctx.rest = dr.DiscordRest(token=token)
    bot = Bot()
    bot.run(token, log_handler=None)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
