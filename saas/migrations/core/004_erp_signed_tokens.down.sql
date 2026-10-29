-- MANUAL ONLY. Fails while erp_jwt providers exist; delete or convert them first.
BEGIN;
SET LOCAL search_path TO app, public;
DROP FUNCTION IF EXISTS resolve_identity_provider_by_issuer(text);
DROP INDEX IF EXISTS identity_providers_issuer_uq;
ALTER TABLE identity_providers
    DROP CONSTRAINT IF EXISTS identity_providers_kind_fields,
    DROP CONSTRAINT identity_providers_kind_check,
    ALTER COLUMN verify_url SET NOT NULL,
    ADD CONSTRAINT identity_providers_kind_check CHECK (kind IN ('erp_token'));
DELETE FROM schema_migrations WHERE version = 4;
COMMIT;
