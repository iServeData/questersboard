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
from sqlalchemy import select

from ..config import Config, ConfigError, ConfigHolder, load
from ..db import Database, make_engine
from ..domain import events, permissions, roster, signups, status
from ..domain.actor import Actor
from ..domain.errors import DomainError
from ..domain.timeutil import discord_ts, now, resolve_local
from ..models import Event, Signup
from ..secrets import SecretError, get_secret, setup_logging

log = logging.getLogger("wowquester.bot")
CID = r"wq:ev:(?P<action>join|tentative|leave|manage):(?P<event_id>\d+)"


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
        return Actor(str(user.id), source, roles, True, str(inter.channel_id), display_name=user.display_name)
    if Ctx.rest is None:
        return Actor(str(user.id), "dm", frozenset(), in_guild=False)
    in_guild, roles, name = await asyncio.to_thread(Ctx.rest.member, cfg.guild_id, str(user.id))
    return Actor(str(user.id), "dm", roles, in_guild, None, display_name=name or user.display_name)


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
    roles = [(r.key, r.label) for r in ev.roles]
    why = {"need": signups.event_requirement(cfg, ev),
           "misfits": [(c.name, signups.fit_problems(cfg, ev, c)) for c in mine if c not in chars]}
    return ev.title, ev.state, [(c.id, f"{c.name} · {c.race_key.title()} {c.class_key.title()}") for c in chars], \
        roles, why


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
                         options=[discord.SelectOption(label=label[:100], value=key) for key, label in roles[:25]])

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
        else:
            async def link():
                pid = await run_tx(lambda s: str(events.get(s, self.event_id).public_id))
                await reply(inter, f"Manage this event on the web: {member_url(cfg, 'events', pid)}")
            await guarded(inter, link())


# --------------------------------------------------------------------------- modals

class CreateModal(discord.ui.Modal, title="Plan a WoW event"):
    name = discord.ui.TextInput(label="Title", min_length=3, max_length=120)
    when = discord.ui.TextInput(label="Start (YYYY-MM-DD HH:MM, your time zone)", placeholder="2026-11-07 19:00")
    tz = discord.ui.TextInput(label="Time zone (IANA)", placeholder="America/Los_Angeles", required=False)
    minutes = discord.ui.TextInput(label="Length in minutes", default="120", max_length=4)
    kind = discord.ui.TextInput(label="Type (dungeon, quest, raid, rp, world_pvp…)", default="quest", max_length=24)

    async def on_submit(self, inter: discord.Interaction):
        cfg = Ctx.cfg()

        async def go():
            actor = await build_actor(inter, "slash" if inter.guild_id else "dm")
            tzname = (self.tz.value or cfg["community"]["default_timezone"]).strip()
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
            await reply(inter, f"Draft saved (drafts reserve nothing and only you can see them). Add roles, "
                               f"location and publish here: {member_url(cfg, 'events', pid, 'edit')}")
        await guarded(inter, go())


class CharacterModal(discord.ui.Modal, title="Add a WoW character"):
    name = discord.ui.TextInput(label="Character name", max_length=24)
    race_class = discord.ui.TextInput(label="Race and class", placeholder="orc warrior", max_length=40)
    place = discord.ui.TextInput(label="Ruleset (Forever) or realm (Retail)", placeholder="pvp", max_length=64)
    roles = discord.ui.TextInput(label="Roles you can fill", placeholder="tank, damage", required=False)

    def __init__(self, edition: str):
        super().__init__()
        self.edition = edition

    async def on_submit(self, inter: discord.Interaction):
        cfg = Ctx.cfg()

        async def go():
            actor = await build_actor(inter, "slash" if inter.guild_id else "dm")
            ed = cfg.edition(self.edition)
            parts = self.race_class.value.strip().lower().rsplit(" ", 1)
            if len(parts) != 2:
                await reply(inter, "Write race then class, e.g. `orc warrior` or `blood elf mage`.")
                return
            data = dict(edition=ed.key, name=self.name.value.strip(), race_key=parts[0].replace(" ", "_"),
                        class_key=parts[1], eligible_roles=[r for r in re.split(r"[,\s]+", self.roles.value) if r])
            place = self.place.value.strip().lower()
            if ed.uses_realms:
                data["realm_slug"] = place
            else:
                data["ruleset"] = place or ed.default_ruleset
            ch = await run_tx(lambda s: roster.add_character(s, cfg, actor, data).name)
            await reply(inter, f"Added **{ch}**. Edit details any time at {member_url(cfg, 'characters')}")
        await guarded(inter, go())


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
        await inter.response.send_modal(CreateModal())

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
                return [(c.name, Ctx.cfg().edition(c.edition).name, c.ruleset or c.realm_slug or "", c.race_key,
                         c.class_key) for c in roster.list_characters(s, a.member_id)]
            rows = await run_tx(q)
            await reply(inter, "\n".join(f"• **{n}** · {e} {p.upper() if p in ('pvp', 'rp', 'normal') else p} · "
                                         f"{r.replace('_', ' ').title()} {k.title()}" for n, e, p, r, k in rows)
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

    for cmd in (quest, character):
        cmd.allowed_contexts = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=False)
        cmd.allowed_installs = app_commands.AppInstallationType(guild=True, user=False)
        tree.add_command(cmd)
    return tree


class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True          # no privileged intents (no message content, no member list)
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
