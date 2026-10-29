-- ERP in front of the module: the ERP signs a short-lived JWT naming the user on every request.
-- Only the ERP's PUBLIC key is stored (config.public_key); it cannot be used to sign tokens.
--   config: {"issuer": "bigship-erp", "algorithm": "RS256", "public_key": "-----BEGIN PUBLIC KEY-----...",
--            "max_ttl_seconds": 300, "name_claim": "name", "phone_claim": "mobile",
--            "role_claim": "role", "role_map": {"Super Admin": "owner"}}

BEGIN;
SET LOCAL search_path TO app, public;

ALTER TABLE identity_providers DROP CONSTRAINT identity_providers_kind_check;
ALTER TABLE identity_providers
    ADD CONSTRAINT identity_providers_kind_check CHECK (kind IN ('erp_token', 'erp_jwt')),
    ALTER COLUMN verify_url DROP NOT NULL,
    ADD CONSTRAINT identity_providers_kind_fields CHECK (
        (kind = 'erp_token' AND verify_url IS NOT NULL)
        OR (kind = 'erp_jwt' AND config ? 'issuer' AND config ? 'public_key'));

CREATE UNIQUE INDEX identity_providers_issuer_uq ON identity_providers ((config ->> 'issuer'))
    WHERE kind = 'erp_jwt';

CREATE FUNCTION resolve_identity_provider_by_issuer(p_issuer text)
RETURNS TABLE (provider_id uuid, tenant_id uuid, config jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, pg_temp AS $$
    SELECT p.id, p.tenant_id, p.config
      FROM identity_providers p
      JOIN tenants t ON t.id = p.tenant_id AND t.status = 'active'
     WHERE p.kind = 'erp_jwt' AND p.enabled AND p.config ->> 'issuer' = p_issuer
$$;

REVOKE ALL ON FUNCTION resolve_identity_provider_by_issuer(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_identity_provider_by_issuer(text) TO app_user;

INSERT INTO schema_migrations (version, name) VALUES (4, 'erp_signed_tokens');

COMMIT;
