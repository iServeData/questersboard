# PostgreSQL hardening checklist

"Current" is what is known about the production host. No database, SSH or firewall change has been
made there; the production host was not accessed from this project. Every "proposed" item was applied to
a disposable local PostgreSQL cluster and tested (`tests/test_db_hardening.py`), not to production.

| # | Control | Current (production) | Proposed | Verified locally |
| --- | --- | --- | --- | --- |
| 1 | PostgreSQL installed | not confirmed; WoWQuester needs PostgreSQL 16 (Ubuntu 24.04 package) | `apt install postgresql-16` (approval) | PG 18 test cluster |
| 2 | No role named `root` | unknown | `roles.sql` refuses to run if one exists; `pg_hba` rejects `root` on socket and TCP | `test_no_role_named_root_and_no_extra_superusers` |
| 3 | `postgres` superuser peer-only | Ubuntu default is `local all postgres peer` | keep; reject `postgres` over TCP | `test_superuser_and_root_password_login_rejected_over_tcp` |
| 4 | Only one superuser | unknown | no other role has `SUPERUSER`, `CREATEROLE`, `CREATEDB`, `REPLICATION`, `BYPASSRLS` | same test |
| 5 | Schema owner is not a superuser | n/a | `wowquester_admin` owns DB and schema, runs migrations; not superuser | `test_admin_role_can_migrate_down_and_up` |
| 6 | Separate service roles | n/a | `wowquester_app`, `wowquester_worker`, `wowquester_readonly`; `NOINHERIT`; statement timeouts | `test_service_roles_cannot_run_ddl`, `test_worker_cannot_read_sessions_or_tokens`, `test_readonly_sees_only_projections` |
| 7 | Audit log append-only for services | n/a | app/worker INSERT only; prune via admin role | `test_app_cannot_rewrite_audit_log` |
| 8 | `PUBLIC` revoked | Ubuntu 16 default lets PUBLIC connect to new DBs | `REVOKE ALL ON DATABASE … FROM PUBLIC`, `REVOKE ALL ON SCHEMA public FROM PUBLIC` | `test_unknown_role_rejected`, grants checked in restore drill |
| 9 | `pg_hba.conf`: socket/localhost SCRAM, everything else rejected | Ubuntu default allows `scram-sha-256` from 127.0.0.1 for all | `deploy/postgres/pg_hba.conf` (explicit reject lines, no `trust`) | `test_socket_login_rejected_for_non_matching_os_user`, `test_unknown_role_rejected` |
| 10 | Listen on localhost only | Ubuntu default `localhost` | `listen_addresses = 'localhost'`; no firewall change needed | test cluster binds 127.0.0.1 |
| 11 | SCRAM password hashing | Ubuntu 16 default `scram-sha-256` | set explicitly | role creation |
| 12 | Log connections, DDL and role changes | Ubuntu default: off | `log_connections`, `log_disconnections`, `log_statement = 'ddl'`; `roles.sql` disables statement logging for its own session so passwords don't reach the log | config file |
| 13 | Passwords never on argv or in files in the repo | n/a | random passwords → systemd credentials; `roles.sql` reads them from the environment | sanitize check |
| 14 | Sensitive fields encrypted | n/a | Battle.net snapshot Fernet-encrypted (`field-key`); sessions and calendar tokens stored as hashes | `test_battlenet_link_state_single_use_and_marks_owned`, `test_session_cookie_flags_and_hash_only` |
| 15 | Backups encrypted | n/a | `pg_dump -Fc` → GnuPG public-key encryption; private key kept off the host | manual drill (see `backup_restore.md`) |
| 16 | Reversible migrations | n/a | Alembic `0001` with tested downgrade | `test_admin_role_can_migrate_down_and_up` |
| 17 | Idle transaction timeout, lock-wait logging | default off | `idle_in_transaction_session_timeout = 60s`, `log_lock_waits = on` | config file |

## Files

- `deploy/postgres/pg_hba.conf`: complete proposed file. If PostgreSQL already serves something else on
  the host, merge the marked WoWQuester lines above that service's lines instead.
- `deploy/postgres/postgresql_wowquester.conf`: drop into `/etc/postgresql/16/main/conf.d/`.
- `deploy/postgres/roles.sql`: roles, database, revokes, extension, default privileges. Idempotent.
- `deploy/postgres/database_init.sql`: empty database with the right owner (restores and drills).
- Grants for tables live in the migration (`src/wowquester/migrations/sql_0001.py`) so they follow the schema.

## Limits

- **OS root can still read everything.** Root can read the data directory, attach to processes, decrypt
  host-bound credentials and read the field key. Database roles and encryption protect against a
  compromised service or a stolen backup, not against root on the host.
- The field key is on the host, so field encryption protects against database-only access (SQL dumps,
  a compromised read role), not against host compromise.
- `ssl = off` is safe only because PostgreSQL listens on loopback/socket. If it ever has to listen
  elsewhere, enable TLS and `hostssl` lines first.
- pgaudit is not required; enabling it is an optional later step.
