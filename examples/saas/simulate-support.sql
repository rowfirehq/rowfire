-- A few minutes of "live" traffic on the MySQL support desk.
--
-- The counterpart of simulate.sql for the second data source:
--
--   docker compose exec -T mysql mysql -uroot -prowfire rowfire_support < examples/saas/simulate-support.sql
--
-- Two urgent tickets nobody has answered, and one that is spam -- which the
-- trigger's `is_spam = 0` keeps out of #support.
INSERT INTO tickets (
    account_id, agent_id, subject, requester_email, priority, status, channel,
    created_at, first_response_at, csat, is_spam, tags
) VALUES
    (7, NULL, 'Checkout is down for all our users', 'ops@halcyonfreight.example',
     'urgent', 'open', 'email', UTC_TIMESTAMP(), NULL, NULL, 0, JSON_ARRAY('outage')),
    (12, NULL, 'Data export missing yesterday''s rows', 'rosa@brightorbit.example',
     'urgent', 'open', 'chat', UTC_TIMESTAMP(), NULL, NULL, 0, JSON_ARRAY('export')),
    (3, NULL, 'URGENT!!! claim your prize', 'win@spam.example',
     'urgent', 'open', 'web', UTC_TIMESTAMP(), NULL, NULL, 1, JSON_ARRAY());
