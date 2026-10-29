-- Sign-in through a customer's ERP. The module never sees ERP passwords and never stores
-- ERP tokens: it asks the ERP "who owns this token?" and then issues its own module token.

BEGIN;
SET LOCAL search_path TO app, public;

-- One row per customer login system. config holds where to find fields in the ERP's reply:
--   {"method": "GET", "user_id_path": "data.login_user_id", "name_path": "data.user_name",
--    "phone_path": "data.MobileNo", "role_path": "data.role", "active_path": "data.is_active_user",
--    "active_value": "1", "role_map": {"Super Admin": "owner"}}
CREATE TABLE identity_providers (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    slug          citext NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    kind          text NOT NULL DEFAULT 'erp_token' CHECK (kind IN ('erp_token')),
    name          text NOT NULL,
    verify_url    text NOT NULL CHECK (verify_url ~ '^https?://'),
    config        jsonb NOT NULL DEFAULT '{}',
    default_role  text NOT NULL DEFAULT 'viewer' CHECK (default_role IN ('admin', 'analyst', 'viewer')),
    enabled       boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id)
);
CREATE TRIGGER identity_providers_touch BEFORE UPDATE ON identity_providers
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
ALTER TABLE identity_providers ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON identity_providers
    USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

-- ERP users have no email/password here; they are identified by (provider, ERP user id).
ALTER TABLE users
    ALTER COLUMN email DROP NOT NULL,
    ADD COLUMN identity_provider_id uuid REFERENCES identity_providers(id) ON DELETE RESTRICT,
    ADD COLUMN external_id text,
    ADD COLUMN phone_hash text,
    ADD COLUMN phone_enc bytea,
    ADD CONSTRAINT users_external_identity_uq UNIQUE (identity_provider_id, external_id),
    ADD CONSTRAINT users_identity_present CHECK (email IS NOT NULL OR external_id IS NOT NULL),
    ADD CONSTRAINT users_external_pair CHECK ((identity_provider_id IS NULL) = (external_id IS NULL));

CREATE FUNCTION resolve_identity_provider(p_slug text)
RETURNS TABLE (provider_id uuid, tenant_id uuid, verify_url text, config jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, pg_temp AS $$
    SELECT p.id, p.tenant_id, p.verify_url, p.config
      FROM identity_providers p
      JOIN tenants t ON t.id = p.tenant_id AND t.status = 'active'
     WHERE p.slug = p_slug AND p.enabled
$$;

-- Called only after the ERP confirmed the token. Creates the user and membership on first
-- sign-in; p_role (from the provider's role_map) updates the role, NULL keeps the current one.
CREATE FUNCTION upsert_external_user(p_provider uuid, p_external_id text, p_name text,
                                     p_phone_hash text, p_phone_enc bytea, p_role text)
RETURNS TABLE (out_user_id uuid, out_tenant_id uuid, out_role text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = app, pg_temp AS $$
DECLARE
    v_tenant uuid;
    v_default text;
    v_user uuid;
    v_status text;
BEGIN
    IF p_role IS NOT NULL AND p_role NOT IN ('owner', 'admin', 'analyst', 'viewer') THEN
        RAISE EXCEPTION 'invalid role %', p_role USING ERRCODE = '22023';
    END IF;
    SELECT p.tenant_id, p.default_role INTO v_tenant, v_default
      FROM identity_providers p WHERE p.id = p_provider AND p.enabled;
    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'unknown identity provider' USING ERRCODE = '42704';
    END IF;

    INSERT INTO users (identity_provider_id, external_id, full_name, phone_hash, phone_enc, last_login_at)
    VALUES (p_provider, p_external_id, coalesce(p_name, ''), p_phone_hash, p_phone_enc, now())
    ON CONFLICT ON CONSTRAINT users_external_identity_uq DO UPDATE
        SET full_name = coalesce(EXCLUDED.full_name, users.full_name),
            phone_hash = coalesce(EXCLUDED.phone_hash, users.phone_hash),
            phone_enc = coalesce(EXCLUDED.phone_enc, users.phone_enc),
            last_login_at = now()
    RETURNING id, status INTO v_user, v_status;
    IF v_status <> 'active' THEN
        RAISE EXCEPTION 'user disabled' USING ERRCODE = '42501';
    END IF;

    INSERT INTO tenant_users AS tu (tenant_id, user_id, role)
    VALUES (v_tenant, v_user, coalesce(p_role, v_default))
    ON CONFLICT ON CONSTRAINT tenant_users_pkey DO UPDATE SET role = coalesce(p_role, tu.role)
    RETURNING tu.role INTO out_role;

    out_user_id := v_user;
    out_tenant_id := v_tenant;
    RETURN NEXT;
END $$;

REVOKE ALL ON FUNCTION resolve_identity_provider(text),
    upsert_external_user(uuid, text, text, text, bytea, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_identity_provider(text),
    upsert_external_user(uuid, text, text, text, bytea, text) TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON identity_providers TO app_user;

INSERT INTO schema_migrations (version, name) VALUES (3, 'erp_identity');

COMMIT;
