-- Trade-In Widget — database schema (SQLite)
-- Dealers + product access live in the shared platform DB; only leads are local.

-- One row per trade-in lead. Created after step 2 (contact), updated after
-- step 3 (condition). Two ADF emails are sent: one at each stage.
CREATE TABLE IF NOT EXISTS trade_leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dealer_id       TEXT    NOT NULL,               -- platform dealer_id
    serial          TEXT,                           -- official offer serial number (recall key)

    -- trade-in vehicle (step 1)
    vehicle_year    TEXT,
    vehicle_make    TEXT,
    vehicle_model   TEXT,
    vehicle_trim    TEXT,

    -- customer (step 2)
    first_name      TEXT,
    last_name       TEXT,
    email           TEXT,
    phone           TEXT,
    tc_agreed       INTEGER NOT NULL DEFAULT 0,
    tc_agreed_at    TEXT,

    -- condition (step 3)
    num_keys             TEXT,   -- 1 | 2 | 3+
    unrepaired_damage    TEXT,   -- Y | N
    engine_light         TEXT,   -- Y | N
    airbag_light         TEXT,   -- Y | N
    brake_light          TEXT,   -- Y | N
    aftermarket_exhaust  TEXT,   -- Y | N
    aftermarket_engine   TEXT,   -- Y | N
    aftermarket_stereo   TEXT,   -- Y | N
    own_or_lease         TEXT,   -- Own | Lease
    miles                TEXT,
    ownership_status     TEXT,   -- loan | lease | title
    loan_balance         TEXT,   -- when ownership_status = loan
    lease_months_remaining TEXT, -- when ownership_status = lease (1-36 | 37+)

    -- computed valuation (filled at step 3)
    value_estimate  REAL,
    value_low       REAL,
    value_high      REAL,
    value_source    TEXT,

    -- validation + delivery
    email_verdict   TEXT,
    email_score     REAL,
    adf_xml         TEXT,                            -- latest ADF payload generated
    stage           TEXT    NOT NULL DEFAULT 'contact', -- contact | complete
    email1_status   TEXT    NOT NULL DEFAULT 'pending', -- after step 2
    email1_detail   TEXT,
    email2_status   TEXT,                            -- after step 3
    email2_detail   TEXT,

    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),

    -- At minimum, a lead must have an email OR a phone.
    CHECK (
        (email IS NOT NULL AND email <> '')
        OR (phone IS NOT NULL AND phone <> '')
    )
);

CREATE INDEX IF NOT EXISTS idx_trade_leads_dealer ON trade_leads(dealer_id);
CREATE INDEX IF NOT EXISTS idx_trade_leads_created ON trade_leads(created_at);

-- Local cache of a representative squish VIN per year/make/model, so valuation
-- never has to scan the huge vin_data1 table at request time. Populated in bulk
-- by build_squish_cache() (one grouped scan) and lazily on cache misses.
CREATE TABLE IF NOT EXISTS squish_map (
    year   INTEGER NOT NULL,
    make   TEXT    NOT NULL,
    model  TEXT    NOT NULL,
    s8     TEXT    NOT NULL,
    s2     TEXT    NOT NULL,
    PRIMARY KEY (year, make, model)
);

-- Local cache of the SET of distinct first-8 VIN prefixes per make/model. A
-- single make/model spans many VDS codes (body/engine/trim), so matching comps
-- needs all of them, not one representative. Populated by build_vin8_map() (one
-- DISTINCT scan) and lazily on misses. Used for retail comps + "similar for
-- sale" count, both matched on these prefixes.
CREATE TABLE IF NOT EXISTS vin8_map (
    make   TEXT NOT NULL,
    model  TEXT NOT NULL,
    s8     TEXT NOT NULL,
    PRIMARY KEY (make, model, s8)
);
CREATE INDEX IF NOT EXISTS idx_vin8_make_model ON vin8_map(make, model);

-- Local cache of tbl_inventory counts per first-8 VIN prefix, so the
-- "Similar Vehicles for Sale" metric is an instant local sum instead of a
-- slow 100-way OR scan at request time. Refreshed by build_inv_count_cache()
-- (one GROUP BY scan of tbl_inventory).
CREATE TABLE IF NOT EXISTS inv_prefix_count (
    s8   TEXT PRIMARY KEY,
    cnt  INTEGER NOT NULL
);
