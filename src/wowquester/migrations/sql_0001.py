"""Database-side enforcement that must hold no matter which code path writes.

* member_reservations: EXCLUDE (member_id =, during &&). A person cannot hold two
  overlapping WoW reservations (organizer, co-host or confirmed participant),
  whichever character they bring. Back-to-back is fine: ranges are [start, end).
* event_reservations: trigger that takes the global schedule advisory lock, then
  - rejects any overlap with an exclusive event (either side),
  - computes the peak number of simultaneous events inside the new range
    (sweep over range start points) and rejects it above schedule_policy.max_simultaneous.
  An exclusion constraint alone cannot express a limit above one, hence the lock + sweep.
* signups: trigger that locks the event row and enforces per-role quantity and
  the overall participant limit for confirmed signups.
* schedule_policy: single row mirrored from config [scheduling] at service start.
"""

UPGRADE_SQL = r"""
ALTER TABLE member_reservations
  ADD CONSTRAINT ex_member_overlap EXCLUDE USING gist (member_id WITH =, during WITH &&);

ALTER TABLE event_reservations
  ADD CONSTRAINT ck_event_res_bounds CHECK (lower_inc(during) AND NOT upper_inc(during) AND NOT isempty(during));
ALTER TABLE member_reservations
  ADD CONSTRAINT ck_member_res_bounds CHECK (lower_inc(during) AND NOT upper_inc(during) AND NOT isempty(during));
CREATE INDEX ix_event_res_during ON event_reservations USING gist (during);

INSERT INTO schedule_policy (id, max_simultaneous) VALUES (1, 1) ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION wq_check_event_reservation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
  lim integer;
  peak integer;
  ex_conflict bigint;
BEGIN
  PERFORM pg_advisory_xact_lock(727001);
  SELECT max_simultaneous INTO lim FROM schedule_policy WHERE id = 1;
  IF lim IS NULL OR lim < 1 THEN lim := 1; END IF;

  SELECT r.event_id INTO ex_conflict FROM event_reservations r
   WHERE r.event_id <> NEW.event_id AND r.during && NEW.during AND (r.exclusive OR NEW.exclusive)
   LIMIT 1;
  IF ex_conflict IS NOT NULL THEN
    RAISE EXCEPTION 'wq_exclusive_conflict:%', ex_conflict USING ERRCODE = '23P01';
  END IF;

  WITH o AS (
    SELECT during FROM event_reservations WHERE event_id <> NEW.event_id AND during && NEW.during
    UNION ALL SELECT NEW.during
  ), pts AS (
    SELECT greatest(lower(during), lower(NEW.during)) AS p FROM o
  )
  SELECT max((SELECT count(*) FROM o WHERE o.during @> pts.p)) INTO peak FROM pts;

  IF peak > lim THEN
    RAISE EXCEPTION 'wq_capacity_conflict:%:%', peak, lim USING ERRCODE = '23P01';
  END IF;
  RETURN NEW;
END
$fn$;

CREATE TRIGGER trg_event_reservation_check
  BEFORE INSERT OR UPDATE OF during, exclusive ON event_reservations
  FOR EACH ROW EXECUTE FUNCTION wq_check_event_reservation();

CREATE OR REPLACE FUNCTION wq_check_signup_capacity() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
  qty integer;
  lim integer;
  n integer;
BEGIN
  IF NEW.state <> 'confirmed' THEN RETURN NEW; END IF;
  IF TG_OP = 'UPDATE' AND OLD.state = 'confirmed' AND OLD.role_key IS NOT DISTINCT FROM NEW.role_key THEN
    RETURN NEW;
  END IF;
  SELECT participant_limit INTO lim FROM events WHERE id = NEW.event_id FOR UPDATE;
  IF NEW.role_key IS NOT NULL THEN
    SELECT quantity INTO qty FROM event_roles WHERE event_id = NEW.event_id AND key = NEW.role_key;
    IF qty IS NULL THEN
      RAISE EXCEPTION 'wq_unknown_role:%', NEW.role_key USING ERRCODE = '23514';
    END IF;
    SELECT count(*) INTO n FROM signups
     WHERE event_id = NEW.event_id AND state = 'confirmed' AND role_key = NEW.role_key AND id <> NEW.id;
    IF n + 1 > qty THEN
      RAISE EXCEPTION 'wq_role_full:%', NEW.role_key USING ERRCODE = '23514';
    END IF;
  END IF;
  IF lim IS NOT NULL THEN
    SELECT count(*) INTO n FROM signups WHERE event_id = NEW.event_id AND state = 'confirmed' AND id <> NEW.id;
    IF n + 1 > lim THEN
      RAISE EXCEPTION 'wq_event_full' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END
$fn$;

CREATE TRIGGER trg_signup_capacity
  BEFORE INSERT OR UPDATE OF state, role_key ON signups
  FOR EACH ROW EXECUTE FUNCTION wq_check_signup_capacity();

-- Least-privilege grants, applied only when the deployment roles exist (deploy/postgres/roles.sql).
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wowquester_app') THEN
    GRANT USAGE ON SCHEMA public TO wowquester_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO wowquester_app;
    REVOKE INSERT, UPDATE, DELETE ON audit_log FROM wowquester_app;
    GRANT INSERT ON audit_log TO wowquester_app;
    REVOKE UPDATE, DELETE ON config_changes FROM wowquester_app;
    REVOKE ALL ON alembic_version FROM wowquester_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO wowquester_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wowquester_worker') THEN
    GRANT USAGE ON SCHEMA public TO wowquester_worker;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO wowquester_worker;
    REVOKE SELECT ON web_sessions, calendar_tokens, battlenet_links FROM wowquester_worker;
    GRANT INSERT, UPDATE, DELETE ON outbox, reminder_deliveries, rate_buckets, api_cache, api_probes,
          public_events, public_chronicle, event_reservations, member_reservations, config_changes
          TO wowquester_worker;
    GRANT UPDATE (state, actual_end, discord_channel_id, discord_message_id, updated_at, version) ON events TO wowquester_worker;
    GRANT UPDATE ON schedule_policy TO wowquester_worker;
    GRANT INSERT, UPDATE ON reference_options TO wowquester_worker;
    GRANT INSERT ON audit_log TO wowquester_worker;
    REVOKE ALL ON alembic_version FROM wowquester_worker;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO wowquester_worker;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wowquester_readonly') THEN
    GRANT USAGE ON SCHEMA public TO wowquester_readonly;
    GRANT SELECT ON public_chronicle, public_events TO wowquester_readonly;
  END IF;
END
$grants$;
"""

DOWNGRADE_SQL = r"""
DROP TRIGGER IF EXISTS trg_signup_capacity ON signups;
DROP FUNCTION IF EXISTS wq_check_signup_capacity();
DROP TRIGGER IF EXISTS trg_event_reservation_check ON event_reservations;
DROP FUNCTION IF EXISTS wq_check_event_reservation();
"""
