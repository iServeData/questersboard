"""Host briefing ([host_briefing] in config.toml): a short DM with everything needed to run the event.

Hosts and co-hosts get it when the event starts (after delay_seconds, so the voice channel and chat exist
and their links can be included). An admin gets it when they press Support Admin. It covers the links,
ending the event, reopening voice, and removing someone from voice, and only lists what that person can
actually do.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from ..config import Config
from ..models import Event
from . import outbox, voice
from .timeutil import UTC, fmt_local, now

KIND = "notify.host_briefing"
WAIT_TRIES = 6           # if voice or the chat isn't ready yet, look again this many times
WAIT_SECONDS = 20


def on_start(s: Session, cfg: Config, ev: Event) -> None:
    if not cfg["host_briefing"]["enabled"]:
        return
    at = now() + timedelta(seconds=cfg["host_briefing"]["delay_seconds"])
    vc = cfg["voice"]
    for mid in sorted(voice.managers(s, ev)):
        p = {"event_id": ev.id, "member_id": mid}
        if vc["enabled"] and vc["guest_invites"]:
            p["guest"] = voice._code_for(s, ev, mid)   # made here: the worker can't create guest links
        outbox.enqueue(s, KIND, f"{ev.id}:{mid}", p, run_after=at)


def on_support_join(s: Session, cfg: Config, ev: Event, member_id: int, moderator: bool) -> None:
    hb = cfg["host_briefing"]
    if hb["enabled"] and hb["support_admin"]:
        outbox.enqueue(s, KIND, f"{ev.id}:{member_id}:support",
                       {"event_id": ev.id, "member_id": member_id, "support": True, "moderator": moderator})


def not_ready(cfg: Config, ev: Event) -> bool:
    """The event's voice channel or chat is still being made (they're opened by the worker after Start)."""
    if ev.state != "in_progress":
        return False
    waiting_voice = cfg["voice"]["enabled"] and not ev.discord_voice_channel_id and ev.voice_closed_at is None
    waiting_chat = cfg["event_threads"]["enabled"] and not ev.discord_thread_id and bool(ev.discord_message_id)
    return waiting_voice or waiting_chat


def _page(cfg: Config, ev: Event) -> str:
    base = cfg["community"]["public_base_url"].rstrip("/") + cfg["web"]["member_path"].rstrip("/")
    return f"{base}/events/{ev.public_id}"


def _where(cfg: Config) -> str:
    chans = cfg["discord"].get("allowed_command_channel_ids", [])
    return f" (type these in {', '.join(f'<#{c}>' for c in chans)})" if chans else ""


def _end_line(ev: Event) -> str:
    return f"Planned end: {fmt_local(ev.end_at, ev.timezone)} · {ev.end_at.astimezone(UTC):%H:%M} UTC"


def _links(cfg: Config, ev: Event, guest: str | None) -> list[str]:
    out = []
    if cfg["voice"]["enabled"]:
        if ev.discord_voice_channel_id:
            out.append(f"- Voice: {voice.link(cfg, ev.discord_voice_channel_id)} (only people signed up, the hosts "
                       "and Support Admins can join)")
        else:
            out.append("- Voice: opens when the event starts")
    if cfg["event_threads"]["enabled"] and ev.discord_thread_id:
        out.append(f"- Event chat: https://discord.com/channels/{cfg.guild_id}/{ev.discord_thread_id}")
    out.append(f"- Event page: {_page(cfg, ev)}")
    if guest:
        n = cfg["voice"]["max_guests_per_invite"]
        out.append(f"- Guest voice link for someone who isn't signed up: {voice.guest_url(cfg, guest)} "
                   f"(up to {n} {'person' if n == 1 else 'people'}, until the event ends)")
    return out


def _removing(cfg: Config, cohost: bool) -> list[str]:
    if not cfg["voice"]["enabled"]:
        return []
    who = ("the host, " if cohost else "") + "the server owner, bots, admins or officers"
    return ["", f"**Removing someone from voice**{_where(cfg)}",
            "- `/voice kick @name`: disconnects them. They can rejoin.",
            "- `/voice ban @name`: keeps them out until the event ends. `/voice unban @name` undoes it.",
            f"- You can't remove {who}."]


def _running(cfg: Config, support: bool = False) -> list[str]:
    if support:
        return ["", "**If the hosts need a hand**",
                "- Organizer on the event post has End event"
                + (" and, if voice closed while it was empty, Reopen voice." if cfg["voice"]["enabled"] else ".")]
    out = ["", "**Running it**",
           "- Organizer on the event post > End event when you're done. Voice closes straight away"
           + (f" and the chat {cfg['event_threads']['archive_after_hours']} hours later."
              if cfg["event_threads"]["enabled"] else ".")]
    grace = cfg["scheduling"].get("overrun_grace_minutes", 30)
    out.append(f"- Running long? {grace} minutes after the planned end you'll get a reminder to end it or extend "
               "the end time on the event page.")
    if cfg["voice"]["enabled"]:
        out.append(f"- Voice closes if it's empty {cfg['voice']['empty_teardown_minutes']} minutes after the "
                   "planned end. Organizer > Reopen voice brings it back.")
    return out


def host_text(cfg: Config, ev: Event, cohost: bool, guest: str | None) -> str:
    role = "co-hosting" if cohost else "hosting"
    lines = [f"**You're {role} {ev.title}. It has started.**", _end_line(ev), "", "**Links**",
             *_links(cfg, ev, guest), *_running(cfg), *_removing(cfg, cohost), ""]
    lines.append("When it ends you can add your story from the event page." if cohost else
                 "When it ends you'll get a link to mark attendance and write the story.")
    return "\n".join(lines)


def support_text(cfg: Config, ev: Event, moderator: bool) -> str:
    note = " The event chat shows that you joined." if cfg["support_admin"]["announce"] and \
        cfg["event_threads"]["enabled"] else ""
    lines = [f"**You're in {ev.title} as Support Admin.**",
             f"You're not signed up, so you don't take a roster spot.{note}", _end_line(ev), "", "**Links**",
             *_links(cfg, ev, None)]
    if moderator:
        lines += _running(cfg, support=True) + _removing(cfg, False)
    lines += ["", "Done helping? Organizer on the event post > Leave Support Admin."]
    return "\n".join(lines)
