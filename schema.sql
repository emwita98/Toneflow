-- AquaPerk · Water Refill Loyalty Programme
-- PostgreSQL schema (idempotent: safe to run more than once)

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(50)  NOT NULL UNIQUE,
    full_name     VARCHAR(100) NOT NULL,
    password_hash TEXT         NOT NULL,
    role          VARCHAR(10)  NOT NULL CHECK (role IN ('admin', 'manager')),
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clients (
    id                    SERIAL PRIMARY KEY,
    name                  VARCHAR(100) NOT NULL,
    phone                 VARCHAR(20)  NOT NULL UNIQUE,      -- digits only, incl. country code
    qr_token              VARCHAR(64)  NOT NULL UNIQUE,      -- secret behind the client's QR code
    points                INTEGER      NOT NULL DEFAULT 0 CHECK (points BETWEEN 0 AND 10),
    total_refills         INTEGER      NOT NULL DEFAULT 0,
    free_refills_redeemed INTEGER      NOT NULL DEFAULT 0,
    last_refill_at        TIMESTAMPTZ,
    created_by            INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Every refill ever served. Rows survive client / staff deletion so that
-- sales history is never lost (client_id and served_by simply become NULL).
CREATE TABLE IF NOT EXISTS refills (
    id          BIGSERIAL PRIMARY KEY,
    client_id   INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    client_name VARCHAR(100),                                -- snapshot ("Walk-in" for non-members)
    served_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_free     BOOLEAN       NOT NULL DEFAULT FALSE,
    amount      NUMERIC(10,2) NOT NULL DEFAULT 0,            -- 0 for free refills
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_refills_created_at ON refills (created_at);
CREATE INDEX IF NOT EXISTS idx_refills_client     ON refills (client_id);
CREATE INDEX IF NOT EXISTS idx_clients_name       ON clients (lower(name));

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO settings (key, value) VALUES
    ('business_name', 'AquaPerk'),
    ('price',         '20.00'),
    ('currency',      'KES')
ON CONFLICT (key) DO NOTHING;
