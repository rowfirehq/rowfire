-- Demo schema: a small B2B SaaS product.
--
-- Used for the README walkthrough and its screenshots. It is not the test
-- fixture (that lives in fixtures/ and its counts are asserted by the suite);
-- this one exists to show the tool on a story most people recognise: a
-- billing system, a trial funnel and an NPS survey, wired to Slack and
-- Zendesk.
--
-- Like the fixture, it is shaped like an application database rather than a
-- warehouse: a status column with no enum, an internal-account flag a naive
-- query forgets, and timestamps that are sometimes null.

CREATE TABLE plans (
    id            serial PRIMARY KEY,
    name          text NOT NULL UNIQUE,
    monthly_price numeric(10, 2) NOT NULL
);

CREATE TABLE accounts (
    id            serial PRIMARY KEY,
    name          text NOT NULL,
    domain        text NOT NULL,
    plan_id       integer NOT NULL REFERENCES plans(id),
    seats         integer NOT NULL DEFAULT 1,
    owner_name    text NOT NULL,
    owner_email   text NOT NULL,
    region        text NOT NULL,
    -- Staff sandboxes and QA tenants. They look exactly like customers.
    is_internal   boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    trial_ends_at timestamptz,
    -- First time the account did the thing the product is for. Null means
    -- they never got there.
    activated_at  timestamptz
);

-- One row per charge attempt. A failed invoice is retried, so one bad card
-- produces several failures in a few days.
CREATE TABLE payment_attempts (
    id           serial PRIMARY KEY,
    account_id   integer NOT NULL REFERENCES accounts(id),
    invoice_no   text NOT NULL,
    amount       numeric(10, 2) NOT NULL,
    attempt      smallint NOT NULL,
    -- 'succeeded' | 'failed'
    outcome      text NOT NULL,
    decline_code text,
    attempted_at timestamptz NOT NULL
);

CREATE TABLE nps_responses (
    id           serial PRIMARY KEY,
    account_id   integer NOT NULL REFERENCES accounts(id),
    respondent   text NOT NULL,
    score        smallint NOT NULL CHECK (score BETWEEN 0 AND 10),
    comment      text,
    submitted_at timestamptz NOT NULL
);

CREATE INDEX accounts_created_at_idx       ON accounts (created_at);
CREATE INDEX payment_attempts_at_idx       ON payment_attempts (attempted_at);
CREATE INDEX nps_responses_submitted_at_idx ON nps_responses (submitted_at);
