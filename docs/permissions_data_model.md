# Permissions and data model

## Who can do what

Capabilities map to Discord role IDs in `[roles]` (live-reloaded). Every entry point (web, slash
command, button, DM, worker) goes through `domain/permissions.py`, server-side. Web sessions don't
cache roles: privileged web actions re-read the member's current roles from Discord, so removing a
role in Discord stops the next action.

Base access ("member"): in the configured guild, holding `[community] member_role_ids` if set, and
for slash commands in an allowed channel.

| Action | Member | Event manager (creator / co-host) | `event_creator` | `event_moderator` | `exclusive_designator` | `chronicle_editor` | `chronicle_reviewer` | `trusted_author` | `admin` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Add/edit/remove own characters, Blizzard check, Battle.net link/unlink | ✓ | | | | | | | | |
| Sign up, tentative, leave, reconfirm, change character/role | ✓ | | | | | | | | |
| Personal calendar link (create/revoke) | ✓ | | | | | | | | |
| Create draft, publish, copy | | | ✓ | ✓ | | | | | |
| Edit, reschedule, start, end, extend, cancel, discard draft | | ✓ | | ✓ (any) | | | | | |
| Approve/reject/waitlist signups, set roles, attendance | | ✓ | | ✓ (any) | | | | | |
| Mark event exclusive | | | | | ✓ | | | | |
| Edit chronicle draft, submit for review | author, co-authors | | | | | ✓ (any) | | | |
| Assign co-authors | author | | | | | ✓ | | | |
| Approve / request changes / reject / unpublish | | | | | | | ✓ | | |
| Publish own entry without review (still privacy-checked) | | | | | | | | ✓ | |
| Withdraw own consent / remove own recognition | ✓ (immediate) | | | | | | | | |
| See who signed up (names, characters) on any event | | ✓ | | ✓ | | | | | ✓ |

Notes:
- **Co-host ≠ co-author.** Co-hosts help run the event. They can't edit its chronicle unless the
  author or an editor adds them as co-authors.
- Reviewers can't approve an entry they wrote or co-wrote.
- Configuration, catalog import, pruning and backups are host-side `wowquester-admin` commands,
  not web pages. The `admin` Discord role is reserved for that kind of web feature and today only
  grants roster visibility.
- The worker runs as a system actor for automatic transitions (reminders, nudges, projections).
  It never changes outcomes, consent or review state.

## Database roles

| Role | Login | Can |
| --- | --- | --- |
| `postgres` | local peer only | superuser; used only for `roles.sql` and restores |
| `wowquester_admin` | scram, socket/localhost | owns the schema; runs migrations, backups, pruning; **not** a superuser, can't create roles or DBs |
| `wowquester_app` (web, bot) | scram | DML on application tables; `audit_log` INSERT only; no `alembic_version` |
| `wowquester_worker` | scram | read most tables (not `web_sessions`, `calendar_tokens`, `battlenet_links`); write outbox, reminders, projections, reservations, reference options; limited `events` columns |
| `wowquester_readonly` | scram, read-only transactions | `public_events`, `public_chronicle` only |

`PUBLIC` has no rights on the database or schema. No role named `root` exists (the role script refuses
to run if one does).

## Tables

| Area | Tables | Notes |
| --- | --- | --- |
| People | `members`, `web_sessions`, `calendar_tokens`, `battlenet_links` | sessions and calendar tokens stored as SHA-256 hashes; Battle.net snapshot Fernet-encrypted |
| Roster | `characters`, `reference_options` | edition, ruleset (Forever) or realm (Retail), faction, race, class, roles; verification `self_reported → api_matched → ownership_verified` |
| Events | `events`, `event_series`, `event_cohosts`, `event_roles`, `signups`, `attendance` | UTC `[start,end)` + IANA zone; version counter for idempotent posts |
| Scheduling | `schedule_policy`, `event_reservations`, `member_reservations` | capacity trigger with advisory lock; `btree_gist` no-overlap per member |
| Reminders | `reminder_deliveries` | PK `(event, member, offset, start_at)` → once per offset, again after reschedule |
| Chronicle | `chronicle_entries`, `chronicle_revisions`, `chronicle_coauthors`, `chronicle_uploads`, `recognition_removals` | every revision kept; published revision pointer separate from latest |
| Public projections | `public_events`, `public_chronicle` | the only tables public pages and feeds read; no Discord IDs, counts only, approved text only |
| Delivery | `outbox` | kind, idempotency key, attempts, next attempt, dead-letter state |
| Ops | `audit_log`, `config_changes`, `api_cache`, `api_probes`, `rate_buckets` | audit append-only for app/worker |

## Personal data and retention

| Data | Kept | How removed |
| --- | --- | --- |
| Discord ID, display name | while the member uses WoWQuester | "Remove me" (`/me/remove`, typed confirmation) or `wowquester-admin erase-member <discord_id> --yes`: withdraws signups, cancels hosted upcoming events, deletes characters (used ones blanked to "(removed)"), rewrites their names out of every Chronicle revision, deletes calendar/Battle.net/reminder/session rows and replaces the Discord ID with `erased:<id>`. Audit rows age out with `audit_days` |
| Web sessions | until expiry + `session_days_after_expiry` | web housekeeping, `wowquester-admin prune` |
| Finished outbox jobs | `outbox_done_days` (14) | worker housekeeping |
| API cache | `api_cache_days` (30) | worker housekeeping |
| Withdrawn/rejected signups of past events | `withdrawn_signup_days` (365) | prune |
| Audit log | `audit_days` (400) | prune, admin role only; services can only append |
| Battle.net snapshot | until the member clicks "Forget my Battle.net link" | deleted; their ownership-verified characters revert to self-reported |
| Personal calendar token | until revoked | member or `wowquester-admin calendar-revoke` |
| Chronicle text | indefinitely once published | unpublish; recognition removal and consent withdrawal apply immediately |
| Backups | 30 days (`WOWQUESTER_BACKUP_KEEP_DAYS`) | rotated by `backup.sh` |

Limits: anyone with OS root on the host can read the database files, process memory and decrypted
credentials. Encryption protects backups and specific fields against database-only access, not against root.
