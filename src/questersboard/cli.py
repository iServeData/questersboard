"""questersboard-admin: operator commands. Run as root or the questersboard user on the host.

Nothing here changes Discord, DNS, Nginx or the firewall. `sync-commands` is the only
command that talks to Discord's command registry and it refuses to run without
`--yes` AND the QUESTERSBOARD_APPROVED_CHANGE environment variable naming the change
(see docs/operations.md); `--dry-run` prints the exact payload instead.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from .config import ConfigError, load
from .envvars import getenv


def _cfg(args):
    try:
        return load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(78)


def _db(cfg, role="admin"):
    from .db import Database, make_engine
    key = {"admin": ("admin_url", "db-admin-password"), "app": ("app_url", "db-app-password"),
           "worker": ("worker_url", "db-worker-password")}[role]
    url = cfg["database"].get(key[0])
    if not url:
        print(f"[database] {key[0]} is not set", file=sys.stderr)
        sys.exit(78)
    return Database(make_engine(url, key[1], 2, "questersboard-admin"))


def cmd_check_config(args):
    cfg = _cfg(args)
    if args.quiet:
        return 0
    print(f"ok: {cfg.path}")
    for k, h in sorted(cfg.section_hashes.items()):
        print(f"  {k:<14} {h[:12]}")
    from .secrets import get_secret
    for name in ("discord-bot-token", "discord-client-secret", "field-key", "db-app-password",
                 "db-worker-password", "blizzard-client-id", "blizzard-client-secret", "ai-api-key"):
        sec = get_secret(name, required=False)
        print(f"  credential {name:<24} {'present fp=' + sec.fingerprint if sec else 'missing'}")
    return 0


def cmd_health(args):
    """DB reachable, web answering on its socket, no dead outbox jobs, worker heartbeat recent."""
    import httpx
    cfg = _cfg(args)
    ok = True
    try:
        db = _db(cfg, "app")
        with db.tx() as s:
            dead = s.execute(text("SELECT count(*) FROM outbox WHERE dead_at IS NOT NULL AND done_at IS NULL")).scalar()
            lag = s.execute(text("SELECT extract(epoch FROM now() - min(run_after)) FROM outbox "
                                 "WHERE done_at IS NULL AND dead_at IS NULL")).scalar()
        print(f"db: ok  outbox dead={dead} oldest_pending={int(lag or 0)}s")
        if (lag or 0) > 900:
            ok = False
        if dead:
            # A dead job is a Discord/API delivery that gave up (e.g. missing channel permission).
            # It needs attention, but it says nothing about whether a new release works, so a
            # deploy check only warns; the plain health check still fails.
            print("outbox: dead jobs need attention: questersboard-admin outbox status")
            if not args.deploy:
                ok = False
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        print(f"db: FAIL {type(exc).__name__}")
        ok = False
    if args.socket and Path(args.socket).exists():
        try:
            r = httpx.Client(transport=httpx.HTTPTransport(uds=args.socket), timeout=5).get("http://questersboard/healthz")
            print(f"web: {r.status_code}")
            ok = ok and r.status_code == 200
        except httpx.HTTPError as exc:
            print(f"web: FAIL {type(exc).__name__}")
            ok = False
    else:
        print("web: socket not present (skipped)")
    return 0 if ok else 1


def cmd_migrate(args):
    cfg = _cfg(args)
    from alembic import command
    from alembic.config import Config as AConfig
    from sqlalchemy.engine import make_url

    from .secrets import get_secret
    url = make_url(cfg["database"]["admin_url"])
    sec = get_secret("db-admin-password", required=False)
    if sec:
        url = url.set(password=sec.reveal())
    here = Path(__file__).resolve().parent / "migrations"
    ac = AConfig()
    ac.set_main_option("script_location", str(here))
    os.environ["QUESTERSBOARD_MIGRATE_URL"] = url.render_as_string(hide_password=False)
    if args.direction == "up":
        command.upgrade(ac, args.target or "head", sql=args.sql)
    else:
        if not args.target:
            print("downgrade needs an explicit target revision (e.g. base)", file=sys.stderr)
            return 2
        command.downgrade(ac, args.target, sql=args.sql)
    return 0


def cmd_seed_catalog(args):
    cfg = _cfg(args)
    from .domain import catalog
    db = _db(cfg, "app")
    with db.tx() as s:
        for ed in cfg.editions:
            print(f"{ed}: {catalog.seed_edition(s, cfg, ed)} rows added")
    return 0


def cmd_blizzard(args):
    cfg = _cfg(args)
    if args.action == "probe-character":
        from .integrations.blizzard import ApiError, Blizzard, Unsupported
        if not args.name:
            print("usage: questersboard-admin blizzard probe-character EDITION --name \"First Last\" [--region us]",
                  file=sys.stderr)
            return 2
        region = args.region or cfg.edition(args.edition).default_region
        try:
            rows = Blizzard(cfg).probe_character(args.edition, region, args.name, args.realm, args.detail)
        except (Unsupported, ApiError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 3
        print(f"{args.edition} {region.upper()} {rows[0]['namespace']}  (read-only; nothing saved)")
        for r in rows:
            print(f"  {r['page']:<16} {r['status']:<6} {r['shape']}")
            for line in r.get("tree") or []:
                print(f"      {line}")
        ok = sum(r["status"] == "200" for r in rows)
        print(f"{ok} of {len(rows)} pages answered. 404 = not served for this character (or not public yet).")
        if args.detail and not any(r.get("tree") for r in rows):
            print(f"note: no structure shown for --detail {args.detail} (page not answered, or not allowed).")
        return 0
    if args.action == "probe-media":
        from .integrations.blizzard import ApiError, Blizzard, Unsupported
        if not args.name:
            print("usage: questersboard-admin blizzard probe-media EDITION --name \"First Last\" [--region us]",
                  file=sys.stderr)
            return 2
        region = args.region or cfg.edition(args.edition).default_region
        try:
            rows = Blizzard(cfg).probe_media(args.edition, region, args.name, args.realm)
        except (Unsupported, ApiError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 3
        if args.detail == "recipes":           # just the known-recipe checks, for a short answer
            first = next((i for i, r in enumerate(rows) if r["check"] == "professions"), len(rows))
            last = next((i for i, r in enumerate(rows) if r["check"] == "talent-tree-index"), len(rows))
            rows = rows[first:last]
        print(f"{args.edition} {region.upper()}  icons, tooltips, talent trees and recipes  (read-only; nothing saved)")
        for r in rows:
            print(f"  {r['check']:<22} {r['status']:<6} {r['namespace']} {r['path']}")
            if r.get("shape"):
                print(f"      {r['shape']}")
            for line in r.get("tree") or []:
                print(f"      {line}")
        ok = sum(r["status"] == "200" for r in rows)
        print(f"{ok} of {len(rows)} checks answered. 404 = Blizzard doesn't serve it for this edition.")
        return 0
    if args.action == "probe-item":
        import json as _json
        from .domain import item_links
        from .integrations.blizzard import ApiError, Blizzard
        if not args.name:
            print("usage: questersboard-admin blizzard probe-item EDITION --name \"Item Name\" [--region us]",
                  file=sys.stderr)
            return 2
        cfg.edition(args.edition)                   # unknown edition: a clear error
        trace: list = []
        try:
            item = item_links.find(None, Blizzard(cfg), cfg, 0, args.name, args.edition, trace)
        except ApiError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 3
        print(f"item search for {args.name!r}: {args.edition} first, then [item_links] fallbacks  "
              "(read-only; nothing saved)")
        for e in trace:
            print(f"  {e['edition']} {e['region'].upper()} {e['namespace'] or '(no static namespace)'}")
            for t in e["pages"]:
                if t.get("error"):
                    print(f"    page {t['page']}: not answered ({t['error']})")
                else:
                    print(f"    page {t['page']} of {t['page_count']}: {t['results']} results, "
                          f"exact matches {t['exact'] or 'none'}")
            if not e["pages"]:
                print("    not searched (no static namespace)")
        if item is None:
            print("no card: no item with exactly that name in any of them")
            return 1
        print(f"chosen item {item['id']} ({item.get('quality') or 'no quality'}) from {trace[-1]['edition']}; the card:")
        print(_json.dumps(item_links.card(item), indent=2, ensure_ascii=False))
        return 0
    from .integrations.blizzard import Blizzard
    db = _db(cfg, "app")
    bz = Blizzard(cfg)
    with db.tx() as s:
        if args.action == "probe":
            res = bz.probe(s, args.edition, args.region or cfg.edition(args.edition).default_region)
        else:
            res = bz.import_reference(s, args.edition, args.region or cfg.edition(args.edition).default_region)
    print(json.dumps(res, indent=2))
    print("note: a probe shows what the API serves today; edit api_status in config.toml yourself.")
    return 0


def cmd_sync_commands(args):
    cfg = _cfg(args)
    from .bot.client import command_payload
    payload = command_payload(cfg)
    if args.dry_run or not args.yes:
        print(json.dumps(payload, indent=2))
        if not args.yes:
            print("\nDry run only. Registering commands is a live Discord change and needs approval:",
                  file=sys.stderr)
            print("  QUESTERSBOARD_APPROVED_CHANGE=sync-commands questersboard-admin sync-commands --yes", file=sys.stderr)
        return 0
    if getenv("APPROVED_CHANGE") != "sync-commands":
        print("refusing: set QUESTERSBOARD_APPROVED_CHANGE=sync-commands after approval", file=sys.stderr)
        return 3
    from .integrations.discord_rest import DiscordRest
    res = DiscordRest().put_commands(cfg["discord"]["application_id"], cfg.guild_id, payload)
    print(f"registered {len(res)} command groups in the configured guild")
    return 0


def cmd_role_emojis(args):
    """Upload the role icons as the bot's own Discord emojis. Read-only (lists what is there and what
    would be uploaded) unless --yes with QUESTERSBOARD_APPROVED_CHANGE=role-emojis. Never replaces or
    deletes an emoji: one that already exists under the configured name is kept as it is."""
    from pathlib import Path
    from .integrations.discord_rest import DiscordError, DiscordRest
    cfg = _cfg(args)
    icons = cfg["event_posts"].get("role_icons", {})
    if not icons:
        print("[event_posts] role_icons is empty: role icons are off, nothing to upload.")
        return 0
    art = Path(args.art) if args.art else Path(__file__).parent / "web" / "static" / "roles"
    app = cfg["discord"]["application_id"]
    try:
        rest = DiscordRest()
        have = rest.app_emojis(app)
    except DiscordError as exc:
        print(f"Discord lookup failed: {exc}", file=sys.stderr)
        return 1
    todo = []
    for key, name in icons.items():
        f = art / f"{key}.png"
        if name in have:
            print(f"  {key}: {name} is already uploaded")
        elif not f.is_file():
            print(f"  {key}: no picture {f.name} in {art}; this role stays plain text")
        elif f.stat().st_size > 256 * 1024:
            print(f"  {key}: {f.name} is over Discord's 256 KB emoji limit; skipped")
        else:
            print(f"  {key}: would upload {f.name} as {name}")
            todo.append((key, name, f))
    if not todo:
        print("Nothing to upload.")
        return 0
    if not args.yes:
        print(f"Dry run: {len(todo)} emoji(s) would be uploaded. Uploading is a live Discord change and needs "
              "approval:", file=sys.stderr)
        print("  QUESTERSBOARD_APPROVED_CHANGE=role-emojis questersboard-admin role-emojis --yes", file=sys.stderr)
        return 0
    if getenv("APPROVED_CHANGE") != "role-emojis":
        print("refusing: set QUESTERSBOARD_APPROVED_CHANGE=role-emojis after approval", file=sys.stderr)
        return 3
    for key, name, f in todo:
        try:
            rest.create_app_emoji(app, name, f.read_bytes())
        except DiscordError as exc:
            print(f"  {key}: upload failed: {exc}", file=sys.stderr)
            return 1
        print(f"  {key}: uploaded as {name}")
    print("Done. New and edited event posts show the icons within 30 minutes; "
          "`questersboard-admin refresh-posts --yes` updates the current posts.")
    return 0


def cmd_discord_permissions(args):
    """Read-only: effective permissions in the WoW category and slash-command registration."""
    cfg = _cfg(args)
    from .integrations import discord_audit
    from .integrations.discord_rest import DiscordError, DiscordRest
    try:
        data = discord_audit.fetch(DiscordRest(), cfg)
    except DiscordError as exc:
        print(f"Discord lookup failed: {exc}", file=sys.stderr)
        return 1
    rep = discord_audit.build(cfg, cfg.guild_id, *data, category_id=args.category)
    print(rep.text())
    print(discord_audit.LEGEND)
    print(f"\n{rep.fixes} to fix, {rep.notes} note(s). Read-only: nothing was changed in Discord.")
    return 2 if rep.fixes else 0


def cmd_calendar_revoke(args):
    cfg = _cfg(args)
    db = _db(cfg, "app")
    with db.tx() as s:
        n = s.execute(text("UPDATE calendar_tokens SET revoked_at = now() WHERE revoked_at IS NULL AND member_id = "
                           "(SELECT id FROM members WHERE discord_id = :d)"), {"d": args.discord_id}).rowcount
        s.execute(text("INSERT INTO audit_log (source, action, target_type, target_id, detail) "
                       "VALUES ('cli', 'calendar.revoke_admin', 'member', :d, '{}'::jsonb)"), {"d": args.discord_id})
    print(f"revoked {n} calendar link(s)")
    return 0


def cmd_erase_member(args):
    """The same removal as the web page's "Remove me", for someone who can no longer log in."""
    from .domain import erasure
    from .domain.errors import NotFound
    cfg = _cfg(args)
    db = _db(cfg, "app")
    with db.tx() as s:
        try:
            mid = erasure.find_member_id(s, args.discord_id)
        except NotFound as exc:
            print(str(exc), file=sys.stderr)
            return 1
        p = erasure.plan(s, mid)
        print(f"member {mid}: leave {len(p.leave)} event(s), cancel {len(p.cancel)} hosted event(s), "
              f"discard {p.drafts} draft(s), remove {p.characters} character(s), anonymise {p.stories} story(ies), "
              f"calendar links {p.calendar_links}, battle.net {'yes' if p.battlenet else 'no'}")
        if not args.yes:
            print("dry run: nothing changed. Re-run with --yes to remove this member permanently.")
            return 0
        r = erasure.erase(s, cfg, mid, source="cli")
    print("removed: " + ", ".join(f"{k}={len(v) if isinstance(v, list) else v}" for k, v in r.items()))
    return 0


def cmd_prune(args):
    cfg = _cfg(args)
    r = cfg["retention"]
    db = _db(cfg, "admin")
    with db.tx() as s:
        a = s.execute(text("DELETE FROM audit_log WHERE at < now() - make_interval(days => :d)"),
                      {"d": r["audit_days"]}).rowcount
        b = s.execute(text(
            "DELETE FROM signups su USING events e WHERE su.event_id = e.id AND su.state IN ('withdrawn','rejected') "
            "AND e.end_at < now() - make_interval(days => :d)"), {"d": r["withdrawn_signup_days"]}).rowcount
        c = s.execute(text("DELETE FROM web_sessions WHERE expires_at < now()")).rowcount
    print(f"pruned: audit_log={a} withdrawn/rejected signups={b} sessions={c}")
    return 0


def cmd_outbox(args):
    cfg = _cfg(args)
    db = _db(cfg, "app")
    with db.tx() as s:
        if args.action == "retry-dead":
            n = s.execute(text("UPDATE outbox SET dead_at = NULL, attempts = 0, run_after = now() "
                               "WHERE dead_at IS NOT NULL AND done_at IS NULL")).rowcount
            print(f"requeued {n} job(s)")
        elif args.action == "dismiss-dead":
            n = s.execute(text("UPDATE outbox SET done_at = now() WHERE dead_at IS NOT NULL AND done_at IS NULL"
                               )).rowcount
            print(f"dismissed {n} dead job(s); they stay in the outbox history until pruned")
        else:
            for row in s.execute(text(
                    "SELECT kind, count(*) FILTER (WHERE done_at IS NULL AND dead_at IS NULL) pending, "
                    "count(*) FILTER (WHERE dead_at IS NOT NULL) dead FROM outbox GROUP BY kind ORDER BY kind")):
                print(f"{row.kind:<34} pending={row.pending} dead={row.dead}")
            for row in s.execute(text(
                    "SELECT id, kind, key, attempts, dead_at, last_error FROM outbox "
                    "WHERE dead_at IS NOT NULL AND done_at IS NULL ORDER BY dead_at DESC LIMIT 20")):
                print(f"dead #{row.id} {row.kind} {row.key} attempts={row.attempts} "
                      f"at {row.dead_at:%Y-%m-%d %H:%M}Z\n    {(row.last_error or '').strip()[:300]}")
    return 0


def cmd_refresh_posts(args):
    """Re-render every upcoming or running event's Discord post (after an upgrade changes the layout).

    Edits existing bot posts in place; nothing new is posted and nobody is pinged. With [event_threads]
    on, each re-render also opens that event's chat thread. Dry run unless --yes.
    """
    from .domain import outbox
    cfg = _cfg(args)
    db = _db(cfg, "app")
    with db.tx() as s:
        rows = list(s.execute(text(
            "SELECT id, title FROM events WHERE state IN ('scheduled','in_progress') "
            "AND discord_message_id IS NOT NULL ORDER BY start_at")))
        for r in rows:
            print(f"  #{r.id} {r.title}")
        if not args.yes:
            print(f"dry run: {len(rows)} post(s) would be updated. Re-run with --yes to queue the updates.")
            return 0
        for r in rows:
            outbox.enqueue(s, "discord.render_event", str(r.id), {"event_id": r.id})
    print(f"queued {len(rows)} post update(s); the worker applies them within a minute.")
    return 0


def cmd_refresh_item_cards(args):
    """Look up the names in recent item cards again and edit those cards in place (after an upgrade, or
    once Blizzard adds an item that wasn't found). Nothing new is posted. Dry run unless --yes."""
    from .domain import outbox
    cfg = _cfg(args)
    db = _db(cfg, "app")
    with db.tx() as s:
        rows = list(s.execute(text(
            "SELECT DISTINCT ON (key) key, payload->'names' AS names FROM outbox "
            "WHERE kind = 'item_links.post' AND done_at IS NOT NULL AND payload ? 'card_id' "
            "AND done_at > now() - make_interval(hours => :h) ORDER BY key, id DESC"), {"h": args.hours}))
        for r in rows:
            print("  " + ", ".join(f"[{n}]" for n in (r.names or [])))
        if not args.yes:
            print(f"dry run: {len(rows)} card message(s) from the last {args.hours:g} hours would be looked up "
                  "again and edited. Re-run with --yes to queue the updates.")
            return 0
        for r in rows:
            outbox.enqueue(s, "item_links.refresh", r.key, {"key": r.key})
    print(f"queued {len(rows)} card update(s); the worker applies them within a minute.")
    return 0


def cmd_refresh_details(args):
    """Refresh every character's imported Blizzard details (after an upgrade changes how they're stored),
    as if each owner pressed Refresh from Blizzard: same groups, same sharing. Characters without
    imported details are left alone. Read-only toward Discord. Dry run unless --yes."""
    import time
    from .domain import profiles
    from .domain.errors import Invalid, NotFound
    from .integrations.blizzard import ApiError, Blizzard, Unsupported
    cfg = _cfg(args)
    db = _db(cfg, "app")
    with db.tx() as s:
        rows = profiles.operator_refresh_list(s, cfg)
    owners = len({m for _c, _n, m in rows})
    if not args.yes:
        for cid, name, _m in rows:
            print(f"  #{cid} {name}")
        print(f"dry run: {len(rows)} character(s) of {owners} member(s) would be refreshed from Blizzard. "
              "Re-run with --yes to refresh them.")
        return 0
    bz = Blizzard(cfg)
    pause = float(cfg["blizzard"].get("operator_refresh_pause_seconds", 1))
    done, failed = 0, []
    for i, (cid, name, _m) in enumerate(rows):
        if i and pause:
            time.sleep(pause)
        try:
            with db.tx() as s:
                profiles.operator_refresh(s, cfg, bz, cid)
            done += 1
            print(f"  ok      #{cid} {name}", flush=True)
        except (Invalid, NotFound, ApiError, Unsupported) as exc:
            failed.append(name)
            print(f"  failed  #{cid} {name}: {exc}", flush=True)
    print(f"refreshed {done} of {len(rows)} character(s)" + (f"; {len(failed)} failed (kept as they were)" if failed else ""))
    return 1 if failed and not done else 0


def cmd_cover(args):
    """Install the community's default cover art (re-encoded to WebP) as covers/default_cover.webp."""
    from .domain import covers
    from .domain.errors import Invalid
    cfg = _cfg(args)
    try:
        name = covers.save(cfg, Path(args.file).read_bytes())
    except (OSError, Invalid) as exc:
        print(f"cover not installed: {exc}", file=sys.stderr)
        return 1
    d = covers.folder(cfg)
    (d / name).replace(d / "default_cover.webp")
    print(f"installed {d / 'default_cover.webp'}")
    if cfg["event_posts"].get("default_cover") != "default_cover.webp":
        print('set [event_posts] default_cover = "default_cover.webp" in the config to use it')
    return 0


def cmd_backup(args):
    script = Path(args.scripts) / ("backup.sh" if args.action == "backup" else "restore.sh")
    cmd = [str(script)] + ([args.file] if args.file else [])
    return subprocess.call(cmd)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="questersboard-admin")
    p.add_argument("--config", default=None, help="default: $QUESTERSBOARD_CONFIG or /etc/questersboard/config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    cc = sub.add_parser("check-config")
    cc.add_argument("--quiet", action="store_true", help="validate only; exit 78 on error (used by ExecStartPre)")
    cc.set_defaults(fn=cmd_check_config)
    hp = sub.add_parser("health")
    hp.add_argument("--socket", default="/run/questersboard-web.sock")
    hp.add_argument("--deploy", action="store_true", help="release check: dead outbox jobs warn instead of fail")
    hp.set_defaults(fn=cmd_health)
    m = sub.add_parser("migrate")
    m.add_argument("direction", choices=("up", "down"))
    m.add_argument("target", nargs="?")
    m.add_argument("--sql", action="store_true", help="print SQL instead of running it")
    m.set_defaults(fn=cmd_migrate)
    sub.add_parser("seed-catalog").set_defaults(fn=cmd_seed_catalog)
    b = sub.add_parser("blizzard")
    b.add_argument("action", choices=("probe", "import-reference", "probe-character", "probe-media", "probe-item"))
    b.add_argument("edition")
    b.add_argument("--region")
    b.add_argument("--name", help="probe-character / probe-media: the character's name (Forever: first name and surname); probe-item: the item's name (searched in EDITION, then [item_links] fallbacks)")
    b.add_argument("--realm", help="probe-character / probe-media: realm, for editions that use realms")
    b.add_argument("--detail", help="probe-character: show one page's structure (e.g. specializations); probe-media: recipes = only the recipe checks")
    b.set_defaults(fn=cmd_blizzard)
    sc = sub.add_parser("sync-commands")
    sc.add_argument("--yes", action="store_true")
    sc.add_argument("--dry-run", action="store_true")
    sc.set_defaults(fn=cmd_sync_commands)
    re_ = sub.add_parser("role-emojis", help="upload the role icons as the bot's Discord emojis (dry run unless --yes)")
    re_.add_argument("--yes", action="store_true")
    re_.add_argument("--art", help="folder with <role key>.png pictures (default: the built-in icons)")
    re_.set_defaults(fn=cmd_role_emojis)
    dp = sub.add_parser("discord-permissions", help="read-only check of WoW channel permissions and slash commands")
    dp.add_argument("--category", help="category channel ID (default: the event channel's category)")
    dp.set_defaults(fn=cmd_discord_permissions)
    cr = sub.add_parser("calendar-revoke")
    cr.add_argument("discord_id")
    cr.set_defaults(fn=cmd_calendar_revoke)
    em = sub.add_parser("erase-member", help="remove a member's QuestersBoard data (dry run unless --yes)")
    em.add_argument("discord_id")
    em.add_argument("--yes", action="store_true")
    em.set_defaults(fn=cmd_erase_member)
    sub.add_parser("prune").set_defaults(fn=cmd_prune)
    ob = sub.add_parser("outbox")
    ob.add_argument("action", choices=("status", "retry-dead", "dismiss-dead"))
    ob.set_defaults(fn=cmd_outbox)
    rp = sub.add_parser("refresh-posts", help="re-render upcoming events' Discord posts (dry run unless --yes)")
    rp.add_argument("--yes", action="store_true")
    rp.set_defaults(fn=cmd_refresh_posts)
    ri = sub.add_parser("refresh-item-cards", help="look up recent item cards again and edit them in place "
                        "(dry run unless --yes)")
    ri.add_argument("--hours", type=float, default=24, help="cards posted in the last this many hours (default 24)")
    ri.add_argument("--yes", action="store_true")
    ri.set_defaults(fn=cmd_refresh_item_cards)
    rd = sub.add_parser("refresh-details", help="refresh everyone's imported Blizzard details (dry run unless --yes)")
    rd.add_argument("--yes", action="store_true")
    rd.set_defaults(fn=cmd_refresh_details)
    cv = sub.add_parser("default-cover", help="install the default event cover art from an image file")
    cv.add_argument("file")
    cv.set_defaults(fn=cmd_cover)
    bk = sub.add_parser("backup")
    bk.add_argument("action", choices=("backup", "restore"))
    bk.add_argument("file", nargs="?")
    bk.add_argument("--scripts", default="/opt/questersboard/current/deploy/backup")
    bk.set_defaults(fn=cmd_backup)
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
