-- MANUAL ONLY, SQLite >= 3.35. Stop writers and back up the file first.
BEGIN;
DROP TABLE IF EXISTS analysis_cache;
DROP TABLE IF EXISTS analysis_units;
DROP TABLE IF EXISTS analysis_ai_calls;
DROP TABLE IF EXISTS analysis_observations;
DROP TABLE IF EXISTS report_groups;
ALTER TABLE reports DROP COLUMN message_hash;
ALTER TABLE usage_log DROP COLUMN reasoning_tokens;
ALTER TABLE usage_log DROP COLUMN latency_ms;
ALTER TABLE usage_log DROP COLUMN retry_count;
DELETE FROM schema_version WHERE version=2;
COMMIT;
