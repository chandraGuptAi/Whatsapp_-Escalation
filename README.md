# Standalone Python WhatsApp escalation backend

Extracted from `E:\Office\Ai_backend\AI_python_backend\app\escalation_analyst`, including the current local changes. Reports, groups, presets, analysis, cancellation, scheduler, ingestion, WhatsApp worker and exports are preserved. The only business-package change is replacing the host auth import with a standalone identity dependency. No existing Node project files are changed.

Request flow: ERP → main backend **8200** → `/api/escalation/...` gateway → private WhatsApp service **8201**. Existing frontend URLs, user headers, query strings, uploads and download responses remain the same. Other main backend APIs use their existing implementation.

## Local startup

From this directory:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set a random `ESCALATION_SERVICE_TOKEN` and a valid `ESCALATION_DATABASE_URL` (PostgreSQL) in `.env`, plus the existing escalation AI/media settings. Startup requires PostgreSQL configuration; it no longer silently falls back to SQLite. Do not commit credentials. Start with auto-connect and scheduler disabled for verification:

```powershell
.venv\Scripts\python -m uvicorn app.main:build_app --factory --host 127.0.0.1 --port 8201 --workers 1 --no-proxy-headers
```

Only one service worker owns the WhatsApp session/scheduler. The native WhatsApp child process still runs independently inside the service. `GET /health` checks service liveness; it does not claim WhatsApp/provider connectivity. Other routes, including `/api/escalation/docs`, require `X-Escalation-Service-Token`. User routes also require the existing `X-User-Id` header. Optional import routes additionally require `X-Ingest-Secret`.

## Main backend integration

```powershell
python integration/install_gateway.py --backend E:\Office\Ai_backend\AI_python_backend
```

This adds `app/api_gateway.py` and changes only the escalation installer import in `app/main.py`, backing up the original main file under `scratch/gateway-backup-*`. It preserves all other current edits. No dependencies are added to the host: `httpx` is already present.

To activate remote mode, add the settings from `integration/main-backend.env.example` to the main backend environment and restart it. Both services must have the same service token. Leave these settings unset until data migration is complete. With no service URL the main backend continues using the embedded module. With a URL set it never imports/starts the local escalation runtime, database, scheduler or native WhatsApp client. An unavailable private service returns 502/504 for escalation requests; other routes remain available. Requests are not automatically retried, preventing duplicate jobs/uploads.

The existing ERP authentication boundary still owns validation of user identity; the gateway forwards its `X-User-Id` / `X-User-Name`. The private service requires the server secret before trusting those headers. Do not expose port 8201 publicly.

## Data cutover without changing existing data

1. Prepare and test the new service using an empty test data directory, auto-connect/scheduler disabled. Check `/health` and a secret + user authenticated `/api/escalation/groups` request.
2. Stop the old main process for the cutover. Do not allow two native clients or schedulers to own the same session/database.
3. Back up the existing escalation DB and native session. For SQLite, checkpoint both DBs after shutdown (`PRAGMA wal_checkpoint(TRUNCATE)`) before copying the `.sqlite3` files; alternatively use SQLite's backup API for a consistent backup. Do not copy only a live SQLite main file while its WAL contains data. Preserve the source files and restrict backup permissions.
4. Copy `data/escalation.sqlite3` and `data/whatsapp-session.sqlite3` to this service's persistent data directory, or set their two path settings to dedicated migrated locations. Preserve any configured paths, including absolute paths used by old settings. For PostgreSQL, point `ESCALATION_DATABASE_URL` at the existing escalation schema; do not import it again or use the host's unrelated report storage. The original Node Chromium session is not interchangeable with the Python native session.
5. Transfer the escalation settings/AI keys and optional scheduler configuration into the new service environment. Enable `WHATSAPP_AUTOSTART=true` only after the old owner is stopped. Start service 8201, confirm status and saved groups/reports; pair again only if the native session cannot reconnect.
6. Set `ESCALATION_SERVICE_URL=http://127.0.0.1:8201` and matching token on main, restart main on 8200, then verify groups, report generation/polling/cancellation, exports and reconnect through its existing `/api/escalation` URLs.

Rollback: stop the private service first, remove `ESCALATION_SERVICE_URL` from main and restart it. If the new service has accepted data, migrate/checkpoint that latest database/session back before rollback; an old snapshot would lose post-cutover data. Original embedded code is retained as a rollback option, but does not execute in remote mode.

## Docker on the same server

```sh
docker compose up -d --build
```

The compose file binds `127.0.0.1:8201`, with persistent named volume `whatsapp-data`. Migrate data into that volume before production startup; copying files into the project directory does not populate a named volume. The image runs as UID 10001, so migrated files/volume must be writable by that UID.

If main also runs in Docker, `127.0.0.1` refers to its own container: join both services to the same private Docker network and set main's URL to `http://whatsapp:8201` using the service DNS name. No public 8201 mapping is needed in that arrangement. The gateway's default read timeout is 1800 seconds for existing long report calls; set any outer reverse-proxy timeout consistently.

## Verification

```powershell
.venv\Scripts\python -m pip install pytest
.venv\Scripts\python -m pytest -q
```

Tests use isolated temporary databases, mocked AI/transport and disabled WhatsApp auto-connect. No real account is paired by tests. Docker deployment and live WhatsApp pairing require a server/account smoke test during cutover.

## Quick reports

Free-form prompt reports over selected groups, attached files and previous reports (`app/escalation_analyst/api/quick.py`, prompt `prompts/quick-report.md`). Runs use the shared job runner (`kind="quick"`) and AI provider, and are visible only to the user who created them.

- `POST /quick-reports` — `{prompt, groupIds[], hours | fromDate+toDate, contextReportIds[], outputFormat: web|web_excel|word|detailed, voice, files[{name, data(base64)}], requestKey}`. Needs at least one group or file. Returns `202 {id}`.
- `GET /quick-reports?limit&offset` — the caller's runs: status `running|complete|empty|error|cancelled`, inputs, group names, file names.
- `GET /quick-reports/{id}` — one run including the Markdown `report`.
- `POST /quick-reports/{id}/retry` — re-runs with the stored inputs (file text is stored, so files need not be re-uploaded).
- `GET /quick-reports/{id}/download?format=doc|md|csv|xlsx` — Excel/CSV use the report's first Markdown table.

Files: TXT/CSV/MD/JSON/TSV/LOG, Excel `.xlsx` (openpyxl) and Word `.docx` (stdlib), max 10 MB and 5 files; text is extracted at submit time and capped at 60,000 characters per file. PDF and images are rejected: they need a PDF text library (for example `pypdf`) and a vision-capable model, which are not installed. Voice is speech-to-text in the browser; no audio reaches the server.

## SQLite to PostgreSQL

Stop the service, back up the SQLite database, set `ESCALATION_DATABASE_URL` in `.env`, then run:

```powershell
python scripts/migrate_sqlite_to_postgres.py --sqlite data/escalation.sqlite3
```

The importer copies missing rows in one transaction, preserves IDs, resets sequences and verifies every source primary key. Existing conflicting rows are retained. Unknown tables or columns abort the copy. Restart the service after migration. Keep the original SQLite file as a backup. WhatsApp linked-device credentials still use the separate `WHATSAPP_SESSION_PATH` SQLite file.
