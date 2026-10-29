-- MANUAL ONLY. Fails if sessions exist with status 'error' or without phone_hash.
BEGIN;
SET LOCAL search_path TO app, public;
DROP FUNCTION list_wa_sessions(int, int);
CREATE FUNCTION list_wa_sessions(p_shard int, p_shards int)
RETURNS TABLE (session_id uuid, tenant_id uuid, status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, pg_temp AS $$
    SELECT s.id, s.tenant_id, s.status
      FROM wa_sessions s
      JOIN tenants t ON t.id = s.tenant_id AND t.status = 'active'
     WHERE s.status <> 'logged_out'
       AND abs(hashtext(s.tenant_id::text)) % p_shards = p_shard
$$;
REVOKE ALL ON FUNCTION list_wa_sessions(int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_wa_sessions(int, int) TO app_user;
ALTER TABLE wa_sessions
    DROP COLUMN restart_requested_at, DROP COLUMN qr_expires_at, DROP COLUMN qr_image,
    DROP COLUMN status_message, DROP COLUMN device_jid, DROP COLUMN label,
    DROP CONSTRAINT wa_sessions_status_check,
    ADD CONSTRAINT wa_sessions_status_check
        CHECK (status IN ('pending', 'pairing', 'connected', 'disconnected', 'logged_out')),
    ALTER COLUMN phone_hash SET NOT NULL;
DELETE FROM schema_migrations WHERE version = 5;
COMMIT;
