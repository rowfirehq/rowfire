-- A burst of activity, different every time: what the "Simulate new
-- activity" button runs against the SaaS sample database.
--
-- Unlike simulate.sql, which replays the same story for the README, this
-- picks accounts at random. A rule capped at once a day per account would
-- hold back a second run of a fixed script entirely, and a button that
-- usually does nothing reads as broken.
--
-- Run by RowFire only when a deployment sets both ROWFIRE_DEMO_ACTIVITY_DSN
-- (a write-capable login to *this sample database*, never a data source) and
-- ROWFIRE_DEMO_ACTIVITY_SQL (this file). By hand:
--
--   docker compose exec -T postgres psql -U rowfire -d rowfire_fixture < examples/saas/activity.sql

-- A new Enterprise customer, with a made-up name that is new each time.
INSERT INTO accounts (name, domain, plan_id, seats, owner_name, owner_email, region,
                      is_internal, created_at, activated_at)
SELECT company || ' ' || suffix,
       lower(company || suffix) || '.example',
       (SELECT id FROM plans WHERE name = 'Enterprise'),
       (50 + floor(random() * 450))::int,
       owner,
       lower(split_part(owner, ' ', 1) || '@' || company || suffix) || '.example',
       (ARRAY['us-east', 'us-west', 'eu-central', 'ap-south'])[1 + floor(random() * 4)::int],
       false, now(), NULL
FROM (
    SELECT (ARRAY['Northwind', 'Bluefin', 'Lumen', 'Cedar', 'Quarry', 'Atlas',
                  'Harbor', 'Juniper', 'Vertex', 'Solace'])[1 + floor(random() * 10)::int] AS company,
           (ARRAY['Logistics', 'Health', 'Robotics', 'Capital', 'Foods',
                  'Studios', 'Energy', 'Labs'])[1 + floor(random() * 8)::int] AS suffix,
           (ARRAY['Ines Duarte', 'Kofi Mensah', 'Lena Vogel', 'Arjun Rao',
                  'Mei Tanaka', 'Sam Okafor'])[1 + floor(random() * 6)::int] AS owner
) AS made_up;

-- A trial that ends in three days and was never set up: the moment the
-- trial_stalled trigger exists for. Every trigger Get started offers needs
-- something here, or a visitor who picks it waits for a message that can
-- never come (tests/test_examples.py checks that each one gets a row).
INSERT INTO accounts (name, domain, plan_id, seats, owner_name, owner_email, region,
                      is_internal, created_at, trial_ends_at, activated_at)
SELECT company || ' ' || suffix,
       lower(company || suffix) || '.example',
       (SELECT id FROM plans WHERE name = 'Trial'),
       (3 + floor(random() * 20))::int,
       owner,
       lower(split_part(owner, ' ', 1) || '@' || company || suffix) || '.example',
       (ARRAY['us-east', 'us-west', 'eu-central', 'ap-south'])[1 + floor(random() * 4)::int],
       false, now() - interval '11 days', now() + interval '3 days', NULL
FROM (
    SELECT (ARRAY['Maple', 'Orbit', 'Pioneer', 'Riverbend', 'Summit', 'Tandem',
                  'Upland', 'Willow', 'Zenith', 'Copper'])[1 + floor(random() * 10)::int] AS company,
           (ARRAY['Analytics', 'Studio', 'Supply', 'Media', 'Partners',
                  'Works', 'Group', 'Systems'])[1 + floor(random() * 8)::int] AS suffix,
           (ARRAY['Noor Haddad', 'Pablo Ruiz', 'Ada Okoro', 'Lukas Berg',
                  'Hana Sato', 'Omar Farouk'])[1 + floor(random() * 6)::int] AS owner
) AS made_up;

-- Two declined charges, on two paying customers picked at random.
--
-- Picked in a subquery, with the other random values drawn outside it: in one
-- select list Postgres reuses the ORDER BY random() value for every other
-- random() call, and since LIMIT keeps the smallest, every pick came out as
-- the first entry and every score as 0.
INSERT INTO payment_attempts (account_id, invoice_no, amount, attempt, outcome,
                              decline_code, attempted_at)
SELECT picked.id,
       'INV-' || picked.id || '-' || to_char(now(), 'MMDDHH24MISS'),
       picked.monthly_price,
       1,
       'failed',
       (ARRAY['insufficient_funds', 'expired_card', 'card_declined'])[1 + floor(random() * 3)::int],
       now()
FROM (
    SELECT a.id, p.monthly_price
    FROM accounts a
    JOIN plans p ON p.id = a.plan_id
    WHERE p.monthly_price > 0 AND NOT a.is_internal
    ORDER BY random()
    LIMIT 2
) AS picked;

-- Two unhappy customers and one happy one, also at random.
INSERT INTO nps_responses (account_id, respondent, score, comment, submitted_at)
SELECT picked.id, picked.owner_email, floor(random() * 7)::int,
       (ARRAY['Exports keep timing out on large workspaces.',
              'Onboarding took far longer than we were told.',
              'The new billing page is confusing.',
              'Support took three days to answer an outage ticket.',
              'Too many clicks to do anything simple.'])[1 + floor(random() * 5)::int],
       now()
FROM (
    SELECT id, owner_email FROM accounts WHERE NOT is_internal ORDER BY random() LIMIT 2
) AS picked;

INSERT INTO nps_responses (account_id, respondent, score, comment, submitted_at)
SELECT picked.id, picked.owner_email, 9 + floor(random() * 2)::int,
       'Love it, the team uses it daily.', now()
FROM (
    SELECT id, owner_email FROM accounts WHERE NOT is_internal ORDER BY random() LIMIT 1
) AS picked;
