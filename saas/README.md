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

### ERP-signed tokens (ERP in front of the module) — the target setup for Bigship

The ERP checks its own login, then calls this service on the user's behalf with
`Authorization: Bearer <JWT signed by the ERP>` on every request. The service verifies it locally
with the ERP's public key (RS256): issuer, `aud = whatsapp-escalation`, expiry, lifetime <= 300 s.

Claims: `iss`, `aud`, `sub` (login_user_id), `name`, `mobile`, `role` (optional), `iat`, `exp`.

Until the ERP delivers its key, a static stand-in signs test tokens (development only):

```bash
python -m scripts.dev_erp keys        # .dev-erp/ key pair (git-ignored) + setup SQL to run once
python -m scripts.dev_erp token --sub 1 --name "ANKIT JAIN" --mobile 8800674795 --role "Super Admin"
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/v1/me
```

When the ERP delivers: replace `config.public_key` (and `config.issuer` if different) on the
`bigship-erp-jwt` provider row. No code change.

### Simple mode (ERP without a current-user API) — fallback

The ERP call only proves the token belongs to a signed-in ERP user; the user details come from the
ERP's stored login data. Roles are never taken from that data. Known gap: a signed-in ERP user can
claim another user's id; remove `trust_request_identity` once the ERP offers a current-user API.

```sql
INSERT INTO app.identity_providers (tenant_id, slug, name, verify_url, config) VALUES (
  '<tenant id>', 'bigship-erp', 'Bigship ERP', 'https://cmsbstapi.bigship.in/api/escalation-category/list',
  '{"trust_request_identity": true, "user_id_path": "login_user_id", "name_path": "user_name",
    "phone_path": "MobileNo", "active_path": "is_active_user", "active_value": "1"}');
-- after the first sign-in, make the first owner (later done from the module's settings):
UPDATE app.tenant_users SET role = 'owner'
 WHERE user_id = (SELECT id FROM app.users WHERE external_id = '1');
```

Frontend, when the module opens:

```js
const res = await fetch("/v1/auth/exchange/bigship-erp", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ erp_token: localStorage.getItem("token"),
                         user: JSON.parse(localStorage.getItem("userId")) }),
});
const { access_token, refresh_token } = await res.json();
// every module API:
fetch("/v1/groups", { headers: { Authorization: `Bearer ${access_token}` } });
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

## WhatsApp ingest

`workers/ingest_worker.py` keeps each tenant's WhatsApp number connected (one `SessionRunner` per
session, `app/modules/groups/whatsapp/`) and stores group messages. One owner per session is
guaranteed by a PostgreSQL advisory lock. WhatsApp login keys live in neonize's own store:

```text
WA_STORE_DATABASE=postgres://wa_store_user:...@127.0.0.1:5433/whatsapp_sessions   # production
WA_STORE_DATABASE=data/wa-store.sqlite3                                           # local default
```

Keep that store separate from `whatsapp_escalation` and restrict who can read it: it holds the
keys of every connected number.

API (scope `whatsapp:manage`, owners/admins): `POST /v1/groups/sessions` adds a number,
`GET /v1/groups/sessions/{id}` shows status and the QR while pairing, `POST .../reconnect`
restarts it. The worker notices changes within 5 seconds.

## Not ported yet

- WhatsApp: sending reports to WhatsApp, the `!analyse` group command, renaming senders saved
  before their contact was known (`rename_authors`), logging a number out from the module.
- `performance` and `logistics` engines: their jobs fail with a clear "not ported" message.
- Exports (`app/shared/exports`) and a storage adapter for media files.
