"""wowquester-admin: operator commands. Run as root or the wowquester user on the host.

Nothing here changes Discord, DNS, Nginx or the firewall. `sync-commands` is the only
command that talks to Discord's command registry and it refuses to run without
`--yes` AND the WOWQUESTER_APPROVED_CHANGE environment variable naming the change
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
    return Database(make_engine(url, key[1], 2, "wowquester-admin"))


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
            dead = s.execute(text("SELECT count(*) FROM outbox WHERE dead_at IS NOT NULL")).scalar()
            lag = s.execute(text("SELECT extract(epoch FROM now() - min(run_after)) FROM outbox "
                                 "WHERE done_at IS NULL AND dead_at IS NULL")).scalar()
        print(f"db: ok  outbox dead={dead} oldest_pending={int(lag or 0)}s")
        if dead or (lag or 0) > 900:
            ok = False
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        print(f"db: FAIL {type(exc).__name__}")
        ok = False
    if args.socket and Path(args.socket).exists():
        try:
            r = httpx.Client(transport=httpx.HTTPTransport(uds=args.socket), timeout=5).get("http://wowquester/healthz")
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
    os.environ["WOWQUESTER_MIGRATE_URL"] = url.render_as_string(hide_password=False)
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
            print("  WOWQUESTER_APPROVED_CHANGE=sync-commands wowquester-admin sync-commands --yes", file=sys.stderr)
        return 0
    if os.environ.get("WOWQUESTER_APPROVED_CHANGE") != "sync-commands":
        print("refusing: set WOWQUESTER_APPROVED_CHANGE=sync-commands after approval", file=sys.stderr)
        return 3
    from .integrations.discord_rest import DiscordRest
    res = DiscordRest().put_commands(cfg["discord"]["application_id"], cfg.guild_id, payload)
    print(f"registered {len(res)} command groups in the configured guild")
    return 0


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
                               "WHERE dead_at IS NOT NULL")).rowcount
            print(f"requeued {n} job(s)")
        else:
            for row in s.execute(text(
                    "SELECT kind, count(*) FILTER (WHERE done_at IS NULL AND dead_at IS NULL) pending, "
                    "count(*) FILTER (WHERE dead_at IS NOT NULL) dead FROM outbox GROUP BY kind ORDER BY kind")):
                print(f"{row.kind:<34} pending={row.pending} dead={row.dead}")
    return 0


def cmd_backup(args):
    script = Path(args.scripts) / ("backup.sh" if args.action == "backup" else "restore.sh")
    cmd = [str(script)] + ([args.file] if args.file else [])
    return subprocess.call(cmd)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="wowquester-admin")
    p.add_argument("--config", default=None, help="default: $WOWQUESTER_CONFIG or /etc/wowquester/config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    cc = sub.add_parser("check-config")
    cc.add_argument("--quiet", action="store_true", help="validate only; exit 78 on error (used by ExecStartPre)")
    cc.set_defaults(fn=cmd_check_config)
    hp = sub.add_parser("health")
    hp.add_argument("--socket", default="/run/wowquester-web.sock")
    hp.set_defaults(fn=cmd_health)
    m = sub.add_parser("migrate")
    m.add_argument("direction", choices=("up", "down"))
    m.add_argument("target", nargs="?")
    m.add_argument("--sql", action="store_true", help="print SQL instead of running it")
    m.set_defaults(fn=cmd_migrate)
    sub.add_parser("seed-catalog").set_defaults(fn=cmd_seed_catalog)
    b = sub.add_parser("blizzard")
    b.add_argument("action", choices=("probe", "import-reference"))
    b.add_argument("edition")
    b.add_argument("--region")
    b.set_defaults(fn=cmd_blizzard)
    sc = sub.add_parser("sync-commands")
    sc.add_argument("--yes", action="store_true")
    sc.add_argument("--dry-run", action="store_true")
    sc.set_defaults(fn=cmd_sync_commands)
    cr = sub.add_parser("calendar-revoke")
    cr.add_argument("discord_id")
    cr.set_defaults(fn=cmd_calendar_revoke)
    sub.add_parser("prune").set_defaults(fn=cmd_prune)
    ob = sub.add_parser("outbox")
    ob.add_argument("action", choices=("status", "retry-dead"))
    ob.set_defaults(fn=cmd_outbox)
    bk = sub.add_parser("backup")
    bk.add_argument("action", choices=("backup", "restore"))
    bk.add_argument("file", nargs="?")
    bk.add_argument("--scripts", default="/opt/wowquester/current/deploy/backup")
    bk.set_defaults(fn=cmd_backup)
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
