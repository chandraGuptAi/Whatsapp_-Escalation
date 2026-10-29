-- Runtime state for WhatsApp sessions, shared between the API (QR, reconnect button) and the
-- ingest worker (owns the connection). The WhatsApp login keys themselves live in neonize's own
-- store (WA_STORE_DATABASE); device_jid tells the worker which stored device to resume.

BEGIN;
SET LOCAL search_path TO app, public;

ALTER TABLE wa_sessions DROP CONSTRAINT wa_sessions_status_check;
ALTER TABLE wa_sessions
    ALTER COLUMN phone_hash DROP NOT NULL,                -- unknown until the phone is paired
    ADD CONSTRAINT wa_sessions_status_check
        CHECK (status IN ('pending', 'pairing', 'connected', 'disconnected', 'logged_out', 'error')),
    ADD COLUMN label text NOT NULL DEFAULT '',
    ADD COLUMN device_jid text,                           -- "user:device@server" once paired
    ADD COLUMN status_message text,
    ADD COLUMN qr_image text,                             -- data:image/svg+xml;base64,... while pairing
    ADD COLUMN qr_expires_at timestamptz,
    ADD COLUMN restart_requested_at timestamptz;

DROP FUNCTION list_wa_sessions(int, int);
CREATE FUNCTION list_wa_sessions(p_shard int, p_shards int)
RETURNS TABLE (session_id uuid, tenant_id uuid, status text, device_jid text, restart_requested_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, pg_temp AS $$
    SELECT s.id, s.tenant_id, s.status, s.device_jid, s.restart_requested_at
      FROM wa_sessions s
      JOIN tenants t ON t.id = s.tenant_id AND t.status = 'active'
     WHERE s.status <> 'logged_out'
       AND abs(hashtext(s.tenant_id::text)) % p_shards = p_shard
$$;
REVOKE ALL ON FUNCTION list_wa_sessions(int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_wa_sessions(int, int) TO app_user;

INSERT INTO schema_migrations (version, name) VALUES (5, 'wa_session_runtime');

COMMIT;
