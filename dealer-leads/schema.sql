-- Dealer Lead Form — database schema (SQLite)
-- Dealers + product access live in the shared platform DB; only leads are local.

-- One row per lead captured by the customer data-collection page.
CREATE TABLE IF NOT EXISTS leads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dealer_id     TEXT    NOT NULL,                  -- platform dealer_id
    first_name    TEXT,
    last_name     TEXT,
    email         TEXT,
    phone         TEXT,
    comments      TEXT,
    -- vehicle of interest (all optional)
    vehicle_year  TEXT,
    vehicle_make  TEXT,
    vehicle_model TEXT,
    source        TEXT,                            -- e.g. page URL / referrer
    adf_xml       TEXT,                            -- the ADF/XML payload that was generated
    email_status  TEXT    NOT NULL DEFAULT 'pending', -- pending | sent | failed
    email_detail  TEXT,                            -- provider response / error detail
    email_verdict TEXT,                            -- SendGrid validation: Valid|Risky|Invalid|Unknown
    email_score   REAL,                            -- SendGrid validation score (0..1)
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),

    -- At minimum, a lead must have an email OR a phone.
    CHECK (
        (email IS NOT NULL AND email <> '')
        OR (phone IS NOT NULL AND phone <> '')
    )
);

CREATE INDEX IF NOT EXISTS idx_leads_dealer ON leads(dealer_id);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);
