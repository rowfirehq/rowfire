-- Demo seed: ~120 days of a B2B SaaS product.
--
-- Deterministic -- modular arithmetic over generate_series, no random() -- so
-- the numbers in the README stay true on every `docker compose up`.
-- Timestamps are relative to now(), so a 120-day backtest always covers it.

INSERT INTO plans (name, monthly_price) VALUES
    ('Trial',        0),
    ('Starter',     49),
    ('Growth',     299),
    ('Enterprise', 1999);

-- --------------------------------------------------------------- accounts
-- Five sign-ups a day for 120 days. Every 23rd is Enterprise, a third are
-- still on a trial, the rest split between Starter and Growth.
INSERT INTO accounts (
    name, domain, plan_id, seats, owner_name, owner_email, region,
    is_internal, created_at, trial_ends_at, activated_at
)
SELECT
    w.company,
    lower(replace(w.company, ' ', '')) || '.example',
    p.plan_id,
    CASE p.plan_id WHEN 4 THEN 50 + (i % 7) * 25 WHEN 3 THEN 10 + i % 30 ELSE 1 + i % 8 END,
    w.owner,
    lower(split_part(w.owner, ' ', 1)) || '@' || lower(replace(w.company, ' ', '')) || '.example',
    (ARRAY['us-east', 'us-west', 'eu-central', 'ap-southeast'])[1 + i % 4],
    false,
    now() - make_interval(days => d, hours => 2 + (i * 5) % 20),
    CASE WHEN p.plan_id = 1 THEN now() - make_interval(days => d - 14, hours => 2 + (i * 5) % 20) END,
    -- About half of trials never activate; paying accounts always did.
    CASE WHEN p.plan_id = 1 AND i % 2 = 0 THEN NULL
         ELSE now() - make_interval(days => d, hours => (i * 5) % 20) END
FROM generate_series(1, 600) AS s(i)
CROSS JOIN LATERAL (SELECT (600 - i) / 5 AS d) AS t
CROSS JOIN LATERAL (
    SELECT CASE
        WHEN i % 23 = 0 THEN 4
        WHEN i % 3 = 0  THEN 1
        WHEN i % 4 = 0  THEN 3
        ELSE 2
    END AS plan_id
) AS p
CROSS JOIN LATERAL (
    SELECT
        (ARRAY['Blue', 'Copper', 'Granite', 'Harbor', 'Juniper', 'Kite', 'Lumen',
               'Meridian', 'Nimbus', 'Orchard', 'Pioneer', 'Quartz', 'Redwood',
               'Summit', 'Tidal', 'Vertex', 'Willow', 'Zephyr', 'Atlas'])[1 + i % 19]
        || ' ' ||
        (ARRAY['Labs', 'Logistics', 'Health', 'Robotics', 'Studio', 'Analytics',
               'Foods', 'Energy', 'Capital', 'Mobility', 'Retail'])[1 + (i / 19) % 11]
            AS company,
        (ARRAY['Ana', 'Ben', 'Chloe', 'Dev', 'Elif', 'Femi', 'Grace', 'Hiro',
               'Ines', 'Jonas', 'Kara', 'Liam', 'Maya', 'Noor', 'Omar', 'Priya'])[1 + i % 16]
        || ' ' ||
        (ARRAY['Adams', 'Brooks', 'Chen', 'Diaz', 'Evans', 'Fischer', 'Garcia',
               'Hughes', 'Ito', 'Jensen', 'Kowalski', 'Lopez', 'Moreau'])[1 + (i / 16) % 13]
            AS owner
) AS w;

-- Internal tenants: staff sandboxes on the Enterprise plan. A sign-up query
-- that forgets `is_internal = false` posts every one of them to #sales.
INSERT INTO accounts (
    name, domain, plan_id, seats, owner_name, owner_email, region,
    is_internal, created_at, activated_at
)
SELECT
    'Internal QA ' || i, 'qa' || i || '.internal.example', 4, 500,
    'QA Bot', 'qa' || i || '@internal.example', 'us-east', true,
    now() - make_interval(days => i * 9, hours => 3),
    now() - make_interval(days => i * 9, hours => 2)
FROM generate_series(1, 12) AS s(i);

-- ------------------------------------------------------- payment attempts
-- Every paying account is charged every 30 days since it signed up.
-- Roughly one charge in nine declines, and a declined invoice is retried
-- twice more: four hours later, then three days later. Some retries recover, some do not -- which is
-- why "a payment failed" fires far more often than "an account has a billing
-- problem", and why the rule, not the query, decides how often to act.
INSERT INTO payment_attempts (
    account_id, invoice_no, amount, attempt, outcome, decline_code, attempted_at
)
SELECT
    a.id,
    'INV-' || lpad(a.id::text, 4, '0') || '-' || lpad(c.cycle::text, 2, '0'),
    pl.monthly_price * CASE WHEN a.plan_id = 2 THEN 1 ELSE greatest(1, a.seats / 10) END,
    r.attempt,
    CASE
        WHEN (a.id * 7 + c.cycle) % 9 <> 0 THEN 'succeeded'
        WHEN r.attempt = 3 AND a.id % 2 = 0 THEN 'succeeded'
        ELSE 'failed'
    END,
    CASE
        WHEN (a.id * 7 + c.cycle) % 9 <> 0 THEN NULL
        WHEN r.attempt = 3 AND a.id % 2 = 0 THEN NULL
        ELSE (ARRAY['card_declined', 'insufficient_funds', 'expired_card'])[1 + a.id % 3]
    END,
    a.created_at + make_interval(days => 30 * c.cycle + r.retry_days, hours => r.retry_hours)
FROM accounts a
JOIN plans pl ON pl.id = a.plan_id
CROSS JOIN generate_series(1, 4) AS c(cycle)
CROSS JOIN (VALUES (1, 0, 1), (2, 0, 5), (3, 3, 1)) AS r(attempt, retry_days, retry_hours)
WHERE a.plan_id > 1
  AND a.created_at + make_interval(days => 30 * c.cycle + r.retry_days, hours => r.retry_hours) < now()
  -- Only declined invoices are retried.
  AND (r.attempt = 1 OR (a.id * 7 + c.cycle) % 9 = 0);

-- ---------------------------------------------------------- NPS responses
-- Four responses a day. Scores 0-6 are detractors, 9-10 promoters.
INSERT INTO nps_responses (account_id, respondent, score, comment, submitted_at)
SELECT
    1 + (i * 13) % 60,
    (ARRAY['ana', 'ben', 'chloe', 'dev', 'elif', 'femi', 'grace', 'hiro'])[1 + i % 8]
        || '@customer.example',
    sc.score,
    CASE
        WHEN sc.score <= 6 THEN (ARRAY[
            'Exports keep timing out on large workspaces.',
            'Too expensive for what we use.',
            'SSO setup took our IT team a week.',
            'Support took three days to answer.',
            'The new dashboard is slower than the old one.'])[1 + i % 5]
        WHEN sc.score >= 9 THEN 'Love it, the team uses it daily.'
    END,
    now() - make_interval(days => i / 4, hours => (i * 7) % 24)
FROM generate_series(0, 479) AS s(i)
CROSS JOIN LATERAL (
    SELECT (ARRAY[10, 9, 8, 9, 3, 10, 7, 6, 9, 10, 2, 8, 9, 5, 10, 9, 8, 0, 10, 9])[1 + i % 20]
        AS score
) AS sc;
