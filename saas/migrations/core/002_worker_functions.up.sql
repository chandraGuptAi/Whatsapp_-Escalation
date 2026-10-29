-- Worker entry points. Workers connect as app_user with no tenant set, so RLS hides every job;
-- these SECURITY DEFINER functions hand out exactly one job/schedule/session at a time.
-- After claiming, the worker opens a tenant_transaction for that job's tenant to do the work.

BEGIN;
SET LOCAL search_path TO app, public;

-- Re-claims 'running' jobs whose lease expired (worker crashed).
CREATE INDEX jobs_expired_lease_idx ON jobs (locked_until) WHERE status = 'running';

CREATE FUNCTION claim_job(p_worker text, p_lease_seconds int, p_modules text[])
RETURNS TABLE (job_id uuid, tenant_id uuid, module_key text, kind text, options jsonb, attempts smallint)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = app, pg_temp AS $$
    UPDATE jobs j
       SET status = 'running',
           locked_by = p_worker,
           locked_until = now() + make_interval(secs => p_lease_seconds),
           attempts = j.attempts + 1,
           started_at = COALESCE(j.started_at, now())
     WHERE j.id = (
            SELECT q.id
              FROM jobs q
             WHERE q.module_key = ANY (p_modules)
               AND (q.status = 'queued' OR (q.status = 'running' AND q.locked_until < now()))
             ORDER BY q.created_at
             FOR UPDATE SKIP LOCKED
             LIMIT 1)
    RETURNING j.id, j.tenant_id, j.module_key, j.kind, j.options, j.attempts
$$;

-- Leases due schedules for 5 minutes; the scheduler then enqueues the job and sets the real next_run_at.
CREATE FUNCTION claim_due_schedules(p_limit int)
RETURNS TABLE (schedule_id uuid, tenant_id uuid, preset_id uuid, module_key text, options jsonb,
               cron text, timezone text, due_at timestamptz)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = app, pg_temp AS $$
    WITH due AS (
        SELECT s.id, s.next_run_at
          FROM schedules s
          JOIN tenants t ON t.id = s.tenant_id AND t.status = 'active'
         WHERE s.enabled AND s.next_run_at <= now()
         ORDER BY s.next_run_at
         FOR UPDATE OF s SKIP LOCKED
         LIMIT p_limit)
    UPDATE schedules s
       SET next_run_at = now() + interval '5 minutes'
      FROM due, report_presets p
     WHERE s.id = due.id AND p.id = s.preset_id
    RETURNING s.id, s.tenant_id, s.preset_id, p.module_key, p.options, s.cron, s.timezone, due.next_run_at
$$;

-- Ingest workers split sessions between them by hashing the tenant id.
CREATE FUNCTION list_wa_sessions(p_shard int, p_shards int)
RETURNS TABLE (session_id uuid, tenant_id uuid, status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, pg_temp AS $$
    SELECT s.id, s.tenant_id, s.status
      FROM wa_sessions s
      JOIN tenants t ON t.id = s.tenant_id AND t.status = 'active'
     WHERE s.status <> 'logged_out'
       AND abs(hashtext(s.tenant_id::text)) % p_shards = p_shard
$$;

REVOKE ALL ON FUNCTION claim_job(text, int, text[]), claim_due_schedules(int), list_wa_sessions(int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_job(text, int, text[]), claim_due_schedules(int), list_wa_sessions(int, int)
    TO app_user;

INSERT INTO schema_migrations (version, name) VALUES (2, 'worker_functions');

COMMIT;
