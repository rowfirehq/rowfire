# Distribution surface.
#
#   docker build -t rowfire .
#   docker run --rm \
#     -e ROWFIRE_PLATFORM_DSN='postgresql+psycopg://…' \
#     -e ROWFIRE_MASTER_KEY="$ROWFIRE_MASTER_KEY" \
#     rowfire list
#
# Definitions and the customer connection live in the control plane, so the
# only things this needs are its address and the key that unwraps credentials.
# Both arrive as environment variables; nothing sensitive is baked into a
# layer.

# The front end is built here, not on the host: the build is then reproducible
# for anyone who clones this, and does not depend on whatever Node happens to
# be installed locally (Vite 8 needs >=20.19; a stale system Node will not do).
FROM node:20-alpine AS ui

WORKDIR /ui
# Lockfile first, so a source-only change does not reinstall the world.
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY ui/ ./
RUN npm run build


FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, so a source-only change does not re-resolve them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY src ./src
COPY README.md ./

# The built UI has to land inside the package *before* it is installed, since
# --no-editable copies the package into the venv. FastAPI then serves these
# assets from the same origin as the API, so there is no CORS in production.
COPY --from=ui /ui/dist ./src/rowfire/ui_dist

# --no-editable matters: the default editable install leaves the venv pointing
# at /app/src, which does not exist in the final stage, and the entrypoint
# fails with ModuleNotFoundError.
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim

# Runs as a non-root user with no home-directory writes needed. This image is
# pointed at a customer's production replica; it should be able to do nothing
# except read.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin rowfire

COPY --from=builder /app/.venv /app/.venv

# The control plane is the only store now, so the image has to be able to
# create its schema: a container that can run the app but not migrate it
# leaves `docker compose up` reporting a missing table from every panel.
# alembic.ini uses %(here)s, so `alembic -c /app/alembic.ini` works from any
# working directory.
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations

# The SaaS sample, for the hosted demo on a cloud platform: `rowfire cloud
# predeploy` seeds it into the database and every visitor starts from it.
COPY examples/saas /app/examples/saas

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Definitions are mounted here; nothing is written back.
WORKDIR /work
USER rowfire

ENTRYPOINT ["rowfire"]
CMD ["--help"]
