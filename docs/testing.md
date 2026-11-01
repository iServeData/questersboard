# Testing

The suite runs against a **real, disposable PostgreSQL cluster**, because the things that matter most
(the capacity trigger, the per-member exclusion constraint, role grants, `pg_hba` rules) only exist
there. Discord, Blizzard and AI providers are replaced by clearly labelled FIXTURE fakes
(`httpx.MockTransport`, `tests/fake_discord.py`); no test contacts a live service.

## One-time test cluster

Any PostgreSQL ≥ 16 works. Example with a throwaway cluster on port 5433 (never the production cluster):

```sh
sudo -u postgres /usr/lib/postgresql/<ver>/bin/initdb -D /tmp/pgt/data --auth-local=peer --auth-host=scram-sha-256
sudo install -o postgres -m 0640 deploy/postgres/pg_hba.conf /tmp/pgt/data/pg_hba.conf
sudo -u postgres /usr/lib/postgresql/<ver>/bin/pg_ctl -D /tmp/pgt/data \
     -o "-p 5433 -k /tmp/pgt -c listen_addresses=127.0.0.1" -l /tmp/pgt/log start
umask 077; cat > test_pw.env <<PW               # keep OUTSIDE the repository
WQT_ADMIN_PW=$(openssl rand -hex 16)
WQT_APP_PW=$(openssl rand -hex 16)
WQT_WORKER_PW=$(openssl rand -hex 16)
WQT_READONLY_PW=$(openssl rand -hex 16)
PW
set -a; . ./test_pw.env; set +a
sudo -u postgres psql -h /tmp/pgt -p 5433 -v ON_ERROR_STOP=1 -v admin_pw="$WQT_ADMIN_PW" -v app_pw="$WQT_APP_PW" \
     -v worker_pw="$WQT_WORKER_PW" -v readonly_pw="$WQT_READONLY_PW" -f deploy/postgres/roles.sql
```

The harness migrates the schema as `wowquester_admin`, then truncates application tables before each
test. Environment: `WQT_PG_HOST` (127.0.0.1), `WQT_PG_PORT` (5433), `WQT_PG_SOCKET` (/tmp/pgt) and the
four `WQT_*_PW` passwords. If the cluster isn't reachable, database tests are **skipped**, not passed:
check the summary line.

## Running

```sh
set -a; . /path/to/test_pw.env; set +a
WOWQUESTER_DENYLIST=/path/to/private/denylist.txt .venv/bin/python -m pytest -q tests
```

`WOWQUESTER_DENYLIST` points at a private, one-term-per-line list (community names, domains, account
names) that must never appear in the repository; `tests/test_sanitized.py` also scans for Discord IDs,
tokens, private keys and passwords in URLs. Without the denylist the generic checks still run.

## What is covered

| File | Area |
| --- | --- |
| `test_scheduling.py` | capacity limit 1 and >1, half-open intervals, exclusive events, member conflicts, drafts/tentative, concurrency, reschedule, recurrence and DST |
| `test_signups_roster.py` | characters (realm rule), signups, approval, waitlist promotion, duplicates, permissions, copy |
| `test_chronicle.py` | outcomes never inferred, privacy scan, review flow, projections, revisions, recognition removal, trusted authors, uploads, co-host vs co-author |
| `test_series_recommendations.py` | edit one vs this-and-future, explainable recommendations |
| `test_web.py` | login state, sessions, CSRF, rate limits, headers, realm rule, exclusive role, chronicle pages and feeds, calendars, autocomplete, Blizzard check, Battle.net link/unlink, per-event consent |
| `test_worker.py` | canonical post edit/recreate, outbox retry and dead letters, coalescing, reminders, overrun nudges |
| `test_bot.py` | command payloads, persistent component IDs, interaction handling |
| `test_integrations.py` | Blizzard (Forever unsupported, Retail realm, 429/5xx, ptr rejected), Discord REST errors, ICS, AI safety and fallback, config validation |
| `test_cli.py` | check-config exit codes and no secret output, prune, outbox, calendar revoke, sync-commands guard |
| `test_db_hardening.py` | pg_hba rejections, no `root`, single superuser, grants per role, reversible migration |
| `test_sanitized.py` | repository sanitization |

## Not covered by automated tests

Live Discord, live Blizzard, a live AI provider, the production host's PostgreSQL, Nginx and systemd.
Backup/restore and `systemd-analyze verify` were checked by hand in the sandbox (see `backup_restore.md`).

## Local preview

`tools/preview_server.py` runs the real app against the test cluster with FIXTURE login and seeded
events. It **truncates the test database** when started with `--reset`.
