# Escalation Analyst backend

This is the extracted standalone copy. See `../../README.md` for service startup,
private gateway authentication, deployment and cutover instructions. The host
integration notes below describe its original embedded implementation.

This package adds the WhatsApp workspace to AI Box at **`/api/escalation`**. Existing `/api/reports`, insights, chart and canonical-report APIs keep their own code and database. The only host integration is `install_escalation_analyst(app)` in `app/main.py`; its lifespan wrapper preserves the existing host lifespan.

## Where to change code

- `api/`: request validation and routes, grouped by page/API area.
- `repositories/`: database queries, schema, durable jobs and imports.
- `analysis/`: streaming DeepSeek calls, report reuse, group/master analysis and metric parsing.
- `logistics/`: AWB detection, classification, evidence/TAT and Excel export.
- `performance/`: mappings, one frozen dataset, ten sheets, conversation details and template styles.
- `whatsapp/`: native Python WhatsApp client, protobuf message/history capture and Gemini media extraction.
- `exports/`, `utils/`, `prompts/`: document downloads, IST/JS compatibility and editable prompt files.

Dashboard routes reuse the host's `X-User-Id` identity dependency. As with AI Box, a trusted ERP gateway must authenticate that identity before forwarding it. The workspace is shared across its operators; presets are scoped by user. The optional import API requires `X-Ingest-Secret`; native WhatsApp writes directly through repositories. No old dashboard password/session is imported.

## Configure and run

Copy the variables from this folder's `.env.example` into the **host project's existing `.env`**. Do not replace the host file. Install the host requirements, including pinned Neonize 0.5.2 and filelock. Neonize bundles a native Go/whatsmeow library; no Node service or browser is used.

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8002
```

The standalone service requires `ESCALATION_DATABASE_URL` and stores application data in the private PostgreSQL schema `escalation_analyst`. SQLite support remains for isolated tests and migration sources. Existing `saved_reports` tables are never migrated or altered. Schema creation happens on first access, not import. Missing AI/WhatsApp credentials affect this module's operations, not AI Box startup.

POST analysis creates a durable job and returns immediately. Workers persist results/errors; the UI polls status. Group and master jobs share database claims, and heartbeats protect active jobs. Expired claims become errors on the next submission. Snapshot generation, filters, details and Excel exports use the same deterministic dataset. Schedules and daily message retention are **off until `ESCALATION_SCHEDULER_ENABLED=true`**; reports are never purged. Set retention days to `0` to keep messages.

## Master runs

A master run analyses eligible (active) groups with `MASTER_ANALYSIS_CONCURRENCY` workers (default 4). Each worker uses the same durable per-group claim as dashboard analysis, runs it in its own thread with its own database connections, and saves its group report before the next group starts. Provider calls from every caller share `MASTER_AI_MAX_CONCURRENCY` slots; transient errors retry `MASTER_AI_MAX_RETRIES` times, honouring `Retry-After`. No transaction is held during an AI call.

Per group: no messages in the window → skipped without AI; a saved report from the same analysis prompt that covers every message → reused; new messages → incremental update of that report (`MASTER_ANALYSIS_INCREMENTAL_ENABLED=false` makes it a full analysis); otherwise, or with `force`, a full analysis. Reports record a `prompt_version`; reports from an edited prompt are not reused. Older unversioned reports remain reusable. One failed group is recorded and the others continue. Failed groups are listed in the report and excluded from totals; if every group fails, the run fails.

All totals are computed in Python from the groups' machine-count blocks. The code also ranks the worst groups, finds recurring categories and AWBs raised in several groups, and checks each group's counts against its table. Selected previous master reports (`contextReportIds`, validated to be earlier masters of the same cluster) are given to the AI as clearly separated history. The code computes carry-over by AWB: still open, verified closed since, newly opened. Previous totals are never added to the current window. Full group reports are sent when they fit `MASTER_PROMPT_MAX_CHARS`; otherwise AWB-journey sections are dropped, and if needed batches are digested in parallel. The narrative is validated for unknown group tokens and for AI-written totals, which are removed in strict mode.

`GET /master/progress` adds the fields described in the admin panel's `PROGRESS-API.md` to the existing `run` object: `total_eligible_groups`, `counts.{analysed,reused,skipped,failed}` (summing to `completed_groups`), `active_groups[]`, `events[]`, `progress_samples[]`, `phases[]`, `completed_phases[]`, `started_at`/`finished_at`/`last_progress_at`/`updated_at` (epoch seconds), `remaining_phase_estimate_seconds` and `estimated_seconds_remaining`. These estimates use earlier runs' measured merge time, or this run's mean AI call time; they are null until measured. Legacy fields and the `done` phase are unchanged. Stopping preserves completed group reports and counts. A worker that stops heartbeating for `ESCALATION_JOB_STALE_SECONDS` is released, so a restart can start a new run, and that run reuses every saved group report. Each run logs one `Escalation master run … metrics` line with durations, counts, retries and token totals, but no message content. `scripts/benchmark_escalation_master.py` benchmarks orchestration with simulated AI latency.

## Python WhatsApp transport

`whatsapp/client.py` controls a dedicated Python worker through private multiprocessing queues. `whatsapp/worker.py` hosts that worker; `whatsapp/native.py` handles the SDK, QR and connection events. `whatsapp/messages.py` maps real-time and phone-provided history events into the existing database and media pipeline. A QR timeout can restart only the WhatsApp worker, even when the native library is blocked; the HTTP/report backend stays available. No local transport HTTP server or Node process is used. Reports and messages keep their existing storage; linked-device credentials live in a separate `data/whatsapp-session.sqlite3` file. Keep that file private and persistent. File locking prevents two local backend workers from sharing the same session.

1. Stop the old Node transport once. It is no longer needed or contacted.
2. Set `WHATSAPP_AUTOSTART=true` in this Python project's `.env` and start one backend worker. A relative `WHATSAPP_SESSION_PATH` resolves against the working directory.
3. Open the ERP Groups page and click **Connect WhatsApp**. Scan the real QR using the phone's Linked Devices screen. The old Chromium login cannot be imported; pairing is required once for the new Python session.
4. Saved sessions reconnect on backend restart. Group names come from joined-group metadata; live messages and offline/history events are stored idempotently. History availability is controlled by WhatsApp/phone delivery; it is not an unlimited historical export. Each received conversation history chunk is bounded by `WHATSAPP_HISTORY_LIMIT` (default 50).

Neonize provides Windows and Linux native wheels. FFmpeg/FFprobe are optional for text and QR; install them when using media conversion features that require them. Received media is downloaded via the native client and passed to the existing Python Gemini extractor. PNG/QR data and private-message bodies are never printed by application callbacks. A successful QR scan and actual live capture must be verified with the support phone; offline tests do not simulate successful login.

## Import and verification

```powershell
.venv\Scripts\python.exe -m app.escalation_analyst.migrate --source-sqlite E:\Office\whatsapp-ai\data\messages.db
# Add --apply only after reviewing the dry run. For presets add --owner-id <ERP user ID>.
# PostgreSQL source: --source-postgres <connection-url> --source-schema public
.venv\Scripts\python.exe -m pytest tests/escalation_analyst -q
```

Import is repeatable and keeps a raw source archive. Historical deterministic runs receive numeric Python IDs and retain their original UUID as `request.legacyId`. Source sessions and running locks are not copied. Imported running snapshots become errors. Old master-run progress stays in the archive; saved AI reports remain available. Node API keys are archived/imported as hashes, but the optional legacy `/api/v1` key-auth facade is not mounted; use the ERP identity API above.

## Validation and limits

Golden tests cover original Node ten-sheet columns/values, KPI/status/TAT distributions, reference Excel positions/formulas/cached values, deduplication, locks, disconnected access, report reuse, exports and migration. Compare another captured workbook with `scripts/compare_escalation_workbooks.py old.xlsx new.xlsx` before a production cutover. Real provider calls, PostgreSQL import and live pairing require your environment and have not been exercised by offline tests.

PDF uses the host's ReportLab instead of adding Chromium; its layout differs from the old HTML PDF. Markdown/Word exports preserve escaped content; their layout is simpler than the original dashboard renderer. Media payloads are limited to 6MB so base64 fits the existing host 10MB body limit; CSV/XLSX parse locally, other formats use Gemini. These differences are explicit; no provider output or connection success is simulated by the backend.
