-- A few minutes of "live" traffic, for watching rules fire in shadow.
--
-- The poller starts every trigger's watermark at now, so the seeded history
-- never fires. Run this after `docker compose up` and the Activity tab fills
-- with the Slack messages and Zendesk tickets that would have been sent:
--
--   docker compose exec -T postgres psql -U rowfire -d rowfire_fixture < examples/saas/simulate.sql
--
-- Safe to run more than once: each run adds new rows, and the fire ledger
-- decides what is genuinely new.

-- Two Enterprise sign-ups and one internal QA tenant the real rule ignores.
INSERT INTO accounts (name, domain, plan_id, seats, owner_name, owner_email, region,
                      is_internal, created_at, activated_at)
VALUES
    ('Halcyon Freight', 'halcyonfreight.example', 4, 250, 'Rosa Lindqvist',
     'rosa@halcyonfreight.example', 'eu-central', false, now(), now()),
    ('Bright Orbit Health', 'brightorbit.example', 4, 120, 'Tomás Reyes',
     'tomas@brightorbit.example', 'us-west', false, now(), NULL),
    ('Internal QA load test', 'qa-load.internal.example', 4, 500, 'QA Bot',
     'qa-load@internal.example', 'us-east', true, now(), now());

-- One bad card, retried three times. #billing hears about it once today and
-- billing gets one ticket this week, not three.
INSERT INTO payment_attempts (account_id, invoice_no, amount, attempt, outcome,
                              decline_code, attempted_at)
SELECT a.id, 'INV-' || a.id || '-LIVE', 2990.00, r.attempt, 'failed',
       'insufficient_funds', now() - make_interval(mins => 3 - r.attempt)
FROM accounts a
CROSS JOIN generate_series(1, 3) AS r(attempt)
WHERE a.name = 'Meridian Capital' AND a.plan_id > 1 AND NOT a.is_internal
ORDER BY a.id
LIMIT 3;

INSERT INTO payment_attempts (account_id, invoice_no, amount, attempt, outcome,
                              decline_code, attempted_at)
SELECT id, 'INV-' || id || '-LIVE', 49.00, 1, 'failed', 'expired_card', now()
FROM accounts WHERE name = 'Kite Labs' AND plan_id > 1 AND NOT is_internal ORDER BY id LIMIT 1;

-- Two unhappy customers and one happy one.
INSERT INTO nps_responses (account_id, respondent, score, comment, submitted_at)
SELECT id, 'jonas@customer.example', 3,
       'Exports keep timing out on large workspaces.', now()
FROM accounts WHERE name = 'Summit Robotics' AND NOT is_internal ORDER BY id LIMIT 1;

INSERT INTO nps_responses (account_id, respondent, score, comment, submitted_at)
SELECT id, 'priya@customer.example', 5,
       'SSO setup took our IT team a week.', now()
FROM accounts WHERE name = 'Tidal Analytics' AND NOT is_internal ORDER BY id LIMIT 1;

INSERT INTO nps_responses (account_id, respondent, score, comment, submitted_at)
SELECT id, 'maya@customer.example', 10, 'Love it, the team uses it daily.', now()
FROM accounts WHERE name = 'Willow Foods' AND NOT is_internal ORDER BY id LIMIT 1;
