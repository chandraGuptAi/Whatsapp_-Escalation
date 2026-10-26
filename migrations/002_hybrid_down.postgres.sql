-- MANUAL ONLY. Stop writers and back up the private schema first.
-- Removes new cache/observability metadata. Historical reports/messages remain intact.
BEGIN;
SET LOCAL search_path TO escalation_analyst;
DROP TABLE IF EXISTS analysis_cache;
DROP TABLE IF EXISTS analysis_units;
DROP TABLE IF EXISTS analysis_ai_calls;
DROP TABLE IF EXISTS analysis_observations;
DROP TABLE IF EXISTS report_groups;
ALTER TABLE reports DROP COLUMN IF EXISTS message_hash;
ALTER TABLE usage_log DROP COLUMN IF EXISTS reasoning_tokens;
ALTER TABLE usage_log DROP COLUMN IF EXISTS latency_ms;
ALTER TABLE usage_log DROP COLUMN IF EXISTS retry_count;
DELETE FROM schema_version WHERE version=2;
COMMIT;
