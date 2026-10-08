-- Deterministic: arithmetic over a recursive sequence, no RAND(). Times are
-- relative to UTC_TIMESTAMP() so a 120-day window always covers the data.
--
-- What the tests count on:
--   * 480 tickets, four a day for 120 days
--   * every 7th is urgent; every 20th is spam (is_spam = 1)
--   * every 9th has no first response yet (first_response_at IS NULL)
--   * every 5th rated, csat between 1 and 5
--   * subjects carry a literal `%` in "Refund 100%" tickets, every 11th

INSERT INTO agents (name, email) VALUES
    ('Ana Adams',  'ana@support.example'),
    ('Ben Brooks', 'ben@support.example'),
    ('Chloe Chen', 'chloe@support.example');

SET SESSION cte_max_recursion_depth = 1000;

INSERT INTO tickets (
    account_id, agent_id, subject, requester_email, priority, status, channel,
    created_at, first_response_at, csat, is_spam, tags
)
WITH RECURSIVE seq (n) AS (
    SELECT 1
    UNION ALL
    SELECT n + 1 FROM seq WHERE n < 480
)
SELECT
    1 + (n * 13) % 60,
    CASE WHEN n % 9 = 0 THEN NULL ELSE 1 + n % 3 END,
    CASE
        WHEN n % 11 = 0 THEN 'Refund 100% of last invoice'
        WHEN n % 3 = 0  THEN 'Export times out'
        WHEN n % 3 = 1  THEN 'Cannot log in with SSO'
        ELSE 'Question about billing'
    END,
    CONCAT('user', n % 40, '@customer.example'),
    CASE WHEN n % 7 = 0 THEN 'urgent' WHEN n % 3 = 0 THEN 'high' ELSE 'normal' END,
    CASE WHEN n % 4 = 0 THEN 'solved' ELSE 'open' END,
    CASE n % 3 WHEN 0 THEN 'email' WHEN 1 THEN 'chat' ELSE 'web' END,
    UTC_TIMESTAMP() - INTERVAL ((480 - n) DIV 4) DAY - INTERVAL (n % 4) * 5 HOUR,
    CASE
        WHEN n % 9 = 0 THEN NULL
        ELSE UTC_TIMESTAMP() - INTERVAL ((480 - n) DIV 4) DAY - INTERVAL (n % 4) * 5 HOUR
             + INTERVAL 30 + (n % 6) * 20 MINUTE
    END,
    CASE WHEN n % 5 = 0 THEN 1 + n % 5 + (n DIV 5) % 5 ELSE NULL END,
    CASE WHEN n % 20 = 0 THEN 1 ELSE 0 END,
    JSON_ARRAY(CASE n % 3 WHEN 0 THEN 'export' WHEN 1 THEN 'sso' ELSE 'billing' END)
FROM seq;
