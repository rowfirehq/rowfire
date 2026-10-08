-- Fixture seed: ~2000 orders across a 120-day window.
--
-- Fully deterministic -- modular arithmetic over generate_series, no random().
-- Repeated `docker compose down && up` produces byte-identical data, so tests
-- can assert exact counts instead of ranges.
--
-- Timestamps are relative to now(), so `--days 120` always covers the window.
--
-- Six deliberate edge cases are planted here; each one is labelled below and
-- each has a test that depends on it.

-- ---------------------------------------------------------------- services
INSERT INTO services (name, slug) VALUES
    ('Home Cleaning',   'home_cleaning'),
    ('AC Maintenance',  'ac_maintenance'),
    ('Plumbing',        'plumbing'),
    ('Pest Control',    'pest_control');

-- --------------------------------------------------------------- customers
-- 200 real customers.
INSERT INTO customers (first_name, last_name, phone, city, is_test, created_at)
SELECT
    'Customer' || i,
    'Family' || (i % 40),
    '+2010' || lpad(i::text, 8, '0'),
    (ARRAY['Cairo', 'Alexandria', 'Giza', 'Mansoura'])[1 + (i % 4)],
    false,
    now() - make_interval(days => 120 + (i % 200))
FROM generate_series(1, 200) AS s(i);

-- EDGE CASE 2 (part 1): test accounts. Real-looking, must be excluded.
INSERT INTO customers (first_name, last_name, phone, city, is_test, created_at)
SELECT
    'QA' || i,
    'Testaccount',
    '+2011' || lpad(i::text, 8, '0'),
    'Cairo',
    true,
    now() - make_interval(days => 300)
FROM generate_series(1, 5) AS s(i);

-- ----------------------------------------------------------------- workers
-- 40 active (activated_at set), 10 pending (null), 10 suspended (but were
-- activated once -- so `status = 2` and `activated_at IS NOT NULL` disagree).
INSERT INTO workers (full_name, phone, city, status, activated_at, created_at)
SELECT
    'Worker' || i,
    '+2012' || lpad(i::text, 8, '0'),
    (ARRAY['Cairo', 'Alexandria', 'Giza', 'Mansoura'])[1 + (i % 4)],
    CASE
        WHEN i <= 40 THEN 2::smallint   -- active
        WHEN i <= 50 THEN 1::smallint   -- pending
        ELSE 3::smallint                -- suspended
    END,
    CASE
        WHEN i <= 40 THEN now() - make_interval(days => (i * 3) % 120, hours => 4)
        WHEN i <= 50 THEN NULL          -- never activated
        ELSE now() - make_interval(days => 200)  -- activated, later suspended
    END,
    now() - make_interval(days => 210)
FROM generate_series(1, 60) AS s(i);

-- ------------------------------------------------------------------ orders
-- Main body: 90 days x 15 orders = 1350.
--
-- EDGE CASE 5: day offsets 30..59 are skipped entirely -- a full month of
-- zero volume inside the window, to exercise empty-bucket rendering and the
-- min-day statistic.
--
-- EDGE CASE 4: status 7 (auto_completed) is emitted alongside 4 (completed).
-- Both mean "done". `status = 4` silently misses ~135 rows.
INSERT INTO orders (
    customer_id, worker_id, service_id, status,
    total_amount, is_test, created_at, completed_at, paid_at
)
SELECT
    1 + (idx % 200),
    1 + (idx % 60),
    1 + (idx % 4),
    st,
    (50 + (idx * 37) % 450)::numeric(10, 2),
    false,
    now() - make_interval(days => d.day_offset, hours => 20),
    -- Only terminal states carry a completion timestamp.
    CASE WHEN st IN (4, 7)
         THEN now() - make_interval(days => d.day_offset, hours => 24 - n.seq)
    END,
    -- EDGE CASE: ~1 in 47 completed orders is unpaid, so `paid_at IS NOT NULL`
    -- is doing real work in the trigger rather than being decorative.
    CASE WHEN st IN (4, 7) AND idx % 47 <> 0
         THEN now() - make_interval(days => d.day_offset, hours => 23 - n.seq)
    END
FROM generate_series(0, 119) AS d(day_offset)
CROSS JOIN generate_series(1, 15) AS n(seq)
CROSS JOIN LATERAL (SELECT d.day_offset * 15 + n.seq AS idx) AS c
CROSS JOIN LATERAL (
    SELECT CASE c.idx % 10
        WHEN 0 THEN 1::smallint   -- pending
        WHEN 1 THEN 2::smallint   -- confirmed
        WHEN 2 THEN 3::smallint   -- in_progress
        WHEN 3 THEN 5::smallint   -- cancelled
        WHEN 4 THEN 7::smallint   -- auto_completed  <- EDGE CASE 4
        ELSE        4::smallint   -- completed
    END AS st
) AS s
WHERE d.day_offset NOT BETWEEN 30 AND 59;   -- <- EDGE CASE 5

-- EDGE CASE 1: status = 4, paid, is_test = false -- but completed_at IS NULL.
-- These satisfy the trigger predicate yet cannot be placed on the timeline.
-- The backtest must count and surface them, never silently drop them.
INSERT INTO orders (
    customer_id, worker_id, service_id, status,
    total_amount, is_test, created_at, completed_at, paid_at
)
SELECT
    1 + (i % 200), 1 + (i % 60), 1 + (i % 4), 4::smallint,
    (120 + i)::numeric(10, 2), false,
    now() - make_interval(days => (i % 90), hours => 20),
    NULL,                                          -- <- the timeline gap
    now() - make_interval(days => (i % 90), hours => 2)
FROM generate_series(1, 40) AS s(i);

-- EDGE CASE 2 (part 2): orders belonging to test accounts (customers 201-205),
-- flagged is_test = true. A predicate that forgets `is_test = false` picks up
-- all 50 of these.
INSERT INTO orders (
    customer_id, worker_id, service_id, status,
    total_amount, is_test, created_at, completed_at, paid_at
)
SELECT
    201 + (i % 5), 1 + (i % 60), 1 + (i % 4), 4::smallint,
    (999)::numeric(10, 2), true,
    now() - make_interval(days => (i % 100), hours => 20),
    now() - make_interval(days => (i % 100), hours => 6),
    now() - make_interval(days => (i % 100), hours => 5)
FROM generate_series(1, 50) AS s(i);

-- EDGE CASE 3: one customer, 40 completed+paid orders inside a single week
-- (day offsets 5..11). This is the dedup and frequency-cap pressure test:
-- once_ever collapses these to 1, once_per_period to ~1 per period.
INSERT INTO orders (
    customer_id, worker_id, service_id, status,
    total_amount, is_test, created_at, completed_at, paid_at
)
SELECT
    1, 1 + (i % 60), 1 + (i % 4), 4::smallint,
    (75 + i)::numeric(10, 2), false,
    now() - make_interval(days => 5 + (i % 7), hours => 22),
    now() - make_interval(days => 5 + (i % 7), hours => 12 - (i % 10)),
    now() - make_interval(days => 5 + (i % 7), hours => 11 - (i % 10))
FROM generate_series(1, 40) AS s(i);

-- EDGE CASE 6: clock skew. completed_at in the future. These fall outside a
-- backtest window that ends at now(), and must not corrupt the max-day stat.
INSERT INTO orders (
    customer_id, worker_id, service_id, status,
    total_amount, is_test, created_at, completed_at, paid_at
)
SELECT
    1 + (i % 200), 1 + (i % 60), 1 + (i % 4), 4::smallint,
    (500 + i)::numeric(10, 2), false,
    now() - make_interval(hours => 2),
    now() + make_interval(days => i),              -- <- future
    now() + make_interval(days => i)
FROM generate_series(1, 5) AS s(i);
