-- The SELECT-only role you would actually hand over. Same as the fixture's.
CREATE ROLE rowfire_ro WITH LOGIN PASSWORD 'rowfire_ro';

GRANT CONNECT ON DATABASE rowfire_fixture TO rowfire_ro;
GRANT USAGE ON SCHEMA public TO rowfire_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO rowfire_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO rowfire_ro;
REVOKE CREATE ON SCHEMA public FROM rowfire_ro;
