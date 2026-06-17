-- Credit Estimator — database schema (SQLite)
-- Dealers + product access (CREDIT_EST) and the APR/affordability settings live
-- in the shared platform DB; only the captured leads are stored here.

-- One row per lead. The row is created after page 1 (contact), then updated
-- after page 2 (the FICO quiz + deal inputs) with the computed estimate.
CREATE TABLE IF NOT EXISTS credit_leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dealer_id       TEXT    NOT NULL,                  -- platform dealer_id

    -- contact (page 1)
    first_name      TEXT,
    last_name       TEXT,
    email           TEXT,
    phone           TEXT,
    comments        TEXT,
    vehicle_year    TEXT,
    vehicle_make    TEXT,
    vehicle_model   TEXT,
    tc_agreed       INTEGER NOT NULL DEFAULT 0,
    tc_agreed_at    TEXT,
    email_verdict   TEXT,                              -- SendGrid validation verdict
    email_score     REAL,                              -- SendGrid validation score (0..1)

    -- FICO-style quiz answers (page 2)
    payment_history TEXT,
    utilization     TEXT,
    credit_age      TEXT,
    derogatory      TEXT,

    -- deal inputs (page 2)
    vehicle_condition TEXT,                            -- new | used
    vehicle_price   REAL,
    down_payment    REAL,
    trade_value     REAL,
    term_months     INTEGER,
    annual_income   REAL,

    -- computed estimate
    est_score         INTEGER,
    range_low         INTEGER,
    range_high        INTEGER,
    tier              TEXT,
    apr               REAL,
    apr_low           REAL,
    apr_high          REAL,
    amount_financed   REAL,
    monthly_payment   REAL,
    max_vehicle_price REAL,
    approval          TEXT,

    -- delivery
    adf_xml         TEXT,
    stage           TEXT    NOT NULL DEFAULT 'contact',  -- contact | complete
    email1_status   TEXT,                              -- ADF after page 1
    email1_detail   TEXT,
    email2_status   TEXT,                              -- ADF after page 2 (with estimate)
    email2_detail   TEXT,
    source          TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),

    CHECK (
        (email IS NOT NULL AND email <> '')
        OR (phone IS NOT NULL AND phone <> '')
    )
);

CREATE INDEX IF NOT EXISTS idx_credit_leads_dealer ON credit_leads(dealer_id);
CREATE INDEX IF NOT EXISTS idx_credit_leads_created ON credit_leads(created_at);
