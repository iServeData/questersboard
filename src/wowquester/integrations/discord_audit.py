"""Read-only check of the WoW category's Discord permissions and slash-command registration.

`wowquester-admin discord-permissions` fetches (GET only) the guild's roles, channels, the bot's own
member record, the registered guild commands and any command-permission overrides, then works out
the effective permissions the way Discord does (server role permissions, then the channel's
@everyone, role and member overwrites) for:

* @everyone and each configured member role (what an ordinary member gets),
* each configured capability role (officers, reviewers, trusted authors ...), held together with
  the member role as it would be in practice,
* the WoWQuester bot itself.

Each channel is checked against what WoWQuester needs from it: the event channel, the command
channels, the private review channel. Nothing is changed; the report says what to switch in Discord.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config

ADMINISTRATOR = 1 << 3
ADD_REACTIONS = 1 << 6
VIEW = 1 << 10
SEND = 1 << 11
MANAGE_MESSAGES = 1 << 13
EMBED = 1 << 14
ATTACH = 1 << 15
HISTORY = 1 << 16
USE_COMMANDS = 1 << 31
ALL = (1 << 53) - 1

NAMES = {VIEW: "View Channel", SEND: "Send Messages", EMBED: "Embed Links", HISTORY: "Read Message History",
         USE_COMMANDS: "Use Application Commands", ATTACH: "Attach Files", MANAGE_MESSAGES: "Manage Messages",
         ADD_REACTIONS: "Add Reactions"}
SHOWN = (VIEW, SEND, HISTORY, EMBED, USE_COMMANDS)
SHORT = {VIEW: "view", SEND: "send", HISTORY: "history", EMBED: "embed", USE_COMMANDS: "/cmds"}
TYPES = {0: "text", 2: "voice", 4: "category", 5: "announcement", 13: "stage", 15: "forum", 16: "media"}


@dataclass
class Subject:
    label: str
    role_ids: frozenset[str]
    user_id: str | None = None


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    fixes: int = 0
    notes: int = 0

    def ok(self, msg: str) -> None:
        self.lines.append(f"  OK    {msg}")

    def fix(self, msg: str) -> None:
        self.lines.append(f"  FIX   {msg}")
        self.fixes += 1

    def note(self, msg: str) -> None:
        self.lines.append(f"  NOTE  {msg}")
        self.notes += 1

    def head(self, msg: str) -> None:
        self.lines.append("")
        self.lines.append(msg)

    def text(self) -> str:
        return "\n".join(self.lines).strip("\n") + "\n"


def _apply(perms: int, allow: int, deny: int) -> int:
    return (perms & ~deny) | allow


def effective(guild_id: str, roles: dict[str, dict], channel: dict | None, subj: Subject) -> int:
    """Discord's permission algorithm for one subject in one channel (or server-wide if channel is None)."""
    held = set(subj.role_ids) | {guild_id}
    perms = 0
    for rid in held:
        r = roles.get(rid)
        if r is not None:
            perms |= int(r.get("permissions", 0))
    if perms & ADMINISTRATOR:
        return ALL
    if channel is None:
        return perms
    ows = {(str(o["id"]), int(o.get("type", 0))): o for o in channel.get("permission_overwrites", [])}
    ev = ows.get((guild_id, 0))
    if ev:
        perms = _apply(perms, int(ev.get("allow", 0)), int(ev.get("deny", 0)))
    allow = deny = 0
    for rid in held - {guild_id}:
        o = ows.get((rid, 0))
        if o:
            allow |= int(o.get("allow", 0))
            deny |= int(o.get("deny", 0))
    perms = _apply(perms, allow, deny)
    if subj.user_id:
        o = ows.get((subj.user_id, 1))
        if o:
            perms = _apply(perms, int(o.get("allow", 0)), int(o.get("deny", 0)))
    if not perms & VIEW:
        return 0
    if not perms & SEND:
        perms &= ~(EMBED | ATTACH)
    return perms


def _flags(p: int) -> str:
    return " ".join(SHORT[b] if p & b else "-" * len(SHORT[b]) for b in SHOWN)


def _missing(p: int, need: tuple[int, ...]) -> list[str]:
    return [NAMES[b] for b in need if not p & b]


def _synced(ch: dict, parent: dict | None) -> bool:
    if parent is None:
        return True
    key = lambda o: (str(o["id"]), int(o.get("type", 0)), str(o.get("allow", 0)), str(o.get("deny", 0)))  # noqa: E731
    return sorted(map(key, ch.get("permission_overwrites", []))) == \
        sorted(map(key, parent.get("permission_overwrites", [])))


def build(cfg: Config, guild_id: str, roles_list: list[dict], channels: list[dict], bot_member: dict,
          commands: list[dict] | None, command_perms: list[dict] | None, category_id: str | None = None) -> Report:
    roles = {str(r["id"]): r for r in roles_list}
    rname = lambda rid: "@everyone" if rid == guild_id else "@" + roles.get(rid, {}).get("name", f"unknown role {rid}")  # noqa: E731
    chans = {str(c["id"]): c for c in channels}
    d = cfg["discord"]
    event_ch = str(d.get("event_channel_id") or "")
    review_ch = str(d.get("review_channel_id") or "")
    cmd_chs = [str(x) for x in d.get("allowed_command_channel_ids", [])]
    member_roles = [str(x) for x in cfg["community"].get("member_role_ids", [])]
    rep = Report()

    # who we evaluate
    base = frozenset(member_roles)
    subjects = [Subject("@everyone", frozenset())]
    for rid in member_roles:
        subjects.append(Subject(f"member {rname(rid)}", frozenset({rid})))
    cap_roles: dict[str, list[str]] = {}
    for cap in sorted(cfg["roles"]):
        for rid in cfg["roles"][cap]:
            cap_roles.setdefault(str(rid), []).append(cap)
    for rid, caps in cap_roles.items():
        subjects.append(Subject(f"{rname(rid)} ({', '.join(caps)})", base | {rid}))
    bot_user = str((bot_member.get("user") or {}).get("id", ""))
    bot = Subject("WoWQuester bot", frozenset(str(r) for r in bot_member.get("roles", [])), bot_user)
    subjects.append(bot)
    members = subjects[1:1 + len(member_roles)] or subjects[:1]       # ordinary members
    reviewers = [Subject(rname(r), base | {r}) for r, caps in cap_roles.items()
                 if set(caps) & {"chronicle_reviewer", "chronicle_editor", "admin"}]

    # 1. slash commands
    rep.head("Slash commands")
    if commands is None:
        rep.fix("Couldn't read the registered commands (Discord refused). Re-invite the bot with the "
                "applications.commands scope.")
    elif not commands:
        rep.fix("No WoWQuester slash commands are registered in this server, so typing / shows none from "
                "WoWQuester. Registering them is the sync-commands step, which needs your approval.")
    else:
        rep.ok("Registered: " + ", ".join("/" + c.get("name", "?") for c in sorted(commands, key=lambda c: c.get("name", ""))))
    for entry in command_perms or []:
        scope = "all WoWQuester commands" if str(entry.get("id")) == str(d.get("application_id")) else \
            "/" + next((c.get("name", "?") for c in commands or [] if str(c.get("id")) == str(entry.get("id"))), "a command")
        for pm in entry.get("permissions", []):
            tgt = str(pm.get("id"))
            kind = {1: "role", 2: "user", 3: "channel"}.get(int(pm.get("type", 0)), "?")
            if kind == "channel":
                where = "every channel" if str(int(guild_id) - 1) == tgt else "#" + chans.get(tgt, {}).get("name", tgt)
            elif kind == "role":
                where = rname(tgt)
            else:
                where = f"user {tgt}"
            state = "allowed" if pm.get("permission") else "turned OFF"
            line = f"Server Settings > Integrations > WoWQuester: {scope} {state} for {where}"
            (rep.note if pm.get("permission") else rep.fix)(line)
    if cmd_chs:
        rep.ok("Commands are limited to: " + ", ".join("#" + chans.get(c, {}).get("name", c) for c in cmd_chs))
    else:
        rep.note("[discord] allowed_command_channel_ids is empty, so commands would work in any channel the "
                 "member can use.")

    # 2. pick the category
    cat = category_id or (str(chans.get(event_ch, {}).get("parent_id") or "") if event_ch else "")
    if not cat or cat not in chans:
        rep.head("WoW category")
        rep.fix("Couldn't find the WoW category. Pass --category with its ID, or check [discord] event_channel_id.")
        return rep
    category = chans[cat]
    inside = sorted((c for c in channels if str(c.get("parent_id") or "") == cat),
                    key=lambda c: (int(c.get("type", 0)) in (2, 13), c.get("position", 0)))
    rep.head(f"Category \"{category.get('name')}\" ({len(inside)} channels)")
    for subj in subjects:
        rep.lines.append(f"  {subj.label:<46} {_flags(effective(guild_id, roles, category, subj))}")

    for ch in inside:
        cid = str(ch["id"])
        kind = TYPES.get(int(ch.get("type", 0)), str(ch.get("type")))
        purpose = ("event channel" if cid == event_ch else "review channel" if cid == review_ch
                   else "command channel" if cid in cmd_chs else "")
        sync = "synced with category" if _synced(ch, category) else "own permissions"
        rep.head(f"#{ch.get('name')}  [{kind}{', ' + purpose if purpose else ''}; {sync}]")
        for subj in subjects:
            rep.lines.append(f"  {subj.label:<46} {_flags(effective(guild_id, roles, ch, subj))}")
        p = lambda s: effective(guild_id, roles, ch, s)  # noqa: E731
        if cid == event_ch:
            miss = _missing(p(bot), (VIEW, SEND, EMBED, HISTORY))
            rep.fix(f"Bot needs {', '.join(miss)} here to post and update event cards.") if miss else \
                rep.ok("Bot can post and update event cards.")
            for m in members:
                miss = _missing(p(m), (VIEW, HISTORY))
                rep.fix(f"{m.label} needs {', '.join(miss)} to see event cards.") if miss else \
                    rep.ok(f"{m.label} can see event cards and use their buttons.")
                if p(m) & SEND:
                    rep.note(f"{m.label} can also type here. Most communities keep the event channel cards-only.")
        if cid in cmd_chs or (not cmd_chs and "bot" in ch.get("name", "")):
            for m in members:
                miss = _missing(p(m), (VIEW, USE_COMMANDS))
                rep.fix(f"{m.label} needs {', '.join(miss)} here, or WoWQuester's / commands won't appear.") \
                    if miss else rep.ok(f"{m.label} can use / commands here.")
            miss = _missing(p(bot), (VIEW, SEND, EMBED))
            rep.fix(f"Bot needs {', '.join(miss)} here.") if miss else rep.ok("Bot can reply here.")
        elif "bot" in ch.get("name", "") and cmd_chs:
            rep.note("Looks like a bot channel but isn't in [discord] allowed_command_channel_ids, so commands "
                     "typed here will be refused.")
        if cid == review_ch:
            for m in members:
                if p(m) & VIEW:
                    rep.fix(f"{m.label} can see this private review channel. Deny View Channel for {m.label}.")
                else:
                    rep.ok(f"{m.label} can't see it (private).")
            for r in reviewers:
                miss = _missing(p(r), (VIEW, SEND, HISTORY))
                rep.fix(f"{r.label} needs {', '.join(miss)} here to review.") if miss else \
                    rep.ok(f"{r.label} can review here.")
            miss = _missing(p(bot), (VIEW, SEND, EMBED, HISTORY))
            rep.fix(f"Bot needs {', '.join(miss)} here to post review notices.") if miss else \
                rep.ok("Bot can post review notices.")
    for cid, label in [(event_ch, "event_channel_id"), (review_ch, "review_channel_id")] + \
            [(c, "allowed_command_channel_ids") for c in cmd_chs]:
        if cid and str(chans.get(cid, {}).get("parent_id") or "") != cat:
            rep.head(f"[discord] {label}")
            rep.note(f"#{chans.get(cid, {}).get('name', cid)} isn't in this category." if cid in chans else
                     f"Channel {cid} doesn't exist (or the bot can't see it).")
    return rep


LEGEND = ("Columns: view = View Channel, send = Send Messages, history = Read Message History, "
          "embed = Embed Links, /cmds = Use Application Commands. '-' means not allowed.")


def fetch(rest, cfg: Config) -> tuple:
    """GET-only calls. Returns the inputs `build` needs."""
    from .discord_rest import DiscordError, Forbidden, Gone
    g = cfg.guild_id
    app_id = cfg["discord"]["application_id"]
    roles_list = rest._call("GET", f"/guilds/{g}/roles")
    channels = rest._call("GET", f"/guilds/{g}/channels")
    me = rest._call("GET", "/users/@me")
    bot_member = rest._call("GET", f"/guilds/{g}/members/{me['id']}")
    try:
        commands = rest._call("GET", f"/applications/{app_id}/guilds/{g}/commands")
    except (Forbidden, Gone):
        commands = None
    try:
        command_perms = rest._call("GET", f"/applications/{app_id}/guilds/{g}/commands/permissions")
    except (Forbidden, Gone, DiscordError):
        command_perms = []
    return roles_list, channels, bot_member, commands, command_perms
