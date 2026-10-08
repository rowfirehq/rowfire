-- Runs last (alphabetically after schema.sql and seed.sql).
--
-- Creates the SELECT-only role a design partner would actually hand over.
-- The CLI's `SET default_transaction_read_only = on` is belt; this is braces.
-- Tests assert that a write fails, and it should fail for either reason.

CREATE ROLE rowfire_ro WITH LOGIN PASSWORD 'rowfire_ro';

GRANT CONNECT ON DATABASE rowfire_fixture TO rowfire_ro;
GRANT USAGE ON SCHEMA public TO rowfire_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO rowfire_ro;

-- Anything created later stays readable, nothing more.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO rowfire_ro;

-- Explicitly withhold the rest, in case of a permissive default.
REVOKE CREATE ON SCHEMA public FROM rowfire_ro;
