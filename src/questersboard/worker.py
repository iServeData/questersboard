"""questersboard-worker: outbox delivery, reminders, overrun nudges, housekeeping.

Runs as the questersboard_worker DB role. Every side effect (Discord post, DM) comes from
an outbox row written in the same transaction as the change that caused it, so nothing
is lost on crash and nothing is sent for a change that rolled back.

Delivery is at-least-once with idempotency where Discord allows it:
* event posts: one canonical message per event, edited in place; recreated if deleted.
* DMs: one outbox row per (purpose, member, version); a DM is marked done before any
  retry could duplicate it, and reminders are guarded by reminder_deliveries' primary key.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .config import ConfigError, ConfigHolder, load
from .db import Database, make_engine
from .domain import outbox, scheduling, status, voice
from .domain.chronicle import coauthor_ids
from .domain.signups import event_requirement
from .domain.timeutil import discord_ts, fmt_local, now
from .integrations import discord_rest as dr
from .models import ChronicleEntry, ConfigChange, Event, EventCohost, Member, Signup
from .secrets import SecretError, setup_logging

log = logging.getLogger("questersboard.worker")
SUPPRESS_EMBEDS = 1 << 2      # Discord message flag: no link preview cards
MAX_ATTEMPTS = 8
BATCH = 20


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 15 * (2 ** max(0, attempts - 1))))


def log_config(s: Session, service: str, cfg, sections) -> None:
    s.add(ConfigChange(service=service, sections=list(sections),
                       hashes={k: cfg.section_hashes.get(k, "") for k in sections}, actor="file"))


def _open_spots(s: Session, ev: Event) -> str:
    """" Healer spot still open." style note for thread reminders (roles only; counts, no names)."""
    if not ev.roles:
        return ""
    taken = dict(s.execute(select(Signup.role_key, func.count()).where(
        Signup.event_id == ev.id, Signup.state == "confirmed").group_by(Signup.role_key)).all())
    open_ = [f"{r.quantity - taken.get(r.key, 0)} {r.label}" for r in ev.roles if r.quantity > taken.get(r.key, 0)]
    return f" Still open: {', '.join(open_)}." if open_ else " Every role is filled."


class Worker:
    def __init__(self, holder: ConfigHolder, db: Database, rest: dr.DiscordRest | None):
        self.holder, self.db, self.rest = holder, db, rest
        self.stop = threading.Event()
        self.started_at = now()

    @property
    def cfg(self):
        return self.holder.cfg

    # ------------------------------------------------------------------ outbox
    def claim(self) -> list[dict]:
        with self.db.tx() as s:
            rows = s.execute(text("""
                UPDATE outbox SET locked_until = now() + interval '2 minutes', attempts = attempts + 1
                 WHERE id IN (SELECT id FROM outbox
                               WHERE done_at IS NULL AND dead_at IS NULL AND run_after <= now()
                                 AND (locked_until IS NULL OR locked_until < now())
                               ORDER BY run_after LIMIT :n FOR UPDATE SKIP LOCKED)
             RETURNING id, kind, key, payload, attempts"""), {"n": BATCH}).mappings().all()
        return [dict(r) for r in rows]

    def finish(self, job: dict, error: str | None = None, dead: bool = False, retry_in: timedelta | None = None):
        with self.db.tx() as s:
            if error is None:
                # if the job was re-enqueued while we worked (attempts reset to 0), leave it for another pass
                s.execute(text("UPDATE outbox SET done_at = now(), locked_until = NULL "
                               "WHERE id = :id AND attempts = :a"), {"id": job["id"], "a": job["attempts"]})
                s.execute(text("UPDATE outbox SET locked_until = NULL WHERE id = :id AND done_at IS NULL"),
                          {"id": job["id"]})
            elif dead or job["attempts"] >= MAX_ATTEMPTS:
                s.execute(text("UPDATE outbox SET dead_at = now(), last_error = :e, locked_until = NULL "
                               "WHERE id = :id"), {"id": job["id"], "e": error[:500]})
                log.error("outbox job %s %s gave up: %s", job["kind"], job["key"], error)
            else:
                s.execute(text("UPDATE outbox SET run_after = :ra, last_error = :e, locked_until = NULL "
                               "WHERE id = :id"),
                          {"id": job["id"], "e": error[:500], "ra": now() + (retry_in or backoff(job["attempts"]))})

    def run_job(self, job: dict) -> None:
        kind = job["kind"]
        handler = getattr(self, "job_" + kind.replace(".", "_"), None)
        if handler is None:
            self.finish(job, f"unknown job kind {kind}", dead=True)
            return
        try:
            with self.db.tx() as s:
                handler(s, job["payload"] or {})
        except dr.RateLimited as exc:
            self.finish(job, str(exc), retry_in=timedelta(seconds=exc.retry_after + 0.5))
        except dr.Forbidden as exc:
            self.finish(job, str(exc), dead=True)
        except (dr.Transient, dr.DiscordError) as exc:
            self.finish(job, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - keep the loop alive; error recorded on the job
            log.exception("job %s failed", kind)
            self.finish(job, f"{type(exc).__name__}: {exc}")
        else:
            self.finish(job)

    def tick(self) -> int:
        jobs = self.claim()
        for j in jobs:
            self.run_job(j)
        return len(jobs)

    # ------------------------------------------------------------------ helpers
    def _need_rest(self):
        if self.rest is None:
            raise dr.Transient("Discord bot token not installed")
        return self.rest

    _emoji_cache: tuple[float, dict] = (0.0, {})

    def _role_emojis(self, rest) -> dict[str, str]:
        """The bot's own emoji names -> ids, looked up at most every 30 minutes (5 after a failure).
        Any problem just means plain-text roles; it never stops a post."""
        import time
        if not self.cfg["event_posts"].get("role_icons") or not self.cfg["discord"].get("application_id"):
            return {}
        at, cached = self._emoji_cache
        if time.monotonic() < at:
            return cached
        try:
            cached, wait = rest.app_emojis(self.cfg["discord"]["application_id"]), 1800
        except Exception as exc:  # noqa: BLE001
            log.info("role icons unavailable (%s); posting plain role names", type(exc).__name__)
            wait = 300
        self._emoji_cache = (time.monotonic() + wait, cached)
        return cached

    def _url(self, *parts: str) -> str:
        base = self.cfg["community"]["public_base_url"].rstrip("/") + self.cfg["web"]["member_path"].rstrip("/")
        return "/".join([base, *parts])

    def _dm(self, s: Session, member_ids, purpose: str, version, content: str) -> None:
        for mid in sorted(set(member_ids)):
            outbox.enqueue(s, "dm", f"{purpose}:{version}:{mid}", {"member_id": mid, "content": content[:1900]})

    def _managers(self, s: Session, ev: Event) -> set[int]:
        return {ev.creator_id} | set(s.scalars(select(EventCohost.member_id).where(EventCohost.event_id == ev.id)))

    # ------------------------------------------------------------------ jobs
    def job_discord_render_event(self, s: Session, p: dict) -> None:
        from .bot.render import event_message
        ev = s.get(Event, p["event_id"])
        if ev is None or ev.state == "draft":
            return
        voice.request_sync(s, self.cfg, ev)
        channel = ev.discord_channel_id or self.cfg["discord"].get("event_channel_id")
        if not channel:
            log.info("event %s: no event channel configured; post skipped", ev.id)
            return
        rest = self._need_rest()
        payload = event_message(s, self.cfg, ev, self._role_emojis(rest))
        if self.cfg["event_threads"]["enabled"]:
            outbox.enqueue(s, "discord.sync_thread", str(ev.id), {"event_id": ev.id})
        if ev.discord_message_id:
            try:
                rest.edit(channel, ev.discord_message_id, payload)
                return
            except dr.Gone:
                log.warning("event %s: post was deleted; reposting", ev.id)
        mid = rest.post(channel, payload)
        s.execute(text("UPDATE events SET discord_message_id = :m, discord_channel_id = :c WHERE id = :id"),
                  {"m": mid, "c": channel, "id": ev.id})

    # ------------------------------------------------------------------ event chat threads
    def job_discord_sync_thread(self, s: Session, p: dict) -> None:
        """One chat thread per event ([event_threads] enabled = true).

        Opened once the post exists; signed-up players (confirmed, tentative, waitlisted) and hosts are
        added, people who leave are removed. When the event ends or is cancelled, the thread is locked
        and archived archive_after_hours later. Runs after every post render, so it self-heals.
        """
        th = self.cfg["event_threads"]
        ev = s.get(Event, p["event_id"], with_for_update=True)
        if not th["enabled"] or ev is None or not ev.discord_channel_id:
            return
        rest = self._need_rest()
        if ev.state in ("completed", "cancelled"):
            if ev.discord_thread_id:
                ended = (ev.actual_end or ev.end_at) if ev.state == "completed" else now()
                outbox.enqueue(s, "discord.close_thread", str(ev.id), {"event_id": ev.id},
                               run_after=max(now(), ended + timedelta(hours=th["archive_after_hours"])))
            return
        if ev.state not in ("scheduled", "in_progress"):
            return
        if not ev.discord_thread_id:
            name = ev.title[:100 - len(th["name_suffix"])] + th["name_suffix"]
            ev.discord_thread_id = rest.start_thread(ev.discord_channel_id, ev.discord_message_id, name,
                                                     th["private"])
            ev.discord_thread_members = []
            s.flush()
            rest.post(ev.discord_thread_id, {"content": (
                f"Chat for people signed up to **{ev.title}**. Plan here so the main channels stay quiet. "
                f"Joining the event adds you; leaving removes you. This thread closes "
                f"{th['archive_after_hours']} hours after the event.")})
        want = set(s.scalars(select(Signup.member_id).where(
            Signup.event_id == ev.id, Signup.state.in_(("confirmed", "tentative", "waitlisted"))))) | \
            self._managers(s, ev)
        ids = dict(s.execute(select(Member.id, Member.discord_id).where(
            Member.id.in_(want | set(ev.discord_thread_members or [])), Member.erased_at.is_(None))).all())
        have = set(ev.discord_thread_members or [])
        for mid in sorted(want - have):
            if mid in ids:
                try:
                    rest.add_thread_member(ev.discord_thread_id, ids[mid])
                except dr.Gone:
                    log.info("member %s not reachable for the thread; skipped", mid)
        for mid in sorted(have - want):
            if mid in ids:
                rest.remove_thread_member(ev.discord_thread_id, ids[mid])
        ev.discord_thread_members = sorted(m for m in want if m in ids)

    def job_discord_close_thread(self, s: Session, p: dict) -> None:
        ev = s.get(Event, p["event_id"])
        if ev is None or not ev.discord_thread_id or ev.state not in ("completed", "cancelled"):
            return
        self._need_rest().close_thread(ev.discord_thread_id)

    def job_discord_thread_message(self, s: Session, p: dict) -> None:
        ev = s.get(Event, p["event_id"])
        if ev is None or not ev.discord_thread_id or ev.state not in ("scheduled", "in_progress"):
            return
        self._need_rest().post(ev.discord_thread_id, {"content": p["content"][:1900]})

    # ------------------------------------------------------------------ event voice channels (FU-1)
    def _voice_gone(self, s: Session, ev: Event) -> None:
        ev.discord_voice_channel_id = None
        ev.discord_voice_members = {}
        ev.voice_empty_since = None

    def job_discord_sync_voice(self, s: Session, p: dict) -> None:
        """Open, fill and remove the event's voice channel ([voice] enabled). Runs after every post render
        and every signup change, so it self-heals after a restart."""
        vc = self.cfg["voice"]
        ev = s.get(Event, p["event_id"], with_for_update=True)
        if ev is None:
            return
        if ev.state in ("completed", "cancelled"):
            if ev.discord_voice_channel_id:
                self._need_rest().delete_channel(ev.discord_voice_channel_id)
                self._voice_gone(s, ev)
            voice.end_session(s, ev)        # guest links, guests and bans go with the event
            return
        if not vc["enabled"] or ev.state != "in_progress":
            return
        rest, guild = self._need_rest(), self.cfg.guild_id
        if not ev.discord_voice_channel_id:
            if ev.voice_closed_at is not None:          # removed for being empty; a host can reopen it
                return
            made = rest.create_voice_channel(guild, vc["category_id"], voice.channel_name(self.cfg, ev),
                                             vc["user_limit"])
            new_ch = str(made["id"])
            try:
                self._lock_voice(rest, new_ch, made.get("permission_overwrites") or [])
            except dr.DiscordError:
                rest.delete_channel(new_ch)         # never leave an open channel behind
                raise
            ev.discord_voice_channel_id = new_ch
            ev.discord_voice_members = {}
            ev.voice_empty_since = now()
            s.flush()
            # Discord can apply permission changes on a brand-new channel out of order; check again shortly
            outbox.enqueue(s, "discord.sync_voice", f"{ev.id}:relock", {"event_id": ev.id, "relock": True},
                           run_after=now() + timedelta(seconds=5))
            outbox.enqueue(s, "discord.render_event", str(ev.id), {"event_id": ev.id})   # adds Join voice
        ch = ev.discord_voice_channel_id
        tkey = f"voice-open:{ev.id}:{ch}"
        if self.cfg["event_threads"]["enabled"] and ev.discord_thread_id and not s.scalar(
                text("SELECT 1 FROM outbox WHERE kind = 'discord.thread_message' AND key = :k"), {"k": tkey}):
            # once per channel, as soon as both the thread and the channel exist
            outbox.enqueue(s, "discord.thread_message", tkey, {"event_id": ev.id, "content": (
                f"Voice is open: <#{ch}>. Everyone signed up can join; use **Join voice** on the event post.")})
        have = ev.discord_voice_members if isinstance(ev.discord_voice_members, dict) else {}
        ids = voice.discord_ids(s, voice.wanted_members(s, self.cfg, ev))
        want = voice.desired_overwrites(s, self.cfg, ev, ids, have)
        staff = self.cfg.voice_staff_roles()
        done = dict(have)
        compare = have
        try:
            if p.get("relock"):
                self._lock_voice(rest, ch, rest.channel(ch).get("permission_overwrites") or [], keep=have)
                compare = {}                        # set every member's permission again
            for did, st in sorted(want.items()):
                if compare.get(did) != st:
                    allow, deny = voice.OVERWRITE[st]
                    rest.set_overwrite(ch, did, 1, allow, deny)
                    done[did] = st
            for did in sorted(set(have) - set(want)):
                # only with remove_on_leave = true: take the seat away and disconnect (staff may stay)
                rest.delete_overwrite(ch, did)
                done.pop(did, None)
                if rest.voice_channel_of(guild, did) == ch:
                    _, roles, _ = rest.member(guild, did)
                    if not roles & staff:
                        rest.disconnect_voice(guild, did)
        except dr.Gone:
            # someone deleted the channel in Discord: treat it as closed (a host can reopen it)
            log.warning("event %s: voice channel was deleted in Discord", ev.id)
            self._voice_gone(s, ev)
            ev.voice_closed_at = now()
            outbox.enqueue(s, "discord.render_event", str(ev.id), {"event_id": ev.id})
            return
        ev.discord_voice_members = done

    def _lock_voice(self, rest, channel_id: str, current: list[dict], keep=()) -> None:
        _, bot_roles, _ = rest.member(self.cfg.guild_id, rest.me())
        for op in voice.lock_ops(self.cfg, rest.me(), bot_roles, current, keep):
            if op[0] == "put":
                rest.set_overwrite(channel_id, op[1], op[2], op[3], op[4])
            else:
                rest.delete_overwrite(channel_id, op[1])

    def job_discord_voice_kick(self, s: Session, p: dict) -> None:
        """A host kicked or banned someone: disconnect them if they're in this event's channel."""
        ev = s.get(Event, p["event_id"])
        if ev is None or not ev.discord_voice_channel_id:
            return
        rest = self._need_rest()
        if rest.voice_channel_of(self.cfg.guild_id, p["discord_id"]) == ev.discord_voice_channel_id:
            rest.disconnect_voice(self.cfg.guild_id, p["discord_id"])

    def job_discord_close_voice(self, s: Session, p: dict) -> None:
        """Empty for empty_teardown_minutes after the scheduled end: remove the channel, keep the event."""
        ev = s.get(Event, p["event_id"], with_for_update=True)
        if ev is None or not ev.discord_voice_channel_id or ev.id not in voice.due_for_teardown(s, self.cfg) \
                or not voice.bot_watching(s, self.cfg):
            return
        self._need_rest().delete_channel(ev.discord_voice_channel_id)
        self._voice_gone(s, ev)
        ev.voice_closed_at = now()
        outbox.enqueue(s, "discord.render_event", str(ev.id), {"event_id": ev.id})
        if self.cfg["event_threads"]["enabled"] and ev.discord_thread_id:
            outbox.enqueue(s, "discord.thread_message", f"voice-closed:{ev.id}:{int(now().timestamp())}",
                           {"event_id": ev.id, "content": "The voice channel was empty, so it was removed. "
                            "The event is still running; a host can reopen voice from **Organizer**."})

    def scan_voice(self) -> int:
        if not self.cfg["voice"]["enabled"]:
            return 0
        with self.db.tx() as s:
            if not voice.bot_watching(s, self.cfg):    # no fresh head counts: never guess a channel is empty
                return 0
            due = voice.due_for_teardown(s, self.cfg)
            for eid in due:
                outbox.enqueue(s, "discord.close_voice", str(eid), {"event_id": eid})
        return len(due)

    # ------------------------------------------------------------------ item cards ([item_links])
    _hooks: dict[str, dict] = {}

    @property
    def bz(self):
        if getattr(self, "_bz", None) is None:
            from .integrations.blizzard import Blizzard
            self._bz = Blizzard(self.cfg)
        return self._bz

    @bz.setter
    def bz(self, v):
        self._bz = v

    def _item_hook(self, rest, channel_id: str, fresh: bool = False) -> dict:
        """This application's item-card webhook in the channel: found again on Discord, or created.
        Kept in memory only (never stored or logged)."""
        name = self.cfg["item_links"]["webhook_name"]
        if fresh or channel_id not in self._hooks:
            self._hooks = {**self._hooks, channel_id: rest.own_webhook(channel_id, name)
                           or rest.create_webhook(channel_id, name)}
        return self._hooks[channel_id]

    def _item_message(self, s: Session, p: dict) -> tuple[dict | None, str]:
        """The card message for one player message (payload: channel_id, message_id, names) and where it goes."""
        from .domain import item_links
        il = self.cfg["item_links"]
        hours = self.cfg["blizzard"].get("cache_hours_static", 168)
        items, missing = [], []
        for name in (p.get("names") or [])[:il["max_per_message"]]:
            got = item_links.find(s, self.bz, self.cfg, hours, name)
            (items.append(got) if got else missing.append(name))
        target = il["post_channel"] or p["channel_id"]
        guild = self.cfg["community"]["guild_id"]
        jump = f"https://discord.com/channels/{guild}/{p['channel_id']}/{p['message_id']}" \
            if guild and target != p["channel_id"] else ""          # same channel: the card is right below
        return item_links.message(items, missing, jump, il["say_not_found"], il["not_found_text"]), target

    def job_item_links_post(self, s: Session, p: dict) -> None:
        """Cards for the [bracketed] names in one message. Payload: channel_id, message_id, names."""
        il = self.cfg["item_links"]
        if not il["enabled"]:
            return
        rest = self._need_rest()
        msg, target = self._item_message(s, p)
        if msg is None:
            return
        msg["username"] = il["webhook_name"]
        avatar = il["avatar_url"] or rest.avatar_url()
        if avatar:
            msg["avatar_url"] = avatar
        try:
            card_id = rest.execute_webhook(self._item_hook(rest, target), msg)
        except dr.Gone:                         # someone deleted the webhook: make a new one, once
            card_id = rest.execute_webhook(self._item_hook(rest, target, fresh=True), msg)
        s.execute(text("UPDATE outbox SET payload = payload || CAST(:extra AS jsonb) "
                       "WHERE kind = 'item_links.post' AND key = :k AND done_at IS NULL"),
                  {"k": f"{p['channel_id']}:{p['message_id']}",
                   "extra": json.dumps({"card_id": card_id, "card_channel": target})})

    def job_item_links_refresh(self, s: Session, p: dict) -> None:
        """Look the names up again and edit the card already posted, in place (after an upgrade or once
        Blizzard adds an item). Nothing new is posted; a card that's gone stays gone. Payload: key."""
        if not self.cfg["item_links"]["enabled"]:
            return
        row = s.execute(text("SELECT payload FROM outbox WHERE kind = 'item_links.post' AND key = :k "
                             "AND done_at IS NOT NULL ORDER BY id DESC LIMIT 1"), {"k": p["key"]}).scalar()
        if not row or not row.get("card_id") or not row.get("card_channel"):
            return
        msg, _ = self._item_message(s, row)
        if msg is None:                          # nothing to say any more: leave the old card alone
            return
        msg.setdefault("content", "")            # an edit keeps what it doesn't mention: clear old lines
        msg.setdefault("embeds", [])
        rest = self._need_rest()
        try:
            rest.edit_webhook_message(self._item_hook(rest, row["card_channel"]), row["card_id"], msg)
        except dr.Gone:
            log.info("item card %s is no longer there; not refreshed", row["card_id"])

    def job_item_links_delete(self, s: Session, p: dict) -> None:
        """The player deleted their message: remove its cards too (while the delivery record is kept)."""
        key = f"{p['channel_id']}:{p['message_id']}"
        row = s.execute(text("SELECT id, payload, done_at FROM outbox WHERE kind = 'item_links.post' AND key = :k "
                             "ORDER BY id DESC LIMIT 1"), {"k": key}).mappings().first()
        if row is None:
            return
        if row["done_at"] is None:              # not posted yet: just don't
            s.execute(text("UPDATE outbox SET done_at = now(), last_error = 'message deleted' WHERE id = :id"),
                      {"id": row["id"]})
            return
        card, where = (row["payload"] or {}).get("card_id"), (row["payload"] or {}).get("card_channel")
        if not card or not where:
            return
        rest = self._need_rest()
        try:
            rest.delete_webhook_message(self._item_hook(rest, where), card)
        except dr.Gone:
            pass

    def job_dm(self, s: Session, p: dict) -> None:
        if not self.cfg["discord"].get("dm_enabled", True):
            return
        m = s.get(Member, p["member_id"])
        if m is None or m.erased_at:
            return
        try:
            # flags=4 (SUPPRESS_EMBEDS): our links point at sign-in pages, which Discord would otherwise
            # unfurl into a generic "Discord" preview card. The link itself still works.
            self._need_rest().dm(m.discord_id, {"content": p["content"], "flags": SUPPRESS_EMBEDS})
        except dr.Forbidden:
            log.info("member %s has DMs closed; skipped", m.id)   # not retried: user choice
        except dr.Gone:
            log.info("member %s not reachable; skipped", m.id)

    def job_notify_material_change(self, s: Session, p: dict) -> None:
        if not self.cfg["discord"].get("notify_dm_on_material_change", True):
            return
        ev = s.get(Event, p["event_id"])
        if ev is None:
            return
        people = set(s.scalars(select(Signup.member_id).where(
            Signup.event_id == ev.id, Signup.state.in_(("confirmed", "tentative", "waitlisted", "pending")))))
        people |= self._managers(s, ev)
        if "cancelled" in p.get("changes", []):
            msg = f"**{ev.title}** on {fmt_local(ev.start_at, ev.timezone)} was cancelled."
        else:
            words = {"time": "time", "location": "location", "requirements": "who can join"}
            msg = (f"**{ev.title}** changed ({', '.join(words.get(c, c) for c in p.get('changes', []))}). "
                   f"It now starts {discord_ts(ev.start_at, 'F')}.")
            if "requirements" in p.get("changes", []):
                msg += f" It's now for {event_requirement(self.cfg, ev)}."
            repick = set(p.get("repick", []))
            for mid in repick:
                self._dm(s, [mid], f"repick:{ev.id}", ev.version,
                         msg + " Your signed-up character no longer fits, so you're now tentative. "
                         f"Pick another character to stay signed up, or leave: "
                         f"{self._url('events', str(ev.public_id))}")
            people -= repick
            bumped = set(p.get("bumped", []))
            for mid in bumped:
                self._dm(s, [mid], f"bumped:{ev.id}", ev.version,
                         msg + " You have another WoW event at the new time, so you're now tentative. "
                         f"Reconfirm or leave: {self._url('events', str(ev.public_id))}")
            people -= bumped
            if p.get("reconfirm", self.cfg["scheduling"].get("reschedule_requires_reconfirm", True)
                     and "time" in p.get("changes", [])):
                msg += f" Please reconfirm: {self._url('events', str(ev.public_id))}"
        self._dm(s, people, f"change:{ev.id}", ev.version, msg)

    def job_notify_signup_pending(self, s: Session, p: dict) -> None:
        ev = s.get(Event, p["event_id"])
        if ev:
            self._dm(s, self._managers(s, ev), f"pending:{ev.id}", p["signup_id"],
                     f"New signup waiting for approval on **{ev.title}**: {self._url('events', str(ev.public_id))}")

    def job_notify_slot_open(self, s: Session, p: dict) -> None:
        ev = s.get(Event, p["event_id"])
        if ev:
            self._dm(s, self._managers(s, ev), f"slot:{ev.id}", int(time.time() // 600),
                     f"A spot opened on **{ev.title}** and people are waitlisted: "
                     f"{self._url('events', str(ev.public_id))}")

    def job_notify_signup_decision(self, s: Session, p: dict) -> None:
        su = s.get(Signup, p["signup_id"])
        if su is None:
            return
        ev = s.get(Event, su.event_id)
        words = {"confirmed": "You're confirmed for", "promoted": "A spot opened: you're now confirmed for",
                 "rejected": "Your signup wasn't accepted for"}
        self._dm(s, [su.member_id], f"decision:{su.id}", p["decision"],
                 f"{words.get(p['decision'], 'Update on')} **{ev.title}** ({discord_ts(ev.start_at, 'F')}).")

    def job_notify_chronicle_prompt(self, s: Session, p: dict) -> None:
        ev = s.get(Event, p["event_id"])
        e = s.scalar(select(ChronicleEntry).where(ChronicleEntry.event_id == p["event_id"])
                     .order_by(ChronicleEntry.id).limit(1))
        if ev and e:
            managers = self._managers(s, ev)
            self._dm(s, [ev.creator_id], f"chron:{e.id}", 1,
                     f"**{ev.title}** is done. Mark attendance and tell the story (what you set out to do, what "
                     f"happened, the outcome, best moments, screenshots): {self._url('chronicle', str(e.id))}")
            # co-hosts and everyone who took part can add their own story to the same chronicle page
            link = self._url("events", str(ev.public_id))
            self._dm(s, managers - {ev.creator_id}, f"chron-cohost:{ev.id}", 1,
                     f"**{ev.title}** is done. Add your story as co-host from the event page: {link}")
            people = set(s.scalars(select(Signup.member_id).where(
                Signup.event_id == ev.id, Signup.state.in_(("confirmed", "tentative"))))) - managers
            self._dm(s, people, f"chron-attendee:{ev.id}", 1,
                     f"Thanks for joining **{ev.title}**. Want to tell your side of it? Write your story from the "
                     f"event page; it's reviewed before it appears in the chronicle: {link}")

    def job_notify_event_edited(self, s: Session, p: dict) -> None:
        ev = s.get(Event, p["event_id"])
        if ev is None:
            return
        who = s.get(Member, p.get("actor_id")) if p.get("actor_id") else None
        name = f"<@{who.discord_id}>" if who and not who.erased_at else "A host"
        changes = ", ".join(p.get("changes", [])) or "details"
        self._dm(s, self._managers(s, ev) - {p.get("actor_id")}, f"edited:{ev.id}", ev.version,
                 f"{name} changed **{ev.title}** ({changes}): {self._url('events', str(ev.public_id))}")

    def job_notify_pick_role(self, s: Session, p: dict) -> None:
        ev = s.get(Event, p["event_id"])
        if ev is None:
            return
        ids = p.get("signup_ids", [])
        people = set(s.scalars(select(Signup.member_id).where(Signup.id.in_(ids)))) if ids else set()
        roles = ", ".join(r.label for r in sorted(ev.roles, key=lambda r: r.position))
        self._dm(s, people, f"pickrole:{ev.id}", ev.version,
                 f"**{ev.title}** now has group roles ({roles}). Pick yours so the host can balance the group: "
                 f"{self._url('events', str(ev.public_id))}")

    def job_notify_chronicle_coauthor(self, s: Session, p: dict) -> None:
        e = s.get(ChronicleEntry, p["entry_id"])
        if e is None:
            return
        ev = s.get(Event, e.event_id)
        self._dm(s, p.get("member_ids", []), f"coauthor:{e.id}", int(time.time() // 60),
                 f"You were added as a co-author of a **{ev.title}** story. You can edit it here: "
                 f"{self._url('chronicle', str(e.id))}")

    def job_notify_chronicle_review(self, s: Session, p: dict) -> None:
        ch = self.cfg["discord"].get("review_channel_id")
        if not ch:
            return
        note = "privacy update: screenshots need re-clearing" if p.get("privacy") else "a story is waiting"
        self._need_rest().post(ch, {"content": f"Chronicle review: {note}. {self._url('review')}",
                                    "flags": SUPPRESS_EMBEDS})

    def job_notify_publish_request(self, s: Session, p: dict) -> None:
        """A member without the event-creator role sent a draft for publishing: a short notice for staff."""
        ch = self.cfg["discord"].get("publish_request_channel_id") or self.cfg["discord"].get("review_channel_id")
        ev = s.get(Event, p["event_id"])
        if not ch or ev is None or ev.state != "draft" or ev.publish_requested_at is None:
            return
        owner = s.get(Member, ev.creator_id)
        who = (owner.display_name or owner.username) if owner else "a member"
        self._need_rest().post(ch, {"content": (
            f"Event waiting to be published: **{ev.title}** by {who}, <t:{int(ev.start_at.timestamp())}:f>. "
            f"Check it and press Publish: {self._url('events', str(ev.public_id))}"), "flags": SUPPRESS_EMBEDS})

    def job_notify_draft_published(self, s: Session, p: dict) -> None:
        ev = s.get(Event, p["event_id"])
        if ev is None or ev.state not in ("scheduled", "in_progress"):
            return
        self._dm(s, [ev.creator_id], f"published:{ev.id}", ev.version,
                 f"Your event **{ev.title}** was published by a moderator and is now open for signups: "
                 f"{self._url('events', str(ev.public_id))}")

    def job_notify_chronicle_decision(self, s: Session, p: dict) -> None:
        e = s.get(ChronicleEntry, p["entry_id"])
        if e:
            ev = s.get(Event, e.event_id)
            words = {"published": "is published", "changes_requested": "needs changes",
                     "rejected": "wasn't approved", "unpublished": "was taken down"}
            note = (p.get("note") or "").strip()
            why = f" Reason: {note}" if note and p["decision"] == "unpublished" else (
                f" Reviewer note: {note}" if note else "")
            self._dm(s, [e.author_id, *coauthor_ids(s, e.id)], f"chron-decision:{e.id}",
                     p["decision"] + str(e.updated_at.timestamp()),
                     f"Your **{ev.title}** story {words.get(p['decision'], 'was updated')}.{why} "
                     f"{self._url('chronicle', str(e.id))}")

    # ------------------------------------------------------------------ scans
    def scan_reminders(self) -> int:
        offsets = self.cfg["reminders"].get("offsets_minutes", [])
        if not offsets:
            return 0
        n = 0
        with self.db.tx() as s:
            horizon = now() + timedelta(minutes=max(offsets))
            for ev in s.scalars(select(Event).where(Event.state == "scheduled", Event.start_at > now(),
                                                    Event.start_at <= horizon)):
                for off in offsets:
                    due = ev.start_at - timedelta(minutes=off)
                    if not (due <= now() < due + timedelta(minutes=15)):
                        continue
                    th = self.cfg["event_threads"]
                    tkey = f"remind:{ev.id}:{off}:{int(ev.start_at.timestamp())}"
                    if th["enabled"] and th["reminders_in_thread"] and ev.discord_thread_id and not s.scalar(
                            text("SELECT 1 FROM outbox WHERE kind = 'discord.thread_message' AND key = :k"),
                            {"k": tkey}):
                        outbox.enqueue(s, "discord.thread_message", tkey,
                                       {"event_id": ev.id, "content": f"**{ev.title}** starts "
                                        f"{discord_ts(ev.start_at, 'R')}." + _open_spots(s, ev)})
                    people = set(s.scalars(select(Signup.member_id).where(
                        Signup.event_id == ev.id, Signup.state.in_(("confirmed", "tentative"))))) | \
                        self._managers(s, ev)
                    for mid in people:
                        ins = s.execute(text(
                            "INSERT INTO reminder_deliveries (event_id, member_id, offset_minutes, start_at) "
                            "VALUES (:e, :m, :o, :st) ON CONFLICT DO NOTHING RETURNING event_id"),
                            {"e": ev.id, "m": mid, "o": off, "st": ev.start_at}).first()
                        if ins:
                            outbox.enqueue(s, "dm", f"remind:{ev.id}:{off}:{int(ev.start_at.timestamp())}:{mid}",
                                           {"member_id": mid, "content": f"Reminder: **{ev.title}** starts "
                                            f"{discord_ts(ev.start_at, 'R')}."
                                            + (f" Meet at {ev.muster_location['label']}."
                                               if (ev.muster_location or {}).get("label") else "")})
                            n += 1
        return n

    def scan_overruns(self) -> int:
        """Overrun policy: the reservation keeps the planned end; organizers are nudged once to End or Extend.

        Nothing is extended automatically (that could silently block someone else's event).
        """
        grace = timedelta(minutes=self.cfg["scheduling"].get("overrun_grace_minutes", 30))
        n = 0
        with self.db.tx() as s:
            for ev in s.scalars(select(Event).where(Event.state == "in_progress", Event.end_at + grace < now())):
                ins = s.execute(text(
                    "INSERT INTO reminder_deliveries (event_id, member_id, offset_minutes, start_at) "
                    "VALUES (:e, :m, -1, :st) ON CONFLICT DO NOTHING RETURNING event_id"),
                    {"e": ev.id, "m": ev.creator_id, "st": ev.end_at}).first()
                if ins:
                    self._dm(s, self._managers(s, ev), f"overrun:{ev.id}", int(ev.end_at.timestamp()),
                             f"**{ev.title}** is past its planned end. End it, or extend the end time "
                             f"(the extension is checked for conflicts): {self._url('events', str(ev.public_id))}")
                    n += 1
        return n

    def housekeeping(self) -> None:
        r = self.cfg["retention"]
        with self.db.tx() as s:
            s.execute(text("DELETE FROM outbox WHERE done_at < now() - make_interval(days => :d)"),
                      {"d": r["outbox_done_days"]})
            s.execute(text("DELETE FROM api_cache WHERE expires_at < now() - make_interval(days => :d)"),
                      {"d": r["api_cache_days"]})
            self._blizzard_ttl(s)
            s.execute(text("DELETE FROM rate_buckets WHERE window_start < now() - interval '1 day'"))
            # guest voice links, guests and bans never outlive their event (End/Cancel already removes them)
            for tbl in ("event_voice_invites", "event_voice_guests", "event_voice_bans"):
                s.execute(text(f"DELETE FROM {tbl} t USING events e WHERE e.id = t.event_id "
                               "AND e.state <> 'in_progress'"))

    def _blizzard_ttl(self, s) -> None:
        """Blizzard's API terms: data from their API is kept no longer than 30 days.
        - Battle.net character lists are only needed while the member picks characters to add, so they go
          once battlenet_import_minutes has passed.
        - A Blizzard check (api_matched / ownership_verified) not refreshed within data_ttl_days is
          cleared: the character stays on the roster as the member entered it, marked self-reported,
          until they sync or check again.
        - Cached API responses older than data_ttl_days are deleted whatever their cache time.
        - Imported character details (character_profiles) older than data_ttl_days are deleted."""
        bz = self.cfg["blizzard"]
        days = min(int(bz.get("data_ttl_days", 30)), 30)
        s.execute(text("DELETE FROM battlenet_links WHERE linked_at < now() - make_interval(mins => :m)"),
                  {"m": int(bz.get("battlenet_import_minutes", 60))})
        s.execute(text(
            "UPDATE characters SET verification = 'self_reported', api_external_id = NULL, api_namespace = NULL, "
            "api_refreshed_at = NULL, updated_at = now() WHERE verification <> 'self_reported' "
            "AND (api_refreshed_at IS NULL OR api_refreshed_at < now() - make_interval(days => :d))"), {"d": days})
        s.execute(text("DELETE FROM api_cache WHERE fetched_at < now() - make_interval(days => :d)"), {"d": days})
        s.execute(text("DELETE FROM character_profiles WHERE fetched_at < now() - make_interval(days => :d)"),
                  {"d": days})

    def sync_policy(self) -> None:
        with self.db.tx() as s:
            scheduling.sync_policy(s, self.cfg.max_simultaneous)

    # ------------------------------------------------------------------ loop
    def heartbeat(self) -> None:
        """Check in for the status card. Never blocks delivery: a failure (e.g. the table is not
        migrated yet) is logged and the loop carries on."""
        try:
            with self.db.tx() as s:
                status.beat(s, "worker", "running", self.started_at)
        except Exception:  # noqa: BLE001
            log.warning("status heartbeat failed", exc_info=True)

    def run(self) -> None:
        last_scan = last_house = last_beat = 0.0
        self.sync_policy()
        while not self.stop.is_set():
            live = self.holder.check()
            if live:
                with self.db.tx() as s:
                    log_config(s, "worker", self.cfg, live)
                if "scheduling" in live:
                    self.sync_policy()
            try:
                busy = self.tick()
                t = time.monotonic()
                if t - last_beat >= self.cfg["status"]["heartbeat_seconds"]:
                    self.heartbeat()
                    last_beat = t
                if t - last_scan > 30:
                    self.scan_reminders()
                    self.scan_overruns()
                    self.scan_voice()
                    last_scan = t
                if t - last_house > 3600:
                    self.housekeeping()
                    last_house = t
            except Exception:  # noqa: BLE001 - DB hiccup: wait and retry
                log.exception("worker loop error")
                busy = 0
            self.stop.wait(0.5 if busy else 3)


def main() -> int:  # pragma: no cover - service entry point
    setup_logging()
    try:
        cfg = load()
    except ConfigError as exc:
        log.error("invalid configuration: %s", exc)
        return 78                      # EX_CONFIG: systemd won't restart-loop (RestartPreventExitStatus=78)
    db = Database(make_engine(cfg["database"]["worker_url"], "db-worker-password",
                              cfg["database"].get("pool_size", 5), "questersboard-worker"))
    try:
        rest = dr.DiscordRest()
    except SecretError:
        log.warning("discord-bot-token not installed: Discord delivery will retry until it is")
        rest = None
    holder = ConfigHolder(cfg)
    w = Worker(holder, db, rest)
    with db.tx() as s:
        log_config(s, "worker", cfg, sorted(cfg.section_hashes))
    signal.signal(signal.SIGTERM, lambda *_: w.stop.set())
    log.info("worker started")
    w.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["Worker", "main", "json"]
