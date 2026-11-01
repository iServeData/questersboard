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
