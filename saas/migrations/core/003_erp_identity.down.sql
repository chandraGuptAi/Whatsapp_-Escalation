-- MANUAL ONLY. Fails if ERP-only users exist (they have no email); remove them first.
BEGIN;
SET LOCAL search_path TO app, public;
DROP FUNCTION IF EXISTS upsert_external_user(uuid, text, text, text, bytea, text);
DROP FUNCTION IF EXISTS resolve_identity_provider(text);
ALTER TABLE users
    DROP CONSTRAINT IF EXISTS users_external_pair,
    DROP CONSTRAINT IF EXISTS users_identity_present,
    DROP CONSTRAINT IF EXISTS users_external_identity_uq,
    DROP COLUMN IF EXISTS phone_enc,
    DROP COLUMN IF EXISTS phone_hash,
    DROP COLUMN IF EXISTS external_id,
    DROP COLUMN IF EXISTS identity_provider_id,
    ALTER COLUMN email SET NOT NULL;
DROP TABLE IF EXISTS identity_providers;
DELETE FROM schema_migrations WHERE version = 3;
COMMIT;
