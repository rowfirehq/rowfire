# Contributing

Thanks for your interest in contributing! Bug reports, ideas, and pull requests are all welcome.

## Getting started

The project has two parts:

- `src/rowfire/` — the Python engine, CLI, control plane, and FastAPI server
- `ui/` — the React + Vite + TypeScript front end

You need [uv](https://docs.astral.sh/uv/), Docker, and Node.js 20.19 or newer (see `.nvmrc`).

```bash
uv sync
docker compose up -d postgres controlplane
```

This starts the fixture database on port 5433 and the control plane on port 5434. The
[README](README.md#local-development) covers running the CLI and the UI against them.

For the front end with hot reload:

```bash
uv run rowfire serve --dev-origin http://localhost:5173
cd ui && npm install && npm run dev
```

## Before opening a pull request

CI runs these, so running them first saves a round trip:

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest
cd ui && npm run typecheck && npm run build
```

Database-backed tests are marked `db` and skip when the databases are not running. CI
always runs them, so please run the full suite locally too.

## Guidelines

- Keep pull requests focused — one feature or fix per PR.
- Add or update tests for behavior changes. `fixtures/seed.sql` is deterministic; if a
  test needs a new edge case, plant it there.
- If you change a control-plane model, add an Alembic migration
  (`uv run alembic revision --autogenerate -m "..."`).
- Nothing may ever write to the database being read. Changes near trigger SQL validation
  get extra scrutiny.
- Describe what changed and why in the PR, with screenshots for UI changes.

## Reporting bugs and requesting features

Open an issue using the templates provided. For security problems, please follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## Code of conduct

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
