-- The SELECT-only account Rowfire is given. The session Rowfire opens is
-- read-only as well; tests assert a write fails either way.
CREATE USER IF NOT EXISTS 'rowfire_ro'@'%' IDENTIFIED BY 'rowfire_ro';
GRANT SELECT ON rowfire_support.* TO 'rowfire_ro'@'%';
