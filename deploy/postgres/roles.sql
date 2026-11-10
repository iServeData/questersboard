-- QuestersBoard PostgreSQL roles. Run ONCE as the postgres superuser over the local socket.
-- Normally run by deploy/postgres/apply_hardening.sh, which passes the passwords through the
-- environment (WQ_ADMIN_PW, WQ_APP_PW, WQ_WORKER_PW, WQ_READONLY_PW; never argv, so never in `ps`).
-- (psql -v admin_pw=... also works for disposable test clusters.)
-- Passwords are hashed server-side with SCRAM-SHA-256 and never stored in this file.
-- No role named "root" is created. postgres keeps local peer authentication only.

\set ON_ERROR_STOP on
\getenv admin_pw WQ_ADMIN_PW
\getenv app_pw WQ_APP_PW
\getenv worker_pw WQ_WORKER_PW
\getenv readonly_pw WQ_READONLY_PW
SET password_encryption = 'scram-sha-256';
-- log_statement = 'ddl' would otherwise write the CREATE ROLE ... PASSWORD statements (with the
-- password literal) to the server log. Superuser-only setting, this session only.
SET log_statement = 'none';
SET log_min_error_statement = 'panic';

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'root') THEN
    RAISE EXCEPTION 'a PostgreSQL role named root exists; remove it before continuing';
  END IF;
END $$;

-- Schema owner + migrations. Not a superuser, cannot create roles or databases.
SELECT format('CREATE ROLE questersboard_admin LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', :'admin_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_admin') \gexec
-- web + bot: DML only
SELECT format('CREATE ROLE questersboard_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L', :'app_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_app') \gexec
-- worker: outbox, reminders, projections, reference import
SELECT format('CREATE ROLE questersboard_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L', :'worker_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_worker') \gexec
-- readonly: public projections only (reporting / feeds tooling)
SELECT format('CREATE ROLE questersboard_readonly LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L', :'readonly_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_readonly') \gexec

ALTER ROLE questersboard_app SET statement_timeout = '15s';
ALTER ROLE questersboard_worker SET statement_timeout = '60s';
ALTER ROLE questersboard_readonly SET default_transaction_read_only = on;

SELECT 'CREATE DATABASE questersboard OWNER questersboard_admin ENCODING ''UTF8'' TEMPLATE template0'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'questersboard') \gexec

REVOKE ALL ON DATABASE questersboard FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE questersboard TO questersboard_admin;
GRANT CONNECT ON DATABASE questersboard TO questersboard_app, questersboard_worker, questersboard_readonly;

\connect questersboard
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO questersboard_admin;
GRANT USAGE ON SCHEMA public TO questersboard_app, questersboard_worker, questersboard_readonly;
-- btree_gist is needed for the member-overlap exclusion constraint. Creating an
-- extension needs superuser (it isn't "trusted"), so it is done here, once.
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;
-- Table grants are applied by the migration itself (0001) and by default privileges:
ALTER DEFAULT PRIVILEGES FOR ROLE questersboard_admin IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE questersboard_admin IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM PUBLIC;
