-- Shared platform database — dealers + product access grants.
-- Used by BOTH the dealer-leads app and the trade-in app. Each app stores its
-- own leads locally; dealers and entitlements live here, in one place.

-- Dealer master.
CREATE TABLE IF NOT EXISTS dealers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dealer_id           TEXT    NOT NULL UNIQUE,   -- business identifier used in product URLs
    dealer_name         TEXT    NOT NULL,
    address             TEXT,
    city                TEXT,
    state               TEXT,
    zip                 TEXT,
    phone               TEXT,
    lead_email_address  TEXT    NOT NULL,          -- ADF/XML leads are delivered here
    banner_url          TEXT,                      -- optional banner shown on the dealer's forms
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Product catalog: the products this platform offers.
CREATE TABLE IF NOT EXISTS products (
    product_code  TEXT PRIMARY KEY,                -- e.g. LEAD_FORM, TRADE_IN
    product_name  TEXT NOT NULL,
    description   TEXT
);

-- Product access grants: which dealer has which product, for what window, at
-- what price. A grant is "active" when today is within [valid_from, valid_to]
-- (either bound may be NULL = unbounded).
CREATE TABLE IF NOT EXISTS dealer_products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dealer_id       TEXT NOT NULL REFERENCES dealers(dealer_id),
    product_code    TEXT NOT NULL REFERENCES products(product_code),
    valid_from      TEXT,                          -- YYYY-MM-DD (NULL = no start bound)
    valid_to        TEXT,                          -- YYYY-MM-DD (NULL = open-ended)
    monthly_price   REAL,
    per_lead_price  REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (dealer_id, product_code)
);

CREATE INDEX IF NOT EXISTS idx_dealer_products_dealer ON dealer_products(dealer_id);

-- Per-dealer trade-in valuation configuration. base_source picks which comp set
-- anchors the value; adjustment_unit ('percent' | 'dollar') is the unit for the
-- per-condition adjustments AND the displayed range spread. Condition values are
-- signed amounts in that unit (negative = deduct). Reset restores recommendations.
CREATE TABLE IF NOT EXISTS dealer_valuation_settings (
    dealer_id            TEXT PRIMARY KEY REFERENCES dealers(dealer_id),
    base_source          TEXT NOT NULL DEFAULT 'retail',   -- retail | wholesale
    adjustment_unit      TEXT NOT NULL DEFAULT 'dollar',   -- dollar | percent
    range_spread         REAL NOT NULL DEFAULT 500,        -- +/- displayed range, in adjustment_unit
    mileage_rate         REAL NOT NULL DEFAULT 0.12,       -- $/mile fallback when comp-fit unavailable

    adj_keys_1           REAL NOT NULL DEFAULT -250,       -- only 1 key (2 = baseline)
    adj_keys_3plus       REAL NOT NULL DEFAULT 0,          -- 3+ keys
    adj_unrepaired_damage REAL NOT NULL DEFAULT -1000,
    adj_engine_light     REAL NOT NULL DEFAULT -1500,
    adj_airbag_light     REAL NOT NULL DEFAULT -600,
    adj_brake_light      REAL NOT NULL DEFAULT -400,
    adj_aftermarket_exhaust REAL NOT NULL DEFAULT -300,
    adj_aftermarket_engine  REAL NOT NULL DEFAULT -750,
    adj_aftermarket_stereo  REAL NOT NULL DEFAULT -200,

    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-dealer Credit Estimator configuration. Maps an estimated FICO tier to an
-- APR (used-vehicle base; new vehicles get new_apr_delta added), and sets the
-- affordability limits. The FICO-range scoring model is fixed in the app; only
-- these lender-facing rates/limits are dealer-tunable. Reset restores defaults.
CREATE TABLE IF NOT EXISTS dealer_credit_settings (
    dealer_id        TEXT PRIMARY KEY REFERENCES dealers(dealer_id),

    apr_exceptional  REAL NOT NULL DEFAULT 6.49,   -- 800-850
    apr_very_good    REAL NOT NULL DEFAULT 7.49,   -- 740-799
    apr_good         REAL NOT NULL DEFAULT 9.99,   -- 670-739
    apr_fair         REAL NOT NULL DEFAULT 14.49,  -- 580-669
    apr_poor         REAL NOT NULL DEFAULT 19.99,  -- 300-579

    new_apr_delta    REAL NOT NULL DEFAULT -1.0,   -- added to tier APR when vehicle is new
    apr_spread       REAL NOT NULL DEFAULT 1.0,    -- +/- pts shown around the estimated APR
    max_term_months  INTEGER NOT NULL DEFAULT 72,  -- longest term, for the affordability calc
    max_payment_pct  REAL NOT NULL DEFAULT 15.0,   -- % of gross monthly income toward the payment

    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
