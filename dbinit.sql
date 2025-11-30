DROP TABLE IF EXISTS domain CASCADE;

CREATE TABLE domain (
    id      SERIAL PRIMARY KEY,
    name    VARCHAR(100)
);

DROP TABLE IF EXISTS message;

CREATE TABLE message (
    id          BIGSERIAL PRIMARY KEY,
    level       INT,
    ts          BIGINT DEFAULT extract(epoch from now())::BIGINT,
    content     TEXT,
    domain_id   INT,

    CONSTRAINT message_domain_fk
        FOREIGN KEY (domain_id)
        REFERENCES domain(id)
        ON DELETE CASCADE
        ON UPDATE CASCADE
);


DROP TABLE IF EXISTS tg;

CREATE TABLE tg (
    id              SERIAL PRIMARY KEY,
    domain_id       INT,
    cid             INT,
    report_level    INT DEFAULT 30,
    base_level      INT,

    CONSTRAINT tg_domain_fk
        FOREIGN KEY (domain_id)
        REFERENCES domain(id)
        ON DELETE CASCADE
        ON UPDATE CASCADE
);
