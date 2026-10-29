-- SaaS core schema v1: multi-tenant, PostgreSQL 15+ (uses ON DELETE SET NULL (column)).
--
-- Tenant isolation:
--   * Every tenant-owned table carries tenant_id and has row-level security enabled.
--   * Run migrations as the owning role (e.g. app_owner). The service connects as app_user,
--     which is NOT the owner, so RLS always applies to it.
--   * Per request/transaction the service must call:
--       SELECT set_config('app.tenant_id', '<tenant uuid>', true);
--     If it is not set, app.current_tenant_id() is NULL and every policy returns zero rows (fail closed).
--   * Child rows reference parents by (tenant_id, id), so a row can never point at another tenant's data.
--
-- Lookups that happen before the tenant is known (API key, refresh token, login memberships)
-- go through SECURITY DEFINER functions at the end of this file.

BEGIN;

CREATE EXTENSION IF NOT EXISTS citext;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user NOLOGIN NOBYPASSRLS;
    END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS app;
SET LOCAL search_path TO app, public;

CREATE TABLE schema_migrations (
    version     int PRIMARY KEY,
    name        text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- helpers

CREATE FUNCTION current_tenant_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- ---------------------------------------------------------------- tenants and identity

CREATE TABLE tenants (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        citext NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    plan        text NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'starter', 'pro', 'enterprise')),
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deleted')),
    timezone    text NOT NULL DEFAULT 'Asia/Kolkata',
    settings    jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Global: one person can belong to several tenants. Not tenant-scoped, so no RLS.
CREATE TABLE users (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email             citext NOT NULL UNIQUE,
    password_hash     text,
    full_name         text NOT NULL DEFAULT '',
    is_platform_admin boolean NOT NULL DEFAULT false,
    status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    last_login_at     timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tenant_users (
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        text NOT NULL DEFAULT 'viewer' CHECK (role IN ('owner', 'admin', 'analyst', 'viewer')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX tenant_users_user_idx ON tenant_users (user_id);

-- Refresh tokens; access JWTs are stateless and short-lived.
CREATE TABLE auth_sessions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  text NOT NULL UNIQUE,
    user_agent  text,
    ip          inet,
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX auth_sessions_user_idx ON auth_sessions (tenant_id, user_id) WHERE revoked_at IS NULL;

-- Key format: <prefix>.<secret>. Only the prefix is stored in clear; the secret is hashed.
CREATE TABLE api_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name          text NOT NULL,
    prefix        text NOT NULL UNIQUE CHECK (length(prefix) BETWEEN 8 AND 32),
    key_hash      text NOT NULL,
    scopes        text[] NOT NULL DEFAULT '{}',
    created_by    uuid REFERENCES users(id) ON DELETE SET NULL,
    last_used_at  timestamptz,
    expires_at    timestamptz,
    revoked_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX api_keys_tenant_idx ON api_keys (tenant_id);

-- ---------------------------------------------------------------- modules

-- Global catalogue of installable modules. A module folder registers itself here.
CREATE TABLE modules (
    key          text PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_]{1,40}$'),
    name         text NOT NULL,
    description  text NOT NULL DEFAULT '',
    is_core      boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tenant_modules (
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    module_key  text NOT NULL REFERENCES modules(key) ON DELETE CASCADE,
    enabled     boolean NOT NULL DEFAULT true,
    config      jsonb NOT NULL DEFAULT '{}',
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, module_key)
);

-- ---------------------------------------------------------------- whatsapp

CREATE TABLE wa_sessions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    phone_hash       text NOT NULL,
    phone_enc        bytea,
    session_enc      bytea,
    key_version      int NOT NULL DEFAULT 1,
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'pairing', 'connected', 'disconnected', 'logged_out')),
    worker_id        text,
    last_seen_at     timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, phone_hash)
);

CREATE TABLE clusters (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id)
);
CREATE UNIQUE INDEX clusters_tenant_name_uq ON clusters (tenant_id, lower(name));

CREATE TABLE wa_groups (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL,
    session_id       uuid NOT NULL,
    cluster_id       uuid,
    jid              text NOT NULL,
    name             text NOT NULL DEFAULT '',
    name_override    text,
    is_active        boolean NOT NULL DEFAULT true,
    last_message_at  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, jid),
    FOREIGN KEY (tenant_id, session_id) REFERENCES wa_sessions (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, cluster_id) REFERENCES clusters (tenant_id, id) ON DELETE SET NULL (cluster_id)
);
CREATE INDEX wa_groups_cluster_idx ON wa_groups (tenant_id, cluster_id) WHERE cluster_id IS NOT NULL;

-- Largest table: range-partitioned by month on sent_at.
-- Partitioned unique keys must contain sent_at; a WhatsApp message id keeps one sent_at, so dedupe still holds.
CREATE TABLE messages (
    id           uuid NOT NULL DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL,
    group_id     uuid NOT NULL,
    wa_msg_id    text NOT NULL,
    sender_hash  text NOT NULL,
    sender_name  text NOT NULL DEFAULT '',
    kind         text NOT NULL DEFAULT 'text'
                 CHECK (kind IN ('text', 'image', 'video', 'audio', 'document', 'sticker', 'location', 'other')),
    body         text NOT NULL DEFAULT '',
    media_text   text,
    reply_to     text,
    sent_at      timestamptz NOT NULL,
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, sent_at),
    UNIQUE (tenant_id, group_id, wa_msg_id, sent_at),
    FOREIGN KEY (tenant_id, group_id) REFERENCES wa_groups (tenant_id, id) ON DELETE CASCADE
) PARTITION BY RANGE (sent_at);

CREATE INDEX messages_group_time_idx ON messages (tenant_id, group_id, sent_at DESC);
CREATE TABLE messages_default PARTITION OF messages DEFAULT;

CREATE TABLE media_items (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    message_id       uuid NOT NULL,
    message_sent_at  timestamptz NOT NULL,
    mime_type        text NOT NULL,
    size_bytes       bigint CHECK (size_bytes >= 0),
    storage_key      text,
    extract_status   text NOT NULL DEFAULT 'pending'
                     CHECK (extract_status IN ('pending', 'done', 'failed', 'skipped')),
    extracted_text   text,
    model            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (message_id, message_sent_at) REFERENCES messages (id, sent_at) ON DELETE CASCADE
);
CREATE INDEX media_items_message_idx ON media_items (message_id, message_sent_at);
CREATE INDEX media_items_pending_idx ON media_items (created_at) WHERE extract_status = 'pending';

-- ---------------------------------------------------------------- jobs and reports

CREATE TABLE report_presets (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    module_key     text NOT NULL REFERENCES modules(key),
    owner_user_id  uuid REFERENCES users(id) ON DELETE SET NULL,
    name           text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    options        jsonb NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id)
);

CREATE TABLE schedules (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL,
    preset_id    uuid NOT NULL,
    cron         text NOT NULL,
    timezone     text NOT NULL DEFAULT 'Asia/Kolkata',
    enabled      boolean NOT NULL DEFAULT true,
    next_run_at  timestamptz,
    last_run_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, preset_id) REFERENCES report_presets (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX schedules_due_idx ON schedules (next_run_at) WHERE enabled;

CREATE TABLE jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    module_key       text NOT NULL REFERENCES modules(key),
    kind             text NOT NULL,
    idempotency_key  text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'complete', 'empty', 'failed', 'cancelled')),
    requested_by     uuid REFERENCES users(id) ON DELETE SET NULL,
    schedule_id      uuid REFERENCES schedules(id) ON DELETE SET NULL,
    options          jsonb NOT NULL DEFAULT '{}',
    progress         smallint NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    attempts         smallint NOT NULL DEFAULT 0,
    error            text,
    locked_by        text,
    locked_until     timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX jobs_queue_idx ON jobs (created_at) WHERE status = 'queued';
CREATE INDEX jobs_tenant_recent_idx ON jobs (tenant_id, module_key, created_at DESC);

CREATE TABLE reports (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL,
    job_id             uuid NOT NULL UNIQUE,
    module_key         text NOT NULL REFERENCES modules(key),
    title              text NOT NULL DEFAULT '',
    window_start       timestamptz,
    window_end         timestamptz,
    content            jsonb NOT NULL,
    summary_md         text NOT NULL DEFAULT '',
    content_hash       text NOT NULL,
    prompt_version     text NOT NULL,
    model              text,
    validation_status  text NOT NULL DEFAULT 'passed'
                       CHECK (validation_status IN ('passed', 'needs_review', 'failed')),
    validation_notes   jsonb NOT NULL DEFAULT '[]',
    visibility         text NOT NULL DEFAULT 'tenant' CHECK (visibility IN ('private', 'tenant')),
    created_by         uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    CHECK (window_end IS NULL OR window_start IS NULL OR window_end > window_start),
    FOREIGN KEY (tenant_id, job_id) REFERENCES jobs (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX reports_tenant_recent_idx ON reports (tenant_id, module_key, created_at DESC);

CREATE TABLE report_groups (
    tenant_id  uuid NOT NULL,
    report_id  uuid NOT NULL,
    group_id   uuid NOT NULL,
    PRIMARY KEY (report_id, group_id),
    FOREIGN KEY (tenant_id, report_id) REFERENCES reports (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, group_id) REFERENCES wa_groups (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX report_groups_group_idx ON report_groups (tenant_id, group_id);

CREATE TABLE report_exports (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL,
    report_id    uuid NOT NULL,
    format       text NOT NULL CHECK (format IN ('pdf', 'xlsx', 'docx', 'csv', 'md')),
    storage_key  text NOT NULL,
    size_bytes   bigint CHECK (size_bytes >= 0),
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (report_id, format),
    FOREIGN KEY (tenant_id, report_id) REFERENCES reports (tenant_id, id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------- AI gateway

-- Same content + same prompt version => reuse result, no DeepSeek call.
CREATE TABLE analysis_cache (
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    content_hash    text NOT NULL,
    prompt_version  text NOT NULL,
    result          jsonb NOT NULL,
    hit_count       int NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, content_hash, prompt_version)
);
CREATE INDEX analysis_cache_expiry_idx ON analysis_cache (expires_at);

-- Reversible masking: the AI sees tokens like <PHONE_7>; the real value never leaves the database.
CREATE TABLE pii_vault (
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    token       text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('phone', 'name', 'email', 'address', 'other')),
    value_hash  text NOT NULL,
    value_enc   bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, token),
    UNIQUE (tenant_id, kind, value_hash)
);

-- One row per outbound AI request: what was sent (by hash and field list), cost and outcome.
CREATE TABLE ai_calls (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL,
    job_id           uuid,
    provider         text NOT NULL CHECK (provider IN ('deepseek', 'gemini')),
    model            text NOT NULL,
    purpose          text NOT NULL,
    prompt_version   text NOT NULL,
    payload_hash     text NOT NULL,
    fields_sent      text[] NOT NULL DEFAULT '{}',
    redaction_count  int NOT NULL DEFAULT 0,
    tokens_in        int NOT NULL DEFAULT 0,
    tokens_out       int NOT NULL DEFAULT 0,
    latency_ms       int,
    cache_hit        boolean NOT NULL DEFAULT false,
    status           text NOT NULL CHECK (status IN ('ok', 'error', 'timeout', 'rejected')),
    error            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, job_id) REFERENCES jobs (tenant_id, id) ON DELETE SET NULL (job_id)
);
CREATE INDEX ai_calls_tenant_time_idx ON ai_calls (tenant_id, created_at DESC);
CREATE INDEX ai_calls_job_idx ON ai_calls (job_id) WHERE job_id IS NOT NULL;

-- ---------------------------------------------------------------- audit

CREATE TABLE audit_log (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    actor_user_id     uuid REFERENCES users(id) ON DELETE SET NULL,
    actor_api_key_id  uuid REFERENCES api_keys(id) ON DELETE SET NULL,
    action            text NOT NULL,
    entity            text NOT NULL,
    entity_id         text,
    ip                inet,
    meta              jsonb NOT NULL DEFAULT '{}',
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_tenant_time_idx ON audit_log (tenant_id, created_at DESC);

-- ---------------------------------------------------------------- updated_at triggers

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['tenants', 'users', 'tenant_modules', 'wa_sessions', 'clusters',
                             'wa_groups', 'report_presets', 'schedules']
    LOOP
        EXECUTE format('CREATE TRIGGER %I_touch BEFORE UPDATE ON app.%I
                        FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at()', t, t);
    END LOOP;
END $$;

-- ---------------------------------------------------------------- row-level security

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenants
    USING (id = current_tenant_id()) WITH CHECK (id = current_tenant_id());

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['tenant_users', 'auth_sessions', 'api_keys', 'tenant_modules', 'wa_sessions',
                             'clusters', 'wa_groups', 'messages', 'media_items', 'report_presets',
                             'schedules', 'jobs', 'reports', 'report_groups', 'report_exports',
                             'analysis_cache', 'pii_vault', 'ai_calls', 'audit_log']
    LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON app.%I
                        USING (tenant_id = app.current_tenant_id())
                        WITH CHECK (tenant_id = app.current_tenant_id())', t);
    END LOOP;
END $$;

-- ---------------------------------------------------------------- pre-tenant lookups

CREATE FUNCTION resolve_api_key(p_prefix text)
RETURNS TABLE (api_key_id uuid, tenant_id uuid, key_hash text, scopes text[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, pg_temp AS $$
    SELECT k.id, k.tenant_id, k.key_hash, k.scopes
      FROM api_keys k
      JOIN tenants t ON t.id = k.tenant_id AND t.status = 'active'
     WHERE k.prefix = p_prefix
       AND k.revoked_at IS NULL
       AND (k.expires_at IS NULL OR k.expires_at > now())
$$;

CREATE FUNCTION resolve_auth_session(p_token_hash text)
RETURNS TABLE (session_id uuid, tenant_id uuid, user_id uuid, expires_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, pg_temp AS $$
    SELECT s.id, s.tenant_id, s.user_id, s.expires_at
      FROM auth_sessions s
      JOIN tenants t ON t.id = s.tenant_id AND t.status = 'active'
     WHERE s.token_hash = p_token_hash
       AND s.revoked_at IS NULL
       AND s.expires_at > now()
$$;

CREATE FUNCTION user_memberships(p_user_id uuid)
RETURNS TABLE (tenant_id uuid, tenant_name text, role text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, pg_temp AS $$
    SELECT t.id, t.name, tu.role
      FROM tenant_users tu
      JOIN tenants t ON t.id = tu.tenant_id AND t.status = 'active'
     WHERE tu.user_id = p_user_id
$$;

-- ---------------------------------------------------------------- partition maintenance

-- Creates monthly message partitions; run on deploy and from a monthly job (as the owner role).
CREATE FUNCTION ensure_message_partitions(p_from date, p_months int)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    m date := date_trunc('month', p_from)::date;
    name text;
BEGIN
    FOR i IN 0 .. p_months - 1 LOOP
        name := format('messages_%s', to_char(m, 'YYYY_MM'));
        IF to_regclass('app.' || name) IS NULL THEN
            EXECUTE format('CREATE TABLE app.%I PARTITION OF app.messages FOR VALUES FROM (%L) TO (%L)',
                           name, m, (m + interval '1 month')::date);
        END IF;
        m := (m + interval '1 month')::date;
    END LOOP;
END $$;

SELECT ensure_message_partitions((now() - interval '1 month')::date, 14);

-- ---------------------------------------------------------------- grants

REVOKE ALL ON SCHEMA app FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO app_user;
REVOKE INSERT, UPDATE, DELETE ON schema_migrations, modules FROM app_user;
REVOKE UPDATE, DELETE ON audit_log, ai_calls FROM app_user;
-- Partitions do not inherit the parent's RLS policy: app_user must only reach them through app.messages.
-- Partitions created later by ensure_message_partitions() get no grants, so they stay closed too.
DO $$
DECLARE part regclass;
BEGIN
    FOR part IN SELECT inhrelid::regclass FROM pg_inherits WHERE inhparent = 'app.messages'::regclass LOOP
        EXECUTE format('REVOKE ALL ON %s FROM app_user', part);
    END LOOP;
END $$;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA app TO app_user;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app FROM PUBLIC;
GRANT EXECUTE ON FUNCTION current_tenant_id(), touch_updated_at(), resolve_api_key(text),
    resolve_auth_session(text), user_memberships(uuid) TO app_user;

-- ---------------------------------------------------------------- seed module catalogue

INSERT INTO modules (key, name, description, is_core) VALUES
    ('groups',        'Groups',         'WhatsApp groups, messages and clusters', true),
    ('master_report', 'Master reports', 'Scheduled and bulk escalation reports', false),
    ('quick_report',  'Quick reports',  'Prompt-driven reports over groups and files', false),
    ('performance',   'Performance',    'Team performance workbooks', false),
    ('logistics',     'Logistics',      'AWB and TAT logistics reports', false);

INSERT INTO schema_migrations (version, name) VALUES (1, 'core_schema');

COMMIT;
