-- (Re)create an empty QuestersBoard database with the right ownership, extension and PUBLIC revokes.
-- No passwords. Used by restore (scratch drill or approved production restore). Run as postgres over the socket:
--   sudo -u postgres psql -v ON_ERROR_STOP=1 -v db=questersboard_restore_check -f deploy/postgres/database_init.sql
-- Roles must already exist (roles.sql).
\set ON_ERROR_STOP on
SELECT format('DROP DATABASE IF EXISTS %I WITH (FORCE)', :'db') \gexec
SELECT format('CREATE DATABASE %I OWNER questersboard_admin ENCODING ''UTF8'' TEMPLATE template0', :'db') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO questersboard_admin', :'db') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO questersboard_app, questersboard_worker, questersboard_readonly', :'db') \gexec
\connect :db
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO questersboard_admin;
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;
