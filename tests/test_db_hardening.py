"""Database hardening checks, run against the test cluster configured with
deploy/postgres/pg_hba.conf, postgresql_wowquester.conf and roles.sql."""
from __future__ import annotations

import os
import subprocess
import sys

import psycopg
import pytest
from conftest import HOST, PORT, ROOT, needs_db, url

pytestmark = needs_db
SOCK = os.environ.get("WQT_PG_SOCKET", "/tmp/pgt")


def _connect(user, password="x", host=HOST, dbname="wowquester"):
    return psycopg.connect(host=host, port=PORT, user=user, password=password, dbname=dbname, connect_timeout=5)


@pytest.mark.parametrize("user", ["postgres", "root"])
@pytest.mark.parametrize("dbname", ["wowquester", "postgres"])
def test_superuser_and_root_password_login_rejected_over_tcp(user, dbname):
    with pytest.raises(psycopg.OperationalError) as e:
        _connect(user, "any-password", dbname=dbname)
    assert "rejects connection" in str(e.value) or "no pg_hba.conf entry" in str(e.value)


@pytest.mark.skipif(not os.path.isdir(SOCK), reason="no local socket dir")
@pytest.mark.parametrize("user", ["postgres", "root"])
def test_socket_login_rejected_for_non_matching_os_user(user):
    """postgres is peer-only: a different OS user can't become it, with or without a password."""
    if os.geteuid() == 0:
        pytest.skip("running as root")
    with pytest.raises(psycopg.OperationalError):
        _connect(user, "any-password", host=SOCK)


def test_unknown_role_rejected():
    with pytest.raises(psycopg.OperationalError):
        _connect("intruder", "pw")


def test_no_role_named_root_and_no_extra_superusers(admin_engine):
    from sqlalchemy import text
    with admin_engine.connect() as c:
        assert c.execute(text("select count(*) from pg_roles where rolname='root'")).scalar() == 0
        supers = [r[0] for r in c.execute(text("select rolname from pg_roles where rolsuper"))]
        assert supers == ["postgres"]
        attrs = c.execute(text("select rolsuper, rolcreaterole, rolcreatedb from pg_roles "
                               "where rolname like 'wowquester_%'")).all()
        assert all(a == (False, False, False) for a in attrs)
        assert c.execute(text("show password_encryption")).scalar() == "scram-sha-256"
        assert c.execute(text("select bool_or(has_database_privilege('public', 'wowquester', 'CONNECT'))"
                              )).scalar() is False


@pytest.mark.parametrize("role", ["wowquester_app", "wowquester_worker", "wowquester_readonly"])
@pytest.mark.parametrize("ddl", ["CREATE TABLE evil (id int)", "ALTER TABLE events ADD COLUMN x int",
                                 "DROP TABLE signups", "CREATE ROLE evil", "TRUNCATE audit_log"])
def test_service_roles_cannot_run_ddl(role, ddl):
    pw = os.environ.get(f"WQT_{role.split('_', 1)[1].upper()}_PW")
    with _connect(role, pw) as c:
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)):
            c.execute(ddl)


def test_app_cannot_rewrite_audit_log():
    with _connect("wowquester_app", os.environ.get("WQT_APP_PW")) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE audit_log SET action='x'")


def test_readonly_sees_only_projections():
    with _connect("wowquester_readonly", os.environ.get("WQT_READONLY_PW")) as c:
        c.execute("select count(*) from public_chronicle")
        c.execute("select count(*) from public_events")
        c.execute("select count(*) from service_heartbeats")
        c.execute("select count(*) from public_event_voice")
        for t in ("members", "signups", "characters", "web_sessions"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"select * from {t}")
            c.rollback()


def test_worker_cannot_read_sessions_or_tokens():
    with _connect("wowquester_worker", os.environ.get("WQT_WORKER_PW")) as c:
        for t in ("web_sessions", "calendar_tokens", "battlenet_links"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"select * from {t}")
            c.rollback()


def test_admin_role_can_migrate_down_and_up(db):
    env = dict(os.environ, PGPASSWORD=os.environ.get("WQT_ADMIN_PW", ""), PYTHONPATH=str(ROOT / "src"),
               WOWQUESTER_MIGRATE_URL=f"postgresql+psycopg://wowquester_admin@{HOST}:{PORT}/wowquester")
    alembic = [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini")]
    for args in (["downgrade", "base"], ["upgrade", "head"], ["current"]):
        r = subprocess.run(alembic + args, cwd=ROOT, env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]
    assert "0008 (head)" in r.stdout


def test_worker_heartbeat_grants_are_narrow():
    """The worker may record its own check-in but never write voice counts or touch other columns."""
    with _connect("wowquester_worker", os.environ.get("WQT_WORKER_PW")) as c:
        c.execute("select count(*) from service_heartbeats")
        c.execute("select count(*) from public_event_voice")
        for sql in ("INSERT INTO public_event_voice (public_id, in_voice) VALUES (gen_random_uuid(), 1)",
                    "UPDATE service_heartbeats SET name = 'bot'", "DELETE FROM service_heartbeats"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(sql)
            c.rollback()
