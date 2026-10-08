# Security Policy

This project connects to other people's production databases and stores encrypted
credentials for them and for outbound connectors, so security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report vulnerabilities privately using GitHub's
[private vulnerability reporting](../../security/advisories/new) (the "Report a
vulnerability" button on the Security tab). You should get a response within a few days.

Please include:

- A description of the issue and its impact
- Steps to reproduce or a proof of concept
- Any suggested fix, if you have one

Issues of particular interest:

- Any way a trigger's SQL can write to, or lock, the database being read
- Any way to read stored credentials without the master key
- Any way to drive the local API from a page the user visits
- On a hosted demo (`ROWFIRE_HOSTED=1`): any way for one visitor to read or change
  another's workspace, or to make the server connect or send anywhere but the
  sample databases and the Demo inbox

### Known limitation

Integrations may currently call any `http`/`https` URL, including private and
link-local addresses. This is documented in the
[README](README.md#egress-is-currently-unrestricted) and is acceptable for the
supported single-tenant, self-hosted deployment. Reports of it alone are not
needed; a fix that adds an allow/deny policy in `dispatch._check_url` is welcome
as a pull request.

## Supported versions

Only the latest version on `main` receives security fixes.

## Running it safely

- Connect with a role that has `SELECT` only. The tool never writes, but least privilege
  means it cannot.
- Keep `ROWFIRE_MASTER_KEY` out of version control and somewhere durable. Losing it
  makes every stored credential unrecoverable.
- The UI is designed to be local-only: it binds to `127.0.0.1` and rejects non-loopback
  `Host` headers. Do not expose it to a network.
- The passwords in `docker-compose.yml` and `fixtures/` are for the local fixture
  databases only.
- Do not set `ROWFIRE_DEMO_ACTIVITY_DSN` or `ROWFIRE_DEMO_ACTIVITY_SQL` outside a
  demo. Together they enable the "Simulate new activity" button, the one thing
  that writes to a database, and only `examples/saas/compose.yaml` sets them.
