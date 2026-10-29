BEGIN;
SET LOCAL search_path TO app, public;
DROP FUNCTION IF EXISTS claim_job(text, int, text[]);
DROP FUNCTION IF EXISTS claim_due_schedules(int);
DROP FUNCTION IF EXISTS list_wa_sessions(int, int);
DROP INDEX IF EXISTS jobs_expired_lease_idx;
DELETE FROM schema_migrations WHERE version = 2;
COMMIT;
