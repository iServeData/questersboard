"""wowquester-admin against a temp config and the local test cluster (FIXTURE IDs, passwords from env)."""
from __future__ import annotations

import json
import os

import pytest
from conftest import HOST, PORT, ROOT, fake_id, needs_db
from sqlalchemy import text

from wowquester import cli


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    t = (ROOT / "deploy" / "config.example.toml").read_text()
    t = t.replace('guild_id = ""', f'guild_id = "{fake_id(1)}"', 1)
    t = t.replace('application_id = ""', f'application_id = "{fake_id(2)}"', 1)
    t = t.replace("127.0.0.1:5432", f"{HOST}:{PORT}")
    t = t.replace('"/var/lib/wowquester/uploads"', f'"{tmp_path}"')
    p = tmp_path / "config.toml"
    p.write_text(t)
    for role in ("admin", "app", "worker"):
        monkeypatch.setenv(f"WOWQUESTER_SECRET_DB_{role.upper()}_PASSWORD", os.environ.get(f"WQT_{role.upper()}_PW", ""))
    monkeypatch.delenv("WOWQUESTER_APPROVED_CHANGE", raising=False)
    return str(p)


def test_check_config_never_prints_secret_values(cfg_file, capsys, monkeypatch):
    monkeypatch.setenv("WOWQUESTER_SECRET_DISCORD_BOT_TOKEN", "FIXTURE-not-a-real-token-123")
    assert cli.main(["--config", cfg_file, "check-config"]) == 0
    out = capsys.readouterr().out
    assert "FIXTURE-not-a-real-token-123" not in out and "discord-bot-token" in out and "fp=" in out


def test_invalid_config_exits_78(tmp_path, capsys):
    bad = tmp_path / "c.toml"
    bad.write_text("[community]\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(bad), "check-config"])
    assert e.value.code == 78


def test_sync_commands_is_dry_run_without_approval(cfg_file, capsys, monkeypatch):
    called = []
    monkeypatch.setattr("wowquester.integrations.discord_rest.DiscordRest.put_commands",
                        lambda *a, **k: called.append(a) or [])
    assert cli.main(["--config", cfg_file, "sync-commands"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)[0]["name"] in ("quest", "character") and "needs approval" in out.err
    assert cli.main(["--config", cfg_file, "sync-commands", "--yes"]) == 3      # --yes alone is not enough
    assert not called


@needs_db
def test_prune_and_outbox_status(cfg_file, capsys):
    assert cli.main(["--config", cfg_file, "outbox", "status"]) == 0
    assert cli.main(["--config", cfg_file, "prune"]) == 0
    assert "pruned:" in capsys.readouterr().out


@needs_db
def test_calendar_revoke_is_audited(cfg_file, db, capsys):
    did = fake_id(7777)
    with db.tx() as s:
        s.execute(text("DELETE FROM members WHERE discord_id=:d"), {"d": did})
        mid = s.execute(text("INSERT INTO members (discord_id, display_name) VALUES (:d,'x') RETURNING id"),
                        {"d": did}).scalar()
        s.execute(text("INSERT INTO calendar_tokens (member_id, token_hash) VALUES (:m, :h)"),
                  {"m": mid, "h": "f" * 64})
    assert cli.main(["--config", cfg_file, "calendar-revoke", did]) == 0
    with db.tx() as s:
        assert s.execute(text("SELECT count(*) FROM calendar_tokens WHERE member_id=:m AND revoked_at IS NULL"),
                         {"m": mid}).scalar() == 0
        assert s.execute(text("SELECT count(*) FROM audit_log WHERE action='calendar.revoke_admin' AND target_id=:d"),
                         {"d": did}).scalar() >= 1


def test_erase_member_is_dry_run_unless_yes(cfg_file, db, capsys):
    did = fake_id(7778)
    with db.tx() as s:
        s.execute(text("DELETE FROM members WHERE discord_id=:d"), {"d": did})
        mid = s.execute(text("INSERT INTO members (discord_id, display_name) VALUES (:d,'x') RETURNING id"),
                        {"d": did}).scalar()
    assert cli.main(["--config", cfg_file, "erase-member", did]) == 0
    assert "dry run" in capsys.readouterr().out
    with db.tx() as s:
        assert s.execute(text("SELECT erased_at FROM members WHERE id=:m"), {"m": mid}).scalar() is None
    assert cli.main(["--config", cfg_file, "erase-member", did, "--yes"]) == 0
    assert "removed:" in capsys.readouterr().out
    with db.tx() as s:
        assert s.execute(text("SELECT discord_id FROM members WHERE id=:m"), {"m": mid}).scalar() == f"erased:{mid}"
    assert cli.main(["--config", cfg_file, "erase-member", did]) == 1


def test_deploy_scripts_health_check_loads_the_db_credential():
    """Regression: a bare `wowquester-admin health` has no DB password (credentials are only given to
    the units), so install.sh rolled back a healthy upgrade. Both scripts must use deploy/health.sh."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "deploy"
    h = (root / "health.sh").read_text()
    assert "LoadCredentialEncrypted=wowquester.db-app-password:" in h and "--socket" in h
    for name in ("install.sh", "rollback.sh"):
        body = (root / name).read_text()
        assert "health.sh" in body
        assert "wowquester-admin health" not in body and '"$ADMIN_WRAPPER" health' not in body


@needs_db
def test_worker_heartbeat_failure_does_not_block_the_loop(cfg, worker_db, monkeypatch):
    from wowquester.config import ConfigHolder
    from wowquester.domain import status
    from wowquester.worker import Worker

    def boom(*a, **kw):
        raise RuntimeError("relation service_heartbeats does not exist")
    monkeypatch.setattr(status, "beat", boom)
    Worker(ConfigHolder(cfg), worker_db, None).heartbeat()       # logs, does not raise


@needs_db
def test_dead_outbox_job_is_shown_warns_on_deploy_and_can_be_dismissed(cfg_file, db, capsys, tmp_path):
    """Regression: one dead Discord delivery made install.sh roll back a working release."""
    with db.tx() as s:
        s.execute(text("UPDATE outbox SET done_at = now() WHERE done_at IS NULL"))
        s.execute(text("INSERT INTO outbox (kind, key, payload, dead_at, attempts, last_error) "
                       "VALUES ('event.post', 'ev:fixture', '{}', now(), 8, "
                       "'403 Forbidden (error code: 50001): Missing Access')"))
    nosock = str(tmp_path / "absent.sock")
    assert cli.main(["--config", cfg_file, "health", "--socket", nosock]) == 1
    assert cli.main(["--config", cfg_file, "health", "--socket", nosock, "--deploy"]) == 0
    assert "dead jobs need attention" in capsys.readouterr().out
    assert cli.main(["--config", cfg_file, "outbox", "status"]) == 0
    out = capsys.readouterr().out
    assert "dead #" in out and "event.post ev:fixture" in out and "Missing Access" in out
    assert cli.main(["--config", cfg_file, "outbox", "dismiss-dead"]) == 0
    assert "dismissed 1" in capsys.readouterr().out
    assert cli.main(["--config", cfg_file, "health", "--socket", nosock]) == 0


def test_deploy_scripts_use_the_deploy_health_mode():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "deploy"
    assert '"$REPO/deploy/health.sh" --quiet --deploy' in (root / "install.sh").read_text()
    assert 'health.sh" --deploy' in (root / "rollback.sh").read_text()
