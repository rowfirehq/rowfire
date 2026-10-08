-- Fixture schema: a small on-demand marketplace.
--
-- Shaped like a real transactional Postgres database, not a warehouse:
-- integer status codes with no enum type, soft foreign keys, a test-account
-- flag with an unhelpful name, and timestamps that are sometimes null.
-- Every awkwardness here is deliberate -- it is what the backtest has to cope
-- with at a design partner.

CREATE TABLE services (
    id         serial PRIMARY KEY,
    name       text NOT NULL,
    slug       text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE customers (
    id         serial PRIMARY KEY,
    first_name text NOT NULL,
    last_name  text NOT NULL,
    phone      text,
    city       text,
    is_test    boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workers (
    id           serial PRIMARY KEY,
    full_name    text NOT NULL,
    phone        text,
    city         text,
    -- 1 = pending, 2 = active, 3 = suspended
    status       smallint NOT NULL DEFAULT 1,
    activated_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE orders (
    id           serial PRIMARY KEY,
    customer_id  integer NOT NULL REFERENCES customers(id),
    worker_id    integer REFERENCES workers(id),
    service_id   integer NOT NULL REFERENCES services(id),
    -- 1 = pending, 2 = confirmed, 3 = in_progress, 4 = completed,
    -- 5 = cancelled, 7 = auto_completed
    --
    -- 4 and 7 both mean "done". A trigger written as `status = 4` looks
    -- correct and silently misses every auto-completed order. This is the
    -- soft-break the concept doc's semantic-drift detection is aimed at.
    status       smallint NOT NULL DEFAULT 1,
    total_amount numeric(10, 2) NOT NULL DEFAULT 0,
    is_test      boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    paid_at      timestamptz
);

CREATE INDEX orders_completed_at_idx ON orders (completed_at);
CREATE INDEX orders_customer_id_idx  ON orders (customer_id);
CREATE INDEX workers_activated_at_idx ON workers (activated_at);
