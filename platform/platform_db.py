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


# ----- dealers -----

def get_dealer(dealer_id):
    return dlr.one("SELECT * FROM dealers WHERE dealer_id=%(d)s", {"d": dealer_id})


def list_dealers():
    return dlr.query("SELECT * FROM dealers ORDER BY dealer_name")


def upsert_dealer(data):
    fields = ("dealer_id", "dealer_name", "address", "city", "state",
              "zip", "phone", "lead_email_address", "banner_url", "crm_type_id")
    v = {k: (data.get(k) if data.get(k) not in ("", None) else None) for k in fields}
    if dlr.one("SELECT 1 AS x FROM dealers WHERE dealer_id=%(dealer_id)s", v):
        dlr.execute(
            "UPDATE dealers SET dealer_name=%(dealer_name)s, address=%(address)s, "
            "city=%(city)s, state=%(state)s, zip=%(zip)s, phone=%(phone)s, "
            "lead_email_address=%(lead_email_address)s, banner_url=%(banner_url)s, "
            f"crm_type_id=%(crm_type_id)s, updated_at={NOW} WHERE dealer_id=%(dealer_id)s", v)
    else:
        dlr.execute(
            "INSERT INTO dealers (dealer_id, dealer_name, address, city, state, zip, "
            "phone, lead_email_address, banner_url, crm_type_id) VALUES (%(dealer_id)s, "
            "%(dealer_name)s, %(address)s, %(city)s, %(state)s, %(zip)s, %(phone)s, "
            "%(lead_email_address)s, %(banner_url)s, %(crm_type_id)s)", v)


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
# A small key/value settings table holds the global Credit Pipeline on/off
# switch and the poller's high-water mark. credit_pipeline_sent is the
# send-once ledger: one row per match_result.result_id ever processed.

def _ensure_pipeline_tables():
    """Create the settings + Credit Pipeline ledger tables if missing. Idempotent."""
    dlr.execute(
        "IF OBJECT_ID(N'dbo.platform_settings','U') IS NULL "
        "CREATE TABLE dbo.platform_settings ("
        " setting_key NVARCHAR(64) NOT NULL PRIMARY KEY,"
        " setting_value NVARCHAR(MAX) NULL,"
        " updated_at NVARCHAR(32) NULL)")
    dlr.execute(
        "IF OBJECT_ID(N'dbo.credit_pipeline_sent','U') IS NULL "
        "CREATE TABLE dbo.credit_pipeline_sent ("
        " result_id BIGINT NOT NULL PRIMARY KEY,"
        " retailer_name NVARCHAR(200) NULL,"
        " dealer_id NVARCHAR(64) NULL,"
        " lead_id INT NULL,"
        " subsource NVARCHAR(255) NULL,"
        " status NVARCHAR(32) NULL,"
        " detail NVARCHAR(MAX) NULL,"
        " sent_at NVARCHAR(32) NULL)")


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
PIPELINE_WATERMARK_KEY = "credit_pipeline_last_result_id"


def get_pipeline_flow():
    """Global Credit Pipeline lead-flow switch (default OFF)."""
    return get_setting(PIPELINE_FLOW_KEY, "0") == "1"


def set_pipeline_flow(enabled):
    set_setting(PIPELINE_FLOW_KEY, "1" if enabled else "0")


def get_pipeline_watermark():
    try:
        return int(get_setting(PIPELINE_WATERMARK_KEY, "0") or 0)
    except (TypeError, ValueError):
        return 0


def set_pipeline_watermark(result_id):
    set_setting(PIPELINE_WATERMARK_KEY, str(int(result_id)))


def find_dealer_by_name(name):
    """Platform dealer whose name matches the retailer name (case/space-insensitive)."""
    if not name or not name.strip():
        return None
    return dlr.one(
        "SELECT * FROM dealers WHERE LOWER(LTRIM(RTRIM(dealer_name)))="
        "LOWER(LTRIM(RTRIM(%(n)s)))", {"n": name.strip()})


def pipeline_sent_result_ids(ids):
    """Subset of the given result_ids already in the send-once ledger."""
    ints = [int(i) for i in ids if i is not None]
    if not ints:
        return set()
    inlist = ",".join(str(i) for i in ints)
    rows = dlr.query(f"SELECT result_id FROM credit_pipeline_sent WHERE result_id IN ({inlist})")
    return {int(r["result_id"]) for r in rows}


def pipeline_claim(result_id, retailer_name):
    """Insert a 'sending' ledger row. Returns True if newly claimed, False if the
    result_id was already present (so a prior/parallel run owns it)."""
    try:
        dlr.execute(
            "INSERT INTO credit_pipeline_sent (result_id, retailer_name, status, sent_at) "
            f"VALUES (%(r)s, %(n)s, 'sending', {NOW})",
            {"r": int(result_id), "n": (retailer_name or None)})
        return True
    except Exception:
        return False


def pipeline_finalize(result_id, dealer_id, lead_id, subsource, status, detail):
    dlr.execute(
        "UPDATE credit_pipeline_sent SET dealer_id=%(d)s, lead_id=%(l)s, "
        f"subsource=%(ss)s, status=%(s)s, detail=%(det)s, sent_at={NOW} "
        "WHERE result_id=%(r)s",
        {"r": int(result_id), "d": dealer_id, "l": lead_id, "ss": subsource,
         "s": status, "det": (detail or "")[:3900]})
