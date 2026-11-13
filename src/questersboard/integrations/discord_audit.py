"""Read-only check of the WoW category's Discord permissions and slash-command registration.

`questersboard-admin discord-permissions` fetches (GET only) the guild's roles, channels, the bot's own
member record, the registered guild commands and any command-permission overrides, then works out
the effective permissions the way Discord does (server role permissions, then the channel's
@everyone, role and member overwrites) for:

* @everyone and each configured member role (what an ordinary member gets),
* each configured capability role (officers, reviewers, trusted authors ...), held together with
  the member role as it would be in practice,
* the QuestersBoard bot itself.

Each channel is checked against what QuestersBoard needs from it: the event channel, the command
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
MANAGE_THREADS = 1 << 34
CREATE_PUBLIC_THREADS = 1 << 35
CREATE_PRIVATE_THREADS = 1 << 36
SEND_IN_THREADS = 1 << 38
ALL = (1 << 53) - 1
MANAGE_CHANNELS = 1 << 4
STREAM = 1 << 9
CONNECT = 1 << 20
SPEAK = 1 << 21
MOVE_MEMBERS = 1 << 24
USE_VAD = 1 << 25
MANAGE_ROLES = 1 << 28
CREATE_INVITE = 1 << 0
MANAGE_WEBHOOKS = 1 << 29

NAMES = {MANAGE_THREADS: "Manage Threads", CREATE_PUBLIC_THREADS: "Create Public Threads",
         CREATE_PRIVATE_THREADS: "Create Private Threads", SEND_IN_THREADS: "Send Messages in Threads",
         VIEW: "View Channel", SEND: "Send Messages", EMBED: "Embed Links", HISTORY: "Read Message History",
         USE_COMMANDS: "Use Application Commands", ATTACH: "Attach Files", MANAGE_MESSAGES: "Manage Messages",
         ADD_REACTIONS: "Add Reactions", MANAGE_CHANNELS: "Manage Channels", STREAM: "Video",
         CONNECT: "Connect", SPEAK: "Speak", MOVE_MEMBERS: "Move Members", USE_VAD: "Use Voice Activity",
         MANAGE_ROLES: "Manage Permissions", CREATE_INVITE: "Create Invite",
         MANAGE_WEBHOOKS: "Manage Webhooks"}
SHOWN = (VIEW, SEND, HISTORY, EMBED, USE_COMMANDS, SEND_IN_THREADS, CREATE_PUBLIC_THREADS, MANAGE_THREADS)
SHORT = {VIEW: "view", SEND: "send", HISTORY: "history", EMBED: "embed", USE_COMMANDS: "/cmds",
         SEND_IN_THREADS: "thread", CREATE_PUBLIC_THREADS: "+thread", MANAGE_THREADS: "mod-threads"}
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
          commands: list[dict] | None, command_perms: list[dict] | None, people: dict | None = None,
          category_id: str | None = None) -> Report:
    roles = {str(r["id"]): r for r in roles_list}
    rname = lambda rid: "@everyone" if rid == guild_id else "@" + roles.get(rid, {}).get("name", f"unknown role {rid}")  # noqa: E731
    chans = {str(c["id"]): c for c in channels}
    d = cfg["discord"]
    event_ch = str(d.get("event_channel_id") or "")
    review_ch = str(d.get("review_channel_id") or "")
    cmd_chs = [str(x) for x in d.get("allowed_command_channel_ids", [])]
    member_roles = [str(x) for x in cfg["community"].get("member_role_ids", [])]
    rep = Report()
    all_ch = str(int(guild_id) - 1)

    # who we evaluate
    base = frozenset(member_roles)
    subjects = [Subject("@everyone", frozenset())]
    for rid in member_roles:
        subjects.append(Subject(f"member {rname(rid)}", frozenset({rid})))
    cap_roles: dict[str, list[str]] = {}
    for cap in sorted(cfg["roles"]):
        for rid in cfg["roles"][cap]:
            cap_roles.setdefault(str(rid), []).append(cap)
    for rid in cap_roles:
        subjects.append(Subject(rname(rid), base | {rid}))
    bot_user = str((bot_member.get("user") or {}).get("id", ""))
    bot = Subject("QuestersBoard bot", frozenset(str(r) for r in bot_member.get("roles", [])), bot_user)
    subjects.append(bot)
    members = subjects[1:1 + len(member_roles)] or subjects[:1]       # ordinary members
    reviewers = [Subject(rname(r), base | {r}) for r, caps in cap_roles.items()
                 if set(caps) & {"chronicle_reviewer", "chronicle_editor", "admin"}]

    rep.head("QuestersBoard roles")
    for rid, caps in cap_roles.items():
        rep.lines.append(f"  {rname(rid):<30} {', '.join(caps)}")

    # 1. slash commands
    rep.head("Slash commands")
    if commands is None:
        rep.fix("Couldn't read the registered commands (Discord refused). Re-invite the bot with the "
                "applications.commands scope.")
    elif not commands:
        rep.fix("No QuestersBoard slash commands are registered in this server, so typing / shows none from "
                "QuestersBoard. Registering them is the sync-commands step, which needs your approval.")
    else:
        rep.ok("Registered: " + ", ".join("/" + c.get("name", "?") for c in sorted(commands, key=lambda c: c.get("name", ""))))
    for entry in command_perms or []:
        scope = "all QuestersBoard commands" if str(entry.get("id")) == str(d.get("application_id")) else \
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
            line = f"Server Settings > Integrations > QuestersBoard: {scope} {state} for {where}"
            expected = kind == "channel" and ((tgt == all_ch and not pm.get("permission")) or
                                              (tgt in cmd_chs and pm.get("permission")))
            (rep.ok if expected else rep.note if pm.get("permission") else rep.fix)(line)
    app_level = [pm for e in command_perms or [] if str(e.get("id")) == str(d.get("application_id"))
                 for pm in e.get("permissions", []) if int(pm.get("type", 0)) == 3]
    hidden_elsewhere = any(str(pm.get("id")) == all_ch and not pm.get("permission") for pm in app_level)
    shown_in = {str(pm.get("id")) for pm in app_level if pm.get("permission")}
    if commands and cmd_chs:
        if hidden_elsewhere and set(cmd_chs) <= shown_in:
            rep.ok("Discord only offers QuestersBoard commands in: "
                   + ", ".join("#" + chans.get(c, {}).get("name", c) for c in cmd_chs)
                   + " (members with Administrator still see them everywhere).")
        else:
            rep.note("Discord still offers QuestersBoard commands in every channel (they're refused outside the "
                     "command channel). To hide them: Server Settings > Integrations > QuestersBoard > Channels: "
                     "turn off All Channels and add " + ", ".join("#" + chans.get(c, {}).get("name", c) for c in cmd_chs)
                     + ".")
    if cmd_chs:
        rep.ok("QuestersBoard only accepts commands in: " + ", ".join("#" + chans.get(c, {}).get("name", c) for c in cmd_chs))
    else:
        rep.note("[discord] allowed_command_channel_ids is empty, so commands would work in any channel the "
                 "member can use.")

    # 1b. event voice channels
    vc = cfg.raw.get("voice") or {}
    if vc.get("enabled"):
        vcat = str(vc.get("category_id") or "")
        rep.head("Event voice channels ([voice])")
        if vcat not in chans:
            rep.fix(f"[voice] category_id {vcat} isn't a category the bot can see.")
        else:
            need = (VIEW, CONNECT, SPEAK, STREAM, USE_VAD, SEND, HISTORY, MANAGE_CHANNELS, MANAGE_ROLES, MOVE_MEMBERS)
            miss = _missing(effective(guild_id, roles, chans[vcat], bot), need)
            rep.fix(f"Bot needs {', '.join(miss)} on the \"{chans[vcat].get('name')}\" category (category "
                    "permissions only) to open voice channels, give signed-up players access and remove "
                    "people who leave.") if miss else \
                rep.ok(f"Bot can open and manage event voice channels in \"{chans[vcat].get('name')}\".")
        staff = sorted(set(vc.get("staff_role_ids", [])) |
                       (set(cfg["roles"].get("event_moderator", [])) if vc.get("moderators_can_join", True) else set()))
        rep.ok("Can join every event voice channel without signing up: "
               + (", ".join(rname(r) for r in staff) if staff else "nobody (only people signed up and hosts)"))
        if vc.get("guest_invites", True):
            # adding a guest to the server (guilds.join) needs Create Invite at server level
            if _missing(effective(guild_id, roles, None, bot), (CREATE_INVITE,)):
                rep.fix("Guest voice links need the bot's role to have Create Invite (server settings > Roles), "
                        "so guests who aren't in the server yet can be added. Or set [voice] guest_invites = false.")
            else:
                rep.ok("Bot can add guests from guest voice links to the server.")
        prot = sorted(set(vc.get("protected_role_ids", [])))
        rep.ok("Hosts can't kick or ban from event voice: the server owner, bots"
               + (", " + ", ".join(rname(r) for r in prot) if prot else "")
               + (", the staff and admin roles" if staff or cfg["roles"].get("admin") else ""))

    # 1c. item link channels
    il = cfg.raw.get("item_links") or {}
    if il.get("enabled"):
        rep.head("Item link channels ([item_links])")
        for wid in [str(x) for x in il.get("watch_channels", [])]:
            if wid not in chans:
                rep.fix(f"[item_links] watch channel {wid} doesn't exist (or the bot can't see it).")
                continue
            miss = _missing(effective(guild_id, roles, chans[wid], bot), (VIEW, HISTORY, MANAGE_WEBHOOKS))
            name = chans[wid].get("name")
            rep.fix(f"Bot needs {', '.join(miss)} in #{name} to read [item names] and post item cards.") \
                if miss else rep.ok(f"Bot can read [item names] and post item cards in #{name}.")
        post = str(il.get("post_channel") or "")
        if post:
            miss = _missing(effective(guild_id, roles, chans.get(post), bot), (VIEW, MANAGE_WEBHOOKS)) \
                if post in chans else ["View Channel"]
            rep.fix(f"Bot needs {', '.join(miss)} in the [item_links] post_channel.") if miss else \
                rep.ok("Bot can post item cards in the [item_links] post_channel.")

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
        rep.lines.append(f"  {subj.label:<30} {_flags(effective(guild_id, roles, category, subj))}")

    for ch in inside:
        cid = str(ch["id"])
        kind = TYPES.get(int(ch.get("type", 0)), str(ch.get("type")))
        purpose = ("event channel" if cid == event_ch else "review channel" if cid == review_ch
                   else "command channel" if cid in cmd_chs else "")
        sync = "synced with category" if _synced(ch, category) else "own permissions"
        rep.head(f"#{ch.get('name')}  [{kind}{', ' + purpose if purpose else ''}; {sync}]")
        for subj in subjects:
            rep.lines.append(f"  {subj.label:<30} {_flags(effective(guild_id, roles, ch, subj))}")
        p = lambda s: effective(guild_id, roles, ch, s)  # noqa: E731
        if cid == event_ch:
            miss = _missing(p(bot), (VIEW, SEND, EMBED, HISTORY))
            rep.fix(f"Bot needs {', '.join(miss)} here to post and update event cards.") if miss else \
                rep.ok("Bot can post and update event cards.")
            th = cfg.raw.get("event_threads") or {}
            if th.get("enabled"):
                private = bool(th.get("private"))
                need = (CREATE_PRIVATE_THREADS if private else CREATE_PUBLIC_THREADS, SEND_IN_THREADS,
                        MANAGE_THREADS)
                miss = _missing(p(bot), need)
                rep.fix(f"Bot needs {', '.join(miss)} here for event chat threads.") if miss else \
                    rep.ok("Bot can open, fill and close event chat threads.")
                for m in members:
                    if not p(m) & SEND_IN_THREADS:
                        rep.fix(f"{m.label} needs Send Messages in Threads here, or people who join can't "
                                "write in the event chat.")
                    if p(m) & CREATE_PUBLIC_THREADS:
                        rep.note(f"{m.label} can start their own threads on event posts (Create Public "
                                 "Threads), which anyone in the channel can write in. Deny it for "
                                 f"{m.label} if event chat should stay with the people signed up.")
                if private and int(ch.get("type", 0)) == 5:
                    rep.fix("Discord doesn't allow private threads in an Announcement channel, so invite-only "
                            "event chats can't open here. Edit Channel > Overview: turn off Announcement Channel "
                            "(the event posts stay visible to everyone; nobody new can type), or set "
                            "[event_threads] private = false.")
                if private:
                    rep.ok("Event chats are invite-only: only people signed up, the hosts and the bot "
                           "are added.")
                    mods = [s.label for s in subjects if s is not bot and p(s) & MANAGE_THREADS]
                    rep.note("Can also see and write in every invite-only event chat (Manage Threads or "
                             "Administrator): " + (", ".join(mods) if mods else "none of the roles checked")
                             + ". Server members with Administrator always can.") if mods else \
                        rep.ok("No checked role except the bot can see invite-only event chats it wasn't "
                               "added to (members with Administrator always can).")
                else:
                    rep.note("Event chats are public threads: anyone who can see this channel can read "
                             "and write in them, signed up or not. Set [event_threads] private = true to "
                             "keep them to the people signed up.")
            for m in members:
                miss = _missing(p(m), (VIEW, HISTORY))
                rep.fix(f"{m.label} needs {', '.join(miss)} to see event cards.") if miss else \
                    rep.ok(f"{m.label} can see event cards and use their buttons.")
                if p(m) & SEND:
                    rep.note(f"{m.label} can also type here. Most communities keep the event channel cards-only.")
        if cid in cmd_chs or (not cmd_chs and "bot" in ch.get("name", "")):
            for m in members:
                miss = _missing(p(m), (VIEW, USE_COMMANDS))
                rep.fix(f"{m.label} needs {', '.join(miss)} here, or QuestersBoard's / commands won't appear.") \
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
    _who_else(rep, cfg, guild_id, roles, chans, [category, *inside], member_roles, bot, people)
    return rep


def _names(people: list[str], cap: int = 25) -> str:
    people = sorted(people, key=str.lower)
    return ", ".join(people[:cap]) + (f" and {len(people) - cap} more" if len(people) > cap else "")


def _who_else(rep: Report, cfg: Config, guild_id: str, roles: dict, chans: dict, where: list[dict],
              member_roles: list[str], bot: Subject, people: dict | None) -> None:
    """Every server role (not only the ones QuestersBoard knows), and every member when the bot may list
    them: who can see the private channels, every invite-only event chat and every event voice."""
    rname = lambda rid: "@" + roles.get(rid, {}).get("name", f"unknown role {rid}")  # noqa: E731
    base = frozenset(member_roles)
    plain = [rid for rid, r in roles.items() if rid != guild_id and not r.get("managed") and rid not in bot.role_ids]
    admin_roles = [rid for rid in plain if int(roles[rid].get("permissions", 0)) & ADMINISTRATOR]
    asked = people is not None
    people = people or {}
    members = people.get("members")
    owner = str(people.get("owner_id") or "")
    humans = [m for m in members or [] if not (m.get("user") or {}).get("bot")]

    def who(m: dict) -> str:
        u = m.get("user") or {}
        return m.get("nick") or u.get("global_name") or u.get("username") or "someone"

    def held(m: dict) -> set[str]:
        return {str(r) for r in m.get("roles", [])}

    rep.head("Who else can get in (every server role" + (" and member" if members is not None else "") + ")")
    if asked and members is None:
        rep.note("Couldn't list server members (Discord refused; the bot's Server Members Intent is off), so "
                 "this shows roles only, not people.")
    if admin_roles:
        rep.note("Discord lets Administrator ignore every channel permission: anyone with it sees every WoW "
                 "channel and every invite-only event chat, and can join every event voice. Neither QuestersBoard "
                 "nor channel settings can stop it; only taking Administrator off the role can.")
        for rid in sorted(admin_roles, key=lambda r: -int(roles[r].get("position", 0))):
            if members is None:
                rep.lines.append(f"        {rname(rid)} has Administrator")
            else:
                ppl = [who(m) for m in humans if rid in held(m)]
                rep.lines.append(f"        {rname(rid)} has Administrator: {len(ppl)} "
                                 f"{'person' if len(ppl) == 1 else 'people'}" + (f" ({_names(ppl)})" if ppl else ""))
    else:
        rep.ok("No server role has Administrator.")
    if members is not None:
        own = next((who(m) for m in humans if str((m.get("user") or {}).get("id")) == owner), None)
        if own:
            rep.lines.append(f"        Server owner (always has every permission): {own}")
        admins = [m for m in humans if held(m) & set(admin_roles) or str((m.get("user") or {}).get("id")) == owner]
        rep.lines.append(f"        In total {len(admins)} of {len(humans)} people get past every channel and event "
                         "chat this way.")

    ordinary = [Subject("member", base)] if base else [Subject("@everyone", frozenset())]
    for ch in where:
        kind = int(ch.get("type", 0))
        need = VIEW | CONNECT if kind in (2, 13) else VIEW
        if any(effective(guild_id, roles, ch, s) & need == need for s in ordinary):
            continue                                    # open to members anyway
        label = ("category " if kind == 4 else "#") + str(ch.get("name"))
        verb = "join" if kind in (2, 13) else "see"
        extra = [rid for rid in plain if rid not in admin_roles and rid not in base
                 and effective(guild_id, roles, ch, Subject("", base | {rid})) & need == need]
        line = f"{label} is private. Other roles that can {verb} it: " + (", ".join(rname(r) for r in extra) if extra else "no other role")
        if members is not None:
            ppl = [who(m) for m in humans
                   if not (held(m) & set(admin_roles)) and str((m.get("user") or {}).get("id")) != owner
                   and effective(guild_id, roles, ch, Subject("", frozenset(held(m)), str((m.get("user") or {}).get("id"))))
                   & need == need]
            line += f"; {len(ppl)} {'person' if len(ppl) == 1 else 'people'} without Administrator" + \
                (f" ({_names(ppl)})" if ppl else "")
        rep.lines.append(f"        {line}.")

    ev = chans.get(str(cfg["discord"].get("event_channel_id") or ""))
    th = cfg.raw.get("event_threads") or {}
    if ev is not None and th.get("enabled") and th.get("private"):
        mods = [rid for rid in plain if rid not in admin_roles
                and effective(guild_id, roles, ev, Subject("", base | {rid})) & MANAGE_THREADS]
        (rep.note if mods else rep.ok)(
            "Invite-only event chats: besides the people signed up and the hosts, "
            + ("Manage Threads lets " + ", ".join(rname(r) for r in mods) + " open every one"
               if mods else "only Administrator can open them") + ".")
    vc = cfg.raw.get("voice") or {}
    if vc.get("enabled"):
        staff = sorted(cfg.voice_staff_roles())
        rep.ok("Event voice channels are locked: only the people signed up, the hosts"
               + (", " + ", ".join(rname(r) for r in staff) if staff else "")
               + " and Administrator can join (other roles and the category's permissions are removed).")


LEGEND = ("Columns: view = View Channel, send = Send Messages, history = Read Message History, "
          "embed = Embed Links, /cmds = Use Application Commands, thread = Send Messages in Threads, "
          "+thread = Create Public Threads, mod-threads = Manage Threads. '-' means not allowed.")


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
    people: dict = {"owner_id": None, "members": None}
    try:
        people["owner_id"] = rest._call("GET", f"/guilds/{g}").get("owner_id")
        found: list[dict] = []
        after = "0"
        for _ in range(20):                 # up to 20 000 members
            page = rest._call("GET", f"/guilds/{g}/members?limit=1000&after={after}")
            found += page
            if len(page) < 1000:
                break
            after = str(page[-1]["user"]["id"])
        people["members"] = found
    except (Forbidden, Gone, DiscordError):
        pass
    return roles_list, channels, bot_member, commands, command_perms, people
