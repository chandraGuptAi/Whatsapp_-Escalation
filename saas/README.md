# WhatsApp Escalation (SaaS)

Multi-tenant rebuild of the escalation service. Database: `whatsapp_escalation`, schema `app`.

## Layout

```text
app/
  main.py              API process (stateless, scale horizontally)
  core/                platform; modules depend on it, never the reverse
    auth/              JWT + refresh tokens, API keys, scopes, tenant-scoped connection
    db/                pool + tenant_transaction (sets app.tenant_id -> RLS)
    ai/                the ONLY path to DeepSeek/Gemini: mask -> budget -> cache -> call -> audit
    queue/             Postgres job queue (SKIP LOCKED), idempotent enqueue
    reports/           shared report pipeline, runs, presets/schedules, report read API
    jobs/              job status, cancel, live progress (SSE)
    modules/           Module contract + registry (discover / mount / job handlers)
    cache/ security/ events/ observability/ tenancy/
  modules/<key>/       one folder per feature: __init__ (MODULE), api, schemas, service, jobs, tests
  shared/              pure helpers (IST, hashing, exports)
workers/               report_worker, scheduler, ingest_worker (each scales on its own)
migrations/core/       platform schema (001, 002); migrations/modules/<key>/ for module tables
deploy/                Dockerfile + compose.yaml
tests/                 contract tests + tenant isolation tests (real PostgreSQL)
```

## Rules

- Routes get a database connection only through `tenant_conn`; there is no other way to query tenant data.
- A module never imports another module. Shared needs go into `app/core`.
- Only `app/core/ai` calls an AI provider. Bump a module's `PROMPT_VERSION` whenever its prompt changes.
- Add a module: create `app/modules/<key>/` with `MODULE = Module(key="<key>", ...)` and insert a row into
  `app.modules`. Remove one: delete the folder, or list it in `DISABLED_MODULES`.

## Sign-in from a customer ERP (module-only tokens)

The ERP keeps its own login. When the user opens WhatsApp Escalation, the frontend sends the ERP
token once; the module confirms it by calling the ERP's own "current user" API and returns a
module token that works only here (`aud = whatsapp-escalation`). Nothing from the browser's
localStorage (role, mobile, user id) is trusted, and the ERP token is never stored.

```text
POST /v1/auth/exchange/<provider-slug>   {"erp_token": "<ERP bearer token>"}  -> access + refresh token
POST /v1/auth/refresh                    {"refresh_token": "..."}             -> new pair (old one revoked)
POST /v1/auth/logout                     {"refresh_token": "..."}
```

Register each customer's ERP once (as the admin role), pointing `verify_url` at an ERP API that
returns the signed-in user for a Bearer token, and `config` at the fields in its reply:

```sql
INSERT INTO app.tenants (slug, name) VALUES ('bigship', 'Bigship') RETURNING id;
INSERT INTO app.identity_providers (tenant_id, slug, name, verify_url, config) VALUES (
  '<tenant id>', 'bigship-erp', 'Bigship ERP', 'https://<erp host>/<current-user API>',
  '{"user_id_path": "data.login_user_id", "name_path": "data.user_name", "phone_path": "data.MobileNo",
    "role_path": "data.role", "active_path": "data.is_active_user", "active_value": "1",
    "role_map": {"Super Admin": "owner"}}');
INSERT INTO app.tenant_modules (tenant_id, module_key)
  SELECT '<tenant id>', key FROM app.modules WHERE NOT is_core;
```

## Database setup (once, as an admin role)

```bash
createdb -U postgres whatsapp_escalation
psql -U postgres -d whatsapp_escalation -v ON_ERROR_STOP=1 -f migrations/core/001_core_schema.up.sql
psql -U postgres -d whatsapp_escalation -v ON_ERROR_STOP=1 -f migrations/core/002_worker_functions.up.sql
psql -U postgres -d whatsapp_escalation -v ON_ERROR_STOP=1 -f migrations/core/003_erp_identity.up.sql
psql -U postgres -d whatsapp_escalation -c "ALTER ROLE app_user LOGIN PASSWORD '<strong password>'"
```

Monthly (or on deploy): `SELECT app.ensure_message_partitions(now()::date, 3);` as the admin role.

## Run

```bash
pip install -e ".[dev]"   # or install the dependencies listed in pyproject.toml
cp .env.example .env      # fill DATABASE_URL (app_user), JWT_SECRET, API_KEY_PEPPER, DATA_ENCRYPTION_KEY
python -m app.serve 8000          # local, works on Windows; Docker uses the uvicorn CLI
python -m workers.report_worker
python -m workers.scheduler
```

Windows note: psycopg's async driver needs the selector event loop. `app.serve`, the workers and
the tests set it via `use_selector_loop_on_windows()`. Plain `uvicorn ...` in one process on
Windows uses the Proactor loop and fails to connect; Linux containers are unaffected.

## Tests

```bash
python -m pytest -q
# tenant isolation against a disposable migrated database:
SAAS_TEST_OWNER_URL=postgresql://postgres:...@127.0.0.1/whatsapp_escalation_test \
SAAS_TEST_APP_URL=postgresql://app_user:...@127.0.0.1/whatsapp_escalation_test python -m pytest -q tests/isolation
```

## Not ported yet

- `workers/ingest_worker.run_session`: the neonize WhatsApp client (from `app/escalation_analyst/whatsapp/`).
- `performance` and `logistics` engines: their jobs fail with a clear "not ported" message.
- Exports (`app/shared/exports`) and a storage adapter for media files.
