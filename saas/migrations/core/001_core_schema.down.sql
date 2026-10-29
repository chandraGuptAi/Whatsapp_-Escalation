-- MANUAL ONLY. Destroys all SaaS data in schema "app". Back up first.
BEGIN;
DROP SCHEMA IF EXISTS app CASCADE;
COMMIT;
-- The app_user role is cluster-wide and may be shared; drop it separately if no longer used:
--   DROP ROLE IF EXISTS app_user;
