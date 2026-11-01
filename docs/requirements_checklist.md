# Requirements checklist

Status key:
- **Done, tested**: implemented and covered by automated tests against a real local PostgreSQL.
- **Done, fixture-tested**: implemented; the external service is replaced by a labelled FIXTURE
  (mock transport or fake client). The live integration has NOT been exercised.
- **Done, manual check**: implemented and checked by hand in the sandbox (no pytest).
- **Externally unverified**: needs a live service, credentials or the production host.
- **Blocked**: can't be completed until something outside this project changes.

Test file names refer to `tests/`.

## Stack and structure

| Requirement | Status | Evidence |
| --- | --- | --- |
| Python, discord.py, FastAPI, Jinja2 + minimal JS, PostgreSQL, SQLAlchemy + Alembic, outbox worker, systemd, pytest | Done, tested | `pyproject.toml`, `requirements.lock` (hash-pinned) |
| No React/Redis/Celery/Kubernetes/microservices | Done | one package, three systemd units |
| One config for all three services, validated at startup, refuse to start, never print secrets | Done, tested | `test_cli.py::test_invalid_config_exits_78`, `test_check_config_never_prints_secret_values` |
| Config at `/etc/wowquester/config.toml` (root:wowquester 0640); secrets as systemd credentials, `secrets.env` fallback | Done (files and units) / Externally unverified on host | `deploy/config.example.toml`, `deploy/secrets.env.example`, units use `ImportCredential=` |
| Lowercase underscore filenames, no spaces | Done | systemd unit names keep systemd's hyphen convention |
| Repository sanitized (no community names, IDs, tokens, keys) | Done, tested | `test_sanitized.py` + private denylist |

## Characters and Blizzard

| Requirement | Status | Evidence |
| --- | --- | --- |
| Forever: ruleset, realm disabled; Retail: realm required | Done, tested | `test_signups_roster.py::test_forever_has_no_realm_retail_requires_realm`, `test_web.py::test_character_realm_rule_via_web` |
| Race/class validated per edition and faction | Done, tested | `test_invalid_race_class_rejected` |
| Members can only edit their own characters | Done, tested | `test_cannot_edit_others_character`, lookup 404 for others in `test_check_with_blizzard_retail_is_existence_only` |
| Never use `classic-ptr`; never substitute Retail/Classic for Forever | Done, fixture-tested | `test_ptr_namespace_rejected_by_config`, `test_forever_is_unsupported_and_never_falls_back_to_retail`, `test_check_with_blizzard_forever_is_refused_without_requests` (zero requests) |
| API lookup ≠ ownership | Done, fixture-tested | `api_matched` vs `ownership_verified`; `test_retail_lookup_retries_429_and_is_not_ownership` |
| Optional Battle.net ownership link (state single-use, user token never stored, snapshot encrypted) | Done, fixture-tested | `test_battlenet_link_state_single_use_and_marks_owned` |
| Retries, 429 handling, caching, rate limiting | Done, fixture-tested | `test_retail_404_is_not_found_and_5xx_exhausts_to_api_error` |
| Live Blizzard Retail API | Externally unverified | needs a Blizzard API client; run `wowquester-admin blizzard probe retail --region us` |
| Forever API data | **Blocked** | Blizzard hasn't published Forever namespaces (see `plan.md`); ships as `unsupported` |

## Scheduling

| Requirement | Status | Evidence |
| --- | --- | --- |
| Only WoWQuester events count; `max_simultaneous_wow_events` default 1 | Done, tested | `test_scheduling.py::test_default_limit_one_blocks_overlap` |
| Limits above 1 (not an exclusion constraint) | Done, tested | `test_limit_two_and_peak_sweep` |
| Half-open `[start,end)` UTC + IANA zone, DST | Done, tested | `test_back_to_back_allowed_half_open`, `test_weekly_series_keeps_local_time_across_dst`, `test_nonexistent_local_time_skipped_with_note` |
| Exclusive events block everything and need the role | Done, tested | `test_exclusive_blocks_all_and_needs_role`, `test_web.py::test_exclusive_checkbox_needs_role` |
| Member conflicts across all characters | Done, tested | `test_member_conflict_across_characters` |
| Tentative, waitlisted, pending and drafts reserve nothing | Done, tested | `test_tentative_and_pending_do_not_reserve`, `test_drafts_reserve_nothing` |
| Completion/cancel release reservations | Done, tested | `test_completion_and_cancel_release` |
| Transactional enforcement under concurrency | Done, tested | `test_concurrent_publish_only_one_wins`, `test_concurrent_join_last_slot` |
| Reschedule revalidates and requires reconfirmation | Done, tested | `test_reschedule_requires_reconfirm_and_bumps_conflicts` |
| Overruns never auto-extended; one nudge | Done, tested | `test_worker.py::test_overrun_nudged_once_and_not_auto_extended` |

## Signups and lifecycle

| Requirement | Status | Evidence |
| --- | --- | --- |
| Optional roles, presets + custom, open or approval policy | Done, tested | `test_approval_policy_and_permissions` |
| States pending/confirmed/tentative/waitlisted/withdrawn/rejected; no duplicates; waitlist promotion | Done, tested | `test_duplicate_signup_and_wrong_character`, `test_waitlist_promotion_on_leave`, `test_ruleset_mismatch` |
| Attendance separate from signup | Done, tested | `test_chronicle.py::test_end_creates_draft_and_attendance_starts_unknown` |
| Draft → Scheduled → In progress → Completed, Cancelled; managers only, server-side | Done, tested | `test_only_managers_start_end_cancel`, `test_web.py::test_removed_role_stops_privileged_action` |
| Copy creates a clean draft | Done, tested | `test_copy_makes_clean_draft` |
| Full audit trail | Done, tested | every domain change calls `audit.record`; app role INSERT-only (`test_app_cannot_rewrite_audit_log`) |
| One canonical Discord post, edited in place, recreated if deleted, persistent buttons | Done, fixture-tested | `test_worker.py::test_canonical_post_is_edited_and_reposted_when_deleted`, `test_bot.py::test_persistent_custom_id_matches_render` |
| Slash commands (`/quest`, `/character`), edition autocomplete | Done, fixture-tested | `test_bot.py::test_command_payload_shape`; registration is dry-run until approved |
| Live Discord gateway, posting, DMs, command registration | Externally unverified | needs the bot token, guild and approval |

## Chronicle

| Requirement | Status | Evidence |
| --- | --- | --- |
| Never infer outcomes; organizers state them | Done, tested | `test_outcome_never_inferred_and_privacy_scan` |
| Public only after approval; built from projections only | Done, tested | `test_review_publish_projection_is_privacy_safe`, `test_web.py::test_chronicle_public_only_after_approval_and_media_gate`, `test_readonly_sees_only_projections` |
| Per-event consent; neutral descriptions; no Discord IDs leak | Done, tested | same tests; privacy scan blocks opted-out names |
| Recognition removable after publication, immediately | Done, tested | `test_recognition_removal_applies_immediately` |
| Review flow incl. changes requested / rejected → edited → pending | Done, tested | `test_material_edit_keeps_old_version_until_approved` |
| Co-host and co-author distinct | Done, tested | `test_cohost_is_not_coauthor_until_assigned` |
| Trusted authors self-publish (still checked) | Done, tested | `test_trusted_author_self_publishes_but_is_still_checked` |
| Material edits re-gate; old version stays public; revision history | Done, tested | `test_material_edit_keeps_old_version_until_approved` |
| Restricted uploads (type sniffing, re-encode to WebP, metadata stripped, size/count limits, review gate) | Done, tested | `test_bad_upload_rejected`, media gate in web test |
| Safe rendering (Markdown subset, bleach, CSP) | Done, tested | `test_rendering_is_safe`, `test_security_headers_and_health` |

## Website

| Requirement | Status | Evidence |
| --- | --- | --- |
| `/wow/chronicle/` archive, story pages, latest widget, JSON feed, RSS and Atom | Done, tested | web chronicle test (`feed.json`, `feed.xml`, `feed.atom`, `latest.json`) |
| Routine publication needs no site commits or deployments | Done | served by WoWQuester behind Nginx; no site files written |
| Matches the WoW page design | Done, manual check | reuses the site stylesheet via `[branding]`; previews in `docs/previews/` |
| WoW page schedule shows upcoming events | Done, manual check | `site_widget/wow_quester.js` + `upcoming.json` (tested in `test_create_publish_join_and_upcoming_json`); site patch kept outside this repo for prototype review |
| WoW page Community status and Next event cards (Live only after the organiser launches; voice count from the DB once FU-1 exists) | Done, manual check in a local browser | `status.json` (`tests/test_status.py`), `site_widget/wow_quester.js`; site patch kept outside this repo for prototype review |
| Mobile and desktop previews | Done, manual check | `docs/previews/` |

## Enhancements

| Requirement | Status | Evidence |
| --- | --- | --- |
| Blizzard reference data import (race/class/quests/locations) | Done, fixture-tested / Externally unverified live | `wowquester-admin blizzard import-reference`; Forever uses the packaged catalog |
| Autocomplete with free-text fallback | Done, tested | `test_autocomplete_options_and_free_text` |
| Recurring templates: edit one vs this-and-future | Done, tested | `test_series_recommendations.py::test_edit_one_detaches_and_edit_future_skips_detached` |
| Private revocable calendar + public calendar (ICS) | Done, tested | `test_personal_calendar_token_and_revocation`, `test_ics_escapes_folds_and_uses_utc`, `test_calendar_revoke_is_audited` |
| Explainable recommendations | Done, tested | `test_recommendations_explain_fit_and_skip_mismatches` |
| Opt-in AI wording: safe data only, drafts only, template fallback | Done, fixture-tested | `test_ai_payload_is_publication_safe`, `test_ai_reply_used_when_safe_and_template_otherwise`, `test_ai_disabled_or_failing_uses_template` |
| Live AI provider | Externally unverified | off by default; needs a provider key and model |

## Reliability and security

| Requirement | Status | Evidence |
| --- | --- | --- |
| Outbox, idempotency, coalescing, retries, dead letters | Done, tested | `test_worker.py` (all), `test_coalescing_keeps_one_pending_job`, `wowquester-admin outbox` |
| Reminders exactly once per offset; again after reschedule | Done, tested | `test_reminders_once_and_rescheduled_again` |
| Secure sessions (hashed IDs, HttpOnly, Secure, SameSite=Lax) | Done, tested | `test_session_cookie_flags_and_hash_only` |
| OAuth state single-use; guild membership required | Done, tested | `test_login_state_is_single_use_and_bad_state_rejected`, `test_non_guild_member_cannot_log_in` |
| CSRF + Origin check | Done, tested | `test_csrf_required_on_posts` |
| Rate limits | Done, tested | `test_rate_limit` |
| Least privilege DB roles; no `root` role; superuser peer-only; PUBLIC revoked | Done, tested (local cluster) | `test_db_hardening.py` (9 tests) |
| Field encryption (Battle.net snapshot), encrypted backups | Done, tested / manual check | Fernet `field-key`; GnuPG backup drill below |
| Retention and pruning | Done, tested | `test_prune_and_outbox_status`, `test_retention_and_review_channel_validation` |
| Structured logs without secrets | Done | journald via systemd; secrets wrapped so `repr` never shows values |
| Health checks | Done, tested | `/healthz`, `wowquester-admin health`; `test_security_headers_and_health` |
| Reversible migrations | Done, tested | `test_admin_role_can_migrate_down_and_up` |
| Backup, restore drill, rollback | Done, manual check | encrypt → decrypt → checksum → restore into scratch DB with grants → drop (sandbox) |
| systemd sandboxing | Done, manual check | `systemd-analyze verify` clean on all units |
| Host PostgreSQL install and hardening | Externally unverified | proposed in `db_hardening_checklist.md`; not applied |
