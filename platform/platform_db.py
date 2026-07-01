"""Shared platform data access: dealers + product access grants + settings.

Backed by the SQL Server `dlrPro` database (see dlrpro_db.py). Imported by both
the dealer-leads and trade-in/credit apps (each adds this directory to sys.path).
Public function names/signatures are unchanged from the old SQLite version.
"""

from datetime import date

import dlrpro_db as dlr
from dlrpro_db import NOW

# Canonical product codes.
PRODUCT_LEAD_FORM = "LEAD_FORM"
PRODUCT_TRADE_IN = "TRADE_IN"
PRODUCT_CREDIT_EST = "CREDIT_EST"
PRODUCT_CREDIT_PIPELINE = "CREDIT_PIPELINE"
# CDP (cdp.dlrpro.com) modules — granted per dealer here, enforced by the CDP app.
PRODUCT_CDP_VMS = "CDP_VMS"
PRODUCT_CDP_CRM = "CDP_CRM"
PRODUCT_CDP_PROSPECTING = "CDP_PROSPECTING"

# The 4th tuple element is the product's default ADF "source" — the primary
# (sequence=1) source name emitted on every outbound ADF/XML <id> for leads of
# this product. It is admin-editable per product (Products page); init_db only
# seeds it and backfills NULLs, so edits persist across restarts.
DEFAULT_ADF_SOURCE = "RMA Data Plus"
DEFAULT_PRODUCTS = [
    (PRODUCT_LEAD_FORM, "Dealer Lead Form", "Customer lead-capture form with ADF/XML delivery.", DEFAULT_ADF_SOURCE),
    (PRODUCT_TRADE_IN, "Trade-In Widget", "Multi-step trade-in appraisal capture with ADF/XML delivery.", DEFAULT_ADF_SOURCE),
    (PRODUCT_CREDIT_EST, "Credit Estimator", "Lead form + FICO-range estimate, APR/payment, and affordability.", DEFAULT_ADF_SOURCE),
    (PRODUCT_CREDIT_PIPELINE, "Credit Pipeline", "Credit lead pipeline with ADF/XML delivery and source lineage.", "Credit Pipeline"),
    (PRODUCT_CDP_VMS, "CDP · Vehicle Management", "Appraisal, VIN normalization, valuation, photos & inventory.", DEFAULT_ADF_SOURCE),
    (PRODUCT_CDP_CRM, "CDP · CRM", "Customer relationship management (coming soon).", DEFAULT_ADF_SOURCE),
    (PRODUCT_CDP_PROSPECTING, "CDP · Prospecting", "Data mining & opportunity lists (coming soon).", DEFAULT_ADF_SOURCE),
]


def init_db():
    """Ensure the default products exist (tables themselves are created by the
    one-time migrate_to_dlrpro.py). Idempotent."""
    _ensure_pipeline_tables()
    _ensure_crm_tables()
    _ensure_leadsource_tables()
    # Self-healing: the `source` column was added after the initial migration.
    dlr.execute("IF COL_LENGTH('products', 'source') IS NULL "
                "ALTER TABLE products ADD source NVARCHAR(200) NULL")
    for code, name, desc, source in DEFAULT_PRODUCTS:
        p = {"c": code, "n": name, "d": desc, "s": source}
        if dlr.one("SELECT 1 AS x FROM products WHERE product_code=%(c)s", p):
            # Keep name/desc in sync with code, but never clobber an admin-edited
            # source — only backfill it when it's still NULL.
            dlr.execute("UPDATE products SET product_name=%(n)s, description=%(d)s, "
                        "source=COALESCE(source, %(s)s) WHERE product_code=%(c)s", p)
        else:
            dlr.execute("INSERT INTO products (product_code, product_name, description, source) "
                        "VALUES (%(c)s, %(n)s, %(d)s, %(s)s)", p)


# ----- CRM types -----
#
# Master list of dealer CRM systems; a dealer references one via dealers.crm_type_id.

def _ensure_crm_tables():
    """Create the crm_types table + the dealers.crm_type_id column if missing."""
    dlr.execute(
        "IF OBJECT_ID(N'dbo.crm_types','U') IS NULL "
        "CREATE TABLE dbo.crm_types ("
        " id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,"
        " name NVARCHAR(120) NOT NULL,"
        " created_at NVARCHAR(32) NULL,"
        " CONSTRAINT UQ_crm_types_name UNIQUE (name))")
    dlr.execute(
        "IF COL_LENGTH('dbo.dealers','crm_type_id') IS NULL "
        "ALTER TABLE dbo.dealers ADD crm_type_id INT NULL")


def list_crm_types():
    return dlr.query("SELECT * FROM crm_types ORDER BY name")


def get_crm_type(crm_id):
    return dlr.one("SELECT * FROM crm_types WHERE id=%(i)s", {"i": crm_id})


def add_crm_type(name):
    dlr.execute(f"INSERT INTO crm_types (name, created_at) VALUES (%(n)s, {NOW})",
                {"n": name})


def update_crm_type(crm_id, name):
    dlr.execute("UPDATE crm_types SET name=%(n)s WHERE id=%(i)s",
                {"n": name, "i": crm_id})


def delete_crm_type(crm_id):
    dlr.execute("DELETE FROM crm_types WHERE id=%(i)s", {"i": crm_id})


def crm_name_for(dealer):
    """CRM system name for a dealer dict (resolves crm_type_id), or None.
    Never raises — returns None on any error (used during ADF generation)."""
    try:
        cid = (dealer or {}).get("crm_type_id")
        if not cid:
            return None
        row = get_crm_type(cid)
        return row["name"] if row else None
    except Exception:
        return None


# ----- lead sources -----
#
# Master list of lead sources; a dealer references one via dealers.lead_source_id.
# The dealer's lead source is the primary (sequence 1) ADF <id> source. Dealers
# with no selection fall back to DEFAULT_LEAD_SOURCE.

DEFAULT_LEAD_SOURCE = "Credit Pipeline"
DEFAULT_LEAD_SOURCES = ("Credit Pipeline", "G4 Media")


def _ensure_leadsource_tables():
    """Create lead_sources + the dealers.lead_source_id column, and seed the
    default sources. Idempotent."""
    dlr.execute(
        "IF OBJECT_ID(N'dbo.lead_sources','U') IS NULL "
        "CREATE TABLE dbo.lead_sources ("
        " id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,"
        " name NVARCHAR(120) NOT NULL,"
        " created_at NVARCHAR(32) NULL,"
        " CONSTRAINT UQ_lead_sources_name UNIQUE (name))")
    dlr.execute(
        "IF COL_LENGTH('dbo.dealers','lead_source_id') IS NULL "
        "ALTER TABLE dbo.dealers ADD lead_source_id INT NULL")
    for name in DEFAULT_LEAD_SOURCES:
        dlr.execute(
            "IF NOT EXISTS (SELECT 1 FROM lead_sources WHERE name=%(n)s) "
            f"INSERT INTO lead_sources (name, created_at) VALUES (%(n)s, {NOW})",
            {"n": name})


def list_lead_sources():
    return dlr.query("SELECT * FROM lead_sources ORDER BY name")


def get_lead_source(source_id):
    return dlr.one("SELECT * FROM lead_sources WHERE id=%(i)s", {"i": source_id})


def add_lead_source(name):
    dlr.execute(f"INSERT INTO lead_sources (name, created_at) VALUES (%(n)s, {NOW})",
                {"n": name})


def update_lead_source(source_id, name):
    dlr.execute("UPDATE lead_sources SET name=%(n)s WHERE id=%(i)s",
                {"n": name, "i": source_id})


def delete_lead_source(source_id):
    dlr.execute("DELETE FROM lead_sources WHERE id=%(i)s", {"i": source_id})


def lead_source_for(dealer):
    """Primary ADF source for a dealer (resolves lead_source_id), defaulting to
    Credit Pipeline. Never raises (used during ADF generation)."""
    try:
        sid = (dealer or {}).get("lead_source_id")
        if sid:
            row = get_lead_source(sid)
            if row and row.get("name"):
                return row["name"]
    except Exception:
        pass
    return DEFAULT_LEAD_SOURCE


# ----- dealers -----

def get_dealer(dealer_id):
    return dlr.one("SELECT * FROM dealers WHERE dealer_id=%(d)s", {"d": dealer_id})


def list_dealers():
    return dlr.query("SELECT * FROM dealers ORDER BY dealer_name")


def upsert_dealer(data):
    fields = ("dealer_id", "dealer_name", "address", "city", "state",
              "zip", "phone", "lead_email_address", "banner_url",
              "crm_type_id", "lead_source_id")
    v = {k: (data.get(k) if data.get(k) not in ("", None) else None) for k in fields}
    if dlr.one("SELECT 1 AS x FROM dealers WHERE dealer_id=%(dealer_id)s", v):
        dlr.execute(
            "UPDATE dealers SET dealer_name=%(dealer_name)s, address=%(address)s, "
            "city=%(city)s, state=%(state)s, zip=%(zip)s, phone=%(phone)s, "
            "lead_email_address=%(lead_email_address)s, banner_url=%(banner_url)s, "
            "crm_type_id=%(crm_type_id)s, lead_source_id=%(lead_source_id)s, "
            f"updated_at={NOW} WHERE dealer_id=%(dealer_id)s", v)
    else:
        dlr.execute(
            "INSERT INTO dealers (dealer_id, dealer_name, address, city, state, zip, "
            "phone, lead_email_address, banner_url, crm_type_id, lead_source_id) "
            "VALUES (%(dealer_id)s, %(dealer_name)s, %(address)s, %(city)s, %(state)s, "
            "%(zip)s, %(phone)s, %(lead_email_address)s, %(banner_url)s, "
            "%(crm_type_id)s, %(lead_source_id)s)", v)


# ----- products -----

def list_products():
    return dlr.query("SELECT * FROM products ORDER BY product_name")


def get_product(product_code):
    """One product row (incl. its ADF `source`), or None."""
    return dlr.one("SELECT * FROM products WHERE product_code=%(c)s", {"c": product_code})


def update_product_source(product_code, source):
    """Set a product's ADF primary source (sequence=1 source on outbound <id>)."""
    dlr.execute("UPDATE products SET source=%(s)s WHERE product_code=%(c)s",
                {"s": (source or None), "c": product_code})


# ----- grants -----

def list_grants(dealer_id=None):
    if dealer_id:
        return dlr.query("SELECT * FROM dealer_products WHERE dealer_id=%(d)s "
                         "ORDER BY product_code", {"d": dealer_id})
    return dlr.query("SELECT * FROM dealer_products ORDER BY dealer_id, product_code")


def upsert_grant(data):
    fields = ("dealer_id", "product_code", "valid_from", "valid_to",
              "monthly_price", "per_lead_price")
    v = {k: (data.get(k) if data.get(k) not in ("",) else None) for k in fields}
    if dlr.one("SELECT 1 AS x FROM dealer_products WHERE dealer_id=%(dealer_id)s "
               "AND product_code=%(product_code)s", v):
        dlr.execute(
            "UPDATE dealer_products SET valid_from=%(valid_from)s, valid_to=%(valid_to)s, "
            "monthly_price=%(monthly_price)s, per_lead_price=%(per_lead_price)s, "
            f"updated_at={NOW} WHERE dealer_id=%(dealer_id)s AND product_code=%(product_code)s", v)
    else:
        dlr.execute(
            "INSERT INTO dealer_products (dealer_id, product_code, valid_from, valid_to, "
            "monthly_price, per_lead_price) VALUES (%(dealer_id)s, %(product_code)s, "
            "%(valid_from)s, %(valid_to)s, %(monthly_price)s, %(per_lead_price)s)", v)


def delete_grant(grant_id):
    dlr.execute("DELETE FROM dealer_products WHERE id=%(id)s", {"id": grant_id})


def get_active_grant(dealer_id, product_code, on_date=None):
    """Grant row if active on the date (default today), else None. NULL bounds =
    unbounded; ISO 'YYYY-MM-DD' strings compare correctly."""
    today = on_date or date.today().isoformat()
    return dlr.one(
        "SELECT * FROM dealer_products WHERE dealer_id=%(d)s AND product_code=%(p)s "
        "AND (valid_from IS NULL OR valid_from<=%(t)s) "
        "AND (valid_to IS NULL OR valid_to>=%(t)s)",
        {"d": dealer_id, "p": product_code, "t": today})


def dealer_has_product(dealer_id, product_code, on_date=None):
    return get_active_grant(dealer_id, product_code, on_date) is not None


def dealer_has_any_product(dealer_id, product_codes, on_date=None):
    """True if the dealer has an active grant for ANY of the given product codes.
    Used where one app serves more than one product (e.g. the credit app serves
    both Credit Estimator and Credit Pipeline)."""
    return any(dealer_has_product(dealer_id, c, on_date) for c in product_codes)


def count_active_grants(product_code, on_date=None):
    """Number of dealers with an active grant for the product (default today)."""
    today = on_date or date.today().isoformat()
    row = dlr.one(
        "SELECT COUNT(DISTINCT dealer_id) AS c FROM dealer_products "
        "WHERE product_code=%(p)s "
        "AND (valid_from IS NULL OR valid_from<=%(t)s) "
        "AND (valid_to IS NULL OR valid_to>=%(t)s)",
        {"p": product_code, "t": today})
    return row["c"] if row else 0


# ----- valuation settings -----

CONDITION_ADJ_FIELDS = (
    "adj_keys_1", "adj_keys_3plus", "adj_unrepaired_damage", "adj_engine_light",
    "adj_airbag_light", "adj_brake_light", "adj_aftermarket_exhaust",
    "adj_aftermarket_engine", "adj_aftermarket_stereo",
)

RECOMMENDED = {
    "dollar": {
        "range_spread": 500, "mileage_rate": 0.12,
        "adj_keys_1": -250, "adj_keys_3plus": 0, "adj_unrepaired_damage": -1000,
        "adj_engine_light": -1500, "adj_airbag_light": -600, "adj_brake_light": -400,
        "adj_aftermarket_exhaust": -300, "adj_aftermarket_engine": -750,
        "adj_aftermarket_stereo": -200,
    },
    "percent": {
        "range_spread": 3, "mileage_rate": 0.12,
        "adj_keys_1": -1, "adj_keys_3plus": 0, "adj_unrepaired_damage": -5,
        "adj_engine_light": -7, "adj_airbag_light": -3, "adj_brake_light": -2,
        "adj_aftermarket_exhaust": -2, "adj_aftermarket_engine": -4,
        "adj_aftermarket_stereo": -1.5,
    },
}

ALL_SETTING_FIELDS = ("base_source", "adjustment_unit", "range_spread",
                      "mileage_rate") + CONDITION_ADJ_FIELDS


def recommended_settings(unit):
    return dict(RECOMMENDED.get(unit, RECOMMENDED["dollar"]))


def get_valuation_settings(dealer_id):
    row = dlr.one("SELECT * FROM dealer_valuation_settings WHERE dealer_id=%(d)s",
                  {"d": dealer_id})
    if row:
        return row
    defaults = {"dealer_id": dealer_id, "base_source": "retail",
                "adjustment_unit": "dollar"}
    defaults.update(RECOMMENDED["dollar"])
    return defaults


def _upsert_settings(table, fields, data):
    v = {"dealer_id": data["dealer_id"]}
    for f in fields:
        v[f] = data.get(f)
    if dlr.one(f"SELECT 1 AS x FROM {table} WHERE dealer_id=%(dealer_id)s", v):
        sets = ", ".join(f"{f}=%({f})s" for f in fields)
        dlr.execute(f"UPDATE {table} SET {sets}, updated_at={NOW} "
                    "WHERE dealer_id=%(dealer_id)s", v)
    else:
        cols = ", ".join(("dealer_id",) + tuple(fields))
        ph = ", ".join(f"%({c})s" for c in ("dealer_id",) + tuple(fields))
        dlr.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", v)


def upsert_valuation_settings(data):
    _upsert_settings("dealer_valuation_settings", ALL_SETTING_FIELDS, data)


# ----- credit estimator settings -----

CREDIT_APR_FIELDS = (
    "apr_exceptional", "apr_very_good", "apr_good", "apr_fair", "apr_poor",
)
CREDIT_SETTING_FIELDS = CREDIT_APR_FIELDS + (
    "new_apr_delta", "apr_spread", "max_term_months", "max_payment_pct",
)
RECOMMENDED_CREDIT = {
    "apr_exceptional": 6.49, "apr_very_good": 7.49, "apr_good": 9.99,
    "apr_fair": 14.49, "apr_poor": 19.99,
    "new_apr_delta": -1.0, "apr_spread": 1.0,
    "max_term_months": 72, "max_payment_pct": 15.0,
}


def recommended_credit_settings():
    return dict(RECOMMENDED_CREDIT)


def get_credit_settings(dealer_id):
    row = dlr.one("SELECT * FROM dealer_credit_settings WHERE dealer_id=%(d)s",
                  {"d": dealer_id})
    if row:
        return row
    defaults = {"dealer_id": dealer_id}
    defaults.update(RECOMMENDED_CREDIT)
    return defaults


def upsert_credit_settings(data):
    _upsert_settings("dealer_credit_settings", CREDIT_SETTING_FIELDS, data)


# ----- platform settings + Credit Pipeline lead flow -----
#
# platform_settings (key/value) holds the global Credit Pipeline on/off switch.
# The `sent` table is the send-once ledger — one row per (dealer, match result):
# id, result_id, dealer_id (= dealers.id), created. Kept lean; it grows large.

def _ensure_pipeline_tables():
    """Create the settings table + the `sent` ledger if missing. Idempotent."""
    dlr.execute(
        "IF OBJECT_ID(N'dbo.platform_settings','U') IS NULL "
        "CREATE TABLE dbo.platform_settings ("
        " setting_key NVARCHAR(64) NOT NULL PRIMARY KEY,"
        " setting_value NVARCHAR(MAX) NULL,"
        " updated_at NVARCHAR(32) NULL)")
    dlr.execute(
        "IF OBJECT_ID(N'dbo.sent','U') IS NULL "
        "CREATE TABLE dbo.sent ("
        " id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,"
        " result_id BIGINT NULL,"
        " dealer_id BIGINT NULL,"
        " created SMALLDATETIME NULL)")


def get_setting(key, default=None):
    row = dlr.one("SELECT setting_value AS v FROM platform_settings WHERE setting_key=%(k)s",
                  {"k": key})
    return row["v"] if row else default


def set_setting(key, value):
    v = {"k": key, "v": value}
    if dlr.one("SELECT 1 AS x FROM platform_settings WHERE setting_key=%(k)s", v):
        dlr.execute(f"UPDATE platform_settings SET setting_value=%(v)s, updated_at={NOW} "
                    "WHERE setting_key=%(k)s", v)
    else:
        dlr.execute("INSERT INTO platform_settings (setting_key, setting_value, updated_at) "
                    f"VALUES (%(k)s, %(v)s, {NOW})", v)


PIPELINE_FLOW_KEY = "credit_pipeline_flow_enabled"


def get_pipeline_flow():
    """Global Credit Pipeline lead-flow switch (default OFF)."""
    return get_setting(PIPELINE_FLOW_KEY, "0") == "1"


def set_pipeline_flow(enabled):
    set_setting(PIPELINE_FLOW_KEY, "1" if enabled else "0")


# ----- Credit Pipeline sent ledger (dbo.sent) -----

def is_sent(dealers_id, result_id):
    """True if this (dealer, match result) is already in the sent ledger."""
    return dlr.one("SELECT TOP 1 id FROM dbo.sent WHERE dealer_id=%(d)s AND result_id=%(r)s",
                   {"d": int(dealers_id), "r": int(result_id)}) is not None


def record_sent(dealers_id, result_id):
    """Mark a trigger lead as sent — one row in dbo.sent (dealers.id, result_id,
    timestamp). Idempotent: won't duplicate an existing (dealer, result)."""
    dlr.execute(
        "IF NOT EXISTS (SELECT 1 FROM dbo.sent WHERE dealer_id=%(d)s AND result_id=%(r)s) "
        "INSERT INTO dbo.sent (result_id, dealer_id, created) VALUES (%(r)s, %(d)s, GETDATE())",
        {"d": int(dealers_id), "r": int(result_id)})
