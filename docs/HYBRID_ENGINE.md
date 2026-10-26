# Hybrid master engine: implementation audit and rollout

Date: 26 October 2026. This is a gated implementation, **not approval to replace the
production analyst**. No live environment file, service, WhatsApp session or production
database was changed during development. Source changes take effect at the next
operator-managed service start. Baseline: 60 existing tests passed in 45.55 seconds.

## Existing architecture, audited before changes

The ERP uses `E:/Office/Bigship-ERP-Adminpanel/src/app/modules/escalation-analyst`.
`master/master.component.ts:generate()` calls POST `/master/analyse` through
`escalation-api.service.ts`. Public requests reach port 8200:
`AI_python_backend/app/api_gateway.py:ServiceGateway.__call__()` streams to port 8201.
`python-whatsapp-service/app/main.py:build_app()` mounts `/api/escalation`.
The main backend's `app/services/deepseek_service.py` belongs to other AI Box workflows;
it is not the master escalation provider and was not modified.

Actual processing map:

```text
whatsapp/native.py:NativeWhatsApp._register
  -> whatsapp/messages.py:MessageReceiver.live/history/save
  -> whatsapp/ingest.py:ingest
  -> repositories/groups.py:Groups.ingest_info/ingest
  -> repositories/groups.py:Groups.update_body (asynchronous media enrichment)

api/reports.py:master
  -> Runtime.start -> Jobs.claim (database unique active lock)
  -> Runtime._runner (executor + heartbeat + persisted result/error)
  -> Analysis.master -> Analysis._master
  -> Analysis._run_groups (bounded ThreadPoolExecutor)
  -> Analysis.group_for_master -> Runtime.run_inline (shared per-group claim)
  -> Analysis.group -> Groups.messages -> Reports.base
  -> analysis/provider.py:Provider.generate -> Provider._stream
  -> Reports.save -> Progress.checkpoint/finish
  -> aggregate.summarize / Analysis._history / aggregate.carry_over
  -> Analysis._batches / _digest -> Provider.generate (legacy master narrative)
  -> aggregate.validate_narrative / render_sections -> Reports.save

api/reports.py:progress -> Jobs.get (durable snapshots)
api/reports.py:bulk_export -> exports/bulk.py:export_bulk
api/reports.py:download -> exports/documents.py:export
scheduler.py:tick and whatsapp/messages.py:MessageReceiver.live also start analysis.
```

`repositories/database.py:Database` lazily initializes private PostgreSQL schema
`escalation_analyst`, or a separate SQLite database. Tables cover messages, groups,
clusters, schedules, reports, usage, jobs and presets. `migrate.py:migrate` imports
legacy records. Reports are append-only. Existing tests cover ingestion/auth,
single-flight, scheduler/import, exports, cancellation, 349-group concurrency,
performance snapshot parity and protocol lifecycle. Existing logistics/performance
engines already extract AWBs/classify messages, but do not establish seller-confirmed
closure without authoritative links. Their mappings are supplied per performance
request, not a reliable roster available to master analysis.

### Measured versus reported bottlenecks

`prompts/escalation-analyst.md` is 17,611 bytes. Every fresh group uses it and the
complete transcript, or an incremental transcript plus previous prose. Legacy defaults
are 80,000 output tokens, reasoning `max`, four group workers/four AI slots,
two retries, 1,500 second overall request timeout. Environment overrides can differ.
`analysis/observe.py:RunStats` already measures stages, while `Progress` estimates
remaining duration from observed throughput and recent tail timings.

Run #347's 75–79 minutes, 178-second median, 350-second maximum and 107 groups are
user-supplied observations, not reproduced production measurements. No provider call
or paid baseline was made. The original usage path saved prompt/completion totals but
not reasoning tokens. The claimed 80–90% reasoning share remains unverified.

## Implemented architecture

`Analysis.group` selects legacy/shadow/hybrid. Hybrid calls
`analysis/hybrid_service.py:HybridAnalysis.group`, using typed Pydantic objects from
`analysis/hybrid.py`, the existing AWB extractor, source validation and immutable
database checkpoints. Master totals still use existing Python aggregation; hybrid
master narrative uses deterministic templates (`aggregate.hybrid_narrative`) and no
master AI call. Existing Markdown table columns and machine-count keys remain intact.

### Routing and evidence policy

Only a **full-message** explicit numeric AWB/tracking label followed by an enumerated
factual status can bypass AI. Supported statuses: delayed, in transit, out for delivery,
pickup pending, not picked, delivery pending. Full matching avoids negative/future
phrases accidentally matching completion keywords. No response/SLA/closure/identity
is inferred. Distinct message IDs, WhatsApp IDs, timestamps and quotes are preserved.
Same text later is a follow-up, not automatically a duplicate. WhatsApp-ID duplicates
are removed; fallback dedup uses sender + timestamp + exact content.

Other messages route light; conflicting status statements, reopen/partial/negative
language or the configurable distinct-AWB threshold route deep. Message volume alone
does not determine complexity. AI produces `AIResult` JSON with `Case` objects,
literal evidence IDs and confidence. Validation rejects fabricated AWBs, omitted
explicit numeric references, unsupported couriers, duplicate shipment rows, unknown
source IDs and unproven closure. Semantic classifications remain provisional even
after structural/source checks. Missing identities and times remain explicit.

`verified_closed` is deliberately rejected until authoritative seller/proof linkage
is supplied. All AI-supported results require review. A schema/source check is not
proof of semantic correctness. Absence of classified abuse/critical flags is not proof
of their absence. The compatible numeric fields for unsupported response/follow-up
metrics remain zero, with missing-evidence text explaining that they are unknown;
these must not be interpreted as measured absence.

### Prompt and provider changes

`prompts/hybrid-analyst.md`: 2,303 UTF-8 bytes versus 17,611 (about 87% smaller).
It retains the core taxonomy, staff-initiated issues, promise/claim/closure distinction,
deduplication, Hindi/Hinglish support, source constraints and review requirements.
The AI request includes the JSON schema, so total input reduction depends on the case;
prompt-file reduction is not token/cost/latency reduction.

The existing streaming client remains in use. Official DeepSeek v4 Chat Completions
supports `thinking.type` and `reasoning_effort`; route overrides are restricted to
`api.deepseek.com` / `deepseek-v4-pro`. Empty route effort retains existing settings.
See https://api-docs.deepseek.com/guides/thinking_mode/ and
https://api-docs.deepseek.com/api/create-chat-completion/ (checked 26 October 2026).
No real provider response was sampled. Reasoning tokens are nullable and only recorded
when `completion_tokens_details.reasoning_tokens` is exposed. They are a subset of
completion tokens, never added again for cost. Estimates retain existing configured
prices; verify rates before using them for billing decisions.

Legacy retries use bounded exponential backoff with jitter and Retry-After, with no
retries for nontransient 4xx, malformed/truncated output or successful responses whose
usage persistence fails. Global process-local AI concurrency remains independently
bounded. `ESCALATION_AI_MIN_INTERVAL_SECONDS` optionally paces requests. Hybrid calls
have zero automatic retries: a durable call ledger prevents another paid call after
an uncertain timeout/crash, and reuses a completed response even if validation/save
was interrupted. An operator must investigate uncertain calls against provider billing
before explicitly clearing their ledger record. Changing relevant configuration makes
new logical work and can incur a new charge. There is no claim of provider-side exactly
once execution, distributed provider quotas or adaptive capacity estimation.

### Persistent caching and incremental analysis

`repositories/analysis_cache.py:AnalysisCache` keys outputs by full message content,
stable IDs, timestamps, authors, group name, exact window, schema/analyzer/prompt/model
configuration and selected context. Edits/media enrichment/deletion invalidate reuse;
TTL/max-message-ID alone cannot authorize it. Cache entries preserve structured evidence,
review status, duration, AI usage and report references. Historical reports are never
rewritten. Provisional results can be reused but retain their review requirement.

With incremental enabled, `analysis_units` reuses versioned per-message extraction
facts. Only changed/new text is re-extracted. The inexpensive group timeline/routing
is recomputed for correctness; changed ambiguous groups still receive their complete
deduplicated evidence. There is no unsafe delta-only semantic/closure merge.
Legacy reuse now also checks full source content and model configuration for new reports.
Pre-existing unversioned legacy reports retain their old compatibility policy.

### Jobs, recovery, progress and ownership

Reuse the existing database jobs instead of introducing Redis. This deployment already
has PostgreSQL, persistent claims/heartbeats and one service worker; another queue adds
operational cost without solving provider ambiguity. CPU/DB workers and provider slots
retain separate limits. Completed group reports are checkpoints. Stale workers become
failed after heartbeat expiry; POST `/master/runs/{id}/retry` resumes a **new run with the
same frozen window**, bypassing force and reusing compatible checkpoints. Recovery is
operator-triggered, not an autonomous restart/resubmission daemon. Current group scope
is re-evaluated on retry; membership changes are not a positional resume.

Progress payload fields and cancellation APIs remain compatible. GET
`/master/runs/{id}/events` emits authenticated SSE progress and heartbeats. Angular uses
fetch streaming so existing identity headers remain available, includes an inactivity
deadline, aborts on component teardown and falls back to persisted polling on disconnect
or older servers. Reconnection never starts a report. Retry UI calls the new endpoint.
ETA remains based on existing observed work/tail durations, not fixed fake percentages.

New master reports atomically link exact child report IDs in `report_groups`.
Run snapshots also record child IDs. Downloads use those references, so later reports
and current membership cannot silently replace historical/partial export contents.
Old reports without links retain their compatible window-selection fallback.

## Migration and rollback

Schema v2 is additive/lazy through `repositories/schema.py` and `Database.initialize`:
new tables `analysis_cache`, `analysis_units`, `analysis_ai_calls`,
`analysis_observations`, `report_groups`; nullable `reports.message_hash`;
nullable usage reasoning tokens, latency and retry count. No existing business records
are rewritten or deleted. A controlled startup's first DB access applies it. All
instances using new code require the new metadata tables.

Normal rollback: use legacy mode and leave additive metadata in place. Code rollback
does not require destructive SQL. Optional manually executed reverse migrations are
`migrations/002_hybrid_down.postgres.sql` and `.sqlite.sql` (SQLite >=3.35). Stop writers
and back up first. They remove only new metadata, including cached AI responses and
call protection; never run them while new workers are active. Never apply to live DB
as part of verification. PostgreSQL up/down/up was tested in a disposable container.

## Environment settings

Conservative defaults are in `settings.py`; no live `.env` was edited.

- `ESCALATION_ENGINE_MODE=legacy`: legacy, shadow or hybrid. Invalid values fail startup.
- `ESCALATION_HYBRID_ENABLED=false`: hybrid mode also requires explicit true.
- `ESCALATION_AI_ROUTING_ENABLED=false`: ambiguous hybrid groups fail reviewably until enabled.
- `ESCALATION_INCREMENTAL_ENABLED=false`: reuse persistent per-message extraction units.
- `ESCALATION_SHADOW_ENABLED=false`: explicitly enables zero-additional-AI shadow observations.
- `ESCALATION_LIGHT_REASONING=` and `ESCALATION_COMPLEX_REASONING=`: empty retains provider
  settings. Official v4 overrides accept none/low/high/max; unsupported values retain settings.
- `ESCALATION_HYBRID_MAX_TOKENS=8192`: hybrid AI response budget; truncation fails validation.
- `ESCALATION_DEEP_AWB_THRESHOLD=4`: distinct shipment count contributing to deep routing.
- `ESCALATION_AI_MIN_INTERVAL_SECONDS=0`: optional global request spacing per service process.
- Existing `MASTER_ANALYSIS_CONCURRENCY=4`, `MASTER_AI_MAX_CONCURRENCY=4`,
  `MASTER_AI_MAX_RETRIES=2`, `MASTER_AI_RETRY_BACKOFF_SECONDS=2`,
  `ESCALATION_AI_TIMEOUT_SECONDS=1500`, `ESCALATION_JOB_STALE_SECONDS=300`,
  `MASTER_PROGRESS_POLL_INTERVAL_HINT=2`, `MASTER_PROMPT_MAX_CHARS=600000` remain.
- Existing `MASTER_ANALYSIS_INCREMENTAL_ENABLED=true` controls the legacy delta prompt,
  and is distinct from the new per-message extraction flag.

## Benchmarks and verification

`scripts/benchmark_hybrid.py --output benchmarks/hybrid-synthetic.json` is an offline
extractor benchmark. Optional `--dataset reviewed.json` accepts fixed messages,
expected AWBs and recorded legacy report text, records metric differences and marks
them for review. It makes no provider calls. A synthetic 107-group run took 8.795 ms
in extraction/source validation (p50 .0708 ms, p95 .08243 ms, p99 .118132 ms). This excludes
DB jobs, persistence, exports, AI and frontend; **not comparable to the production run**.
The integration test separately exercises 107 durable group reports and zero AI calls,
then reuses all 107 through a fresh database connection. Synthetic AWB precision/recall
is not escalation precision/recall and cannot establish a rollout threshold.

Regression tests include English/Hindi/Hinglish routing, exact/ambiguous statements,
empty/missing-time/300-message cases, multiple references/conflicts, unsupported closure,
duplicate messages, changed content/config/window, persistent incremental units,
uncertain paid-call recovery, historical exports, schema/usage persistence, timeouts,
rate-limit backoff and SSE/retry/auth. Existing cancellation/single-flight/API/export
tests continue to run. See `benchmarks/verification.txt` for final check results.

## Rollout and remaining gates

1. Keep legacy mode during active runs. Do not restart a service to deploy mid-report.
2. At an approved maintenance point, back up the private schema and deploy code in
   legacy mode. Validate existing ERP/API/export contracts.
3. Enable shadow mode plus shadow flag in staging first. Shadow records extraction
   route/count proposals without additional AI calls or changing published reports.
4. Label a representative Hindi/English/Hinglish golden set independently; compare
   classifications, response/closure facts, escalation precision/recall, omissions and
   exported metrics with recorded legacy outputs. Set business-approved thresholds.
5. Enable hybrid and AI routing only for a controlled staging dataset. Measure total
   wall-clock p50/p95/p99, provider-reported reasoning usage, token cost, throughput and
   quality. Set route-specific reasoning overrides only after that comparison.
6. Roll out a small approved production scope; rollback by selecting legacy mode at
   the next controlled restart. Do not mix engine settings during active runs.

Remaining limits: no human-reviewed hybrid golden set, no paid provider benchmark,
no verified production 5–10x improvement, no demonstrated semantic accuracy parity.
Authoritative closure, staff response/SLA metrics, rich urgency/emotion analysis and
selected prior-report context are not yet supported in the conservative hybrid path;
prior context requests fail explicitly rather than dropping context. Legacy remains
available for those workflows. No automatic restart recovery daemon, distributed AI
rate controller, live UI/browser test or manual-review adjudication UI was added.
Hybrid output is therefore a staging/review capability, not full business parity with
the legacy analyst. These gates must be closed before production replacement.
