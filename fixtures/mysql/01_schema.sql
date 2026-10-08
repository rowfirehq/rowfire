-- MySQL fixture: a small support desk.
--
-- The second data source in the test suite and the demo. Shaped like a real
-- application database, with the awkwardness a backtest has to cope with:
-- DATETIME columns with no zone, a tinyint(1) boolean, an ENUM, a JSON
-- column, and a response time that is sometimes null.
--
-- Loaded by the official mysql image from /docker-entrypoint-initdb.d, into
-- the database named by MYSQL_DATABASE (rowfire_support).

CREATE TABLE agents (
    id    INT PRIMARY KEY AUTO_INCREMENT,
    name  VARCHAR(100) NOT NULL,
    email VARCHAR(200) NOT NULL
);

CREATE TABLE tickets (
    id                INT PRIMARY KEY AUTO_INCREMENT,
    account_id        INT NOT NULL,
    agent_id          INT NULL,
    subject           VARCHAR(200) NOT NULL,
    requester_email   VARCHAR(200) NOT NULL,
    priority          ENUM('low', 'normal', 'high', 'urgent') NOT NULL DEFAULT 'normal',
    status            VARCHAR(20) NOT NULL,
    channel           VARCHAR(20) NOT NULL,
    -- Stored as UTC wall-clock time, which is what Rowfire assumes of a
    -- DATETIME: the column itself carries no zone.
    created_at        DATETIME NOT NULL,
    first_response_at DATETIME NULL,
    csat              TINYINT NULL,
    is_spam           TINYINT(1) NOT NULL DEFAULT 0,
    tags              JSON NULL,
    CONSTRAINT tickets_agent_fk FOREIGN KEY (agent_id) REFERENCES agents (id)
);

CREATE INDEX tickets_created_at_idx ON tickets (created_at);
