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
    (PRODUCT_CREDIT_PIPELINE, "Credit Pipeline", "Credit lead pipeline with ADF/XML delivery and source lineage.", DEFAULT_ADF_SOURCE),
    (PRODUCT_CDP_VMS, "CDP · Vehicle Management", "Appraisal, VIN normalization, valuation, photos & inventory.", DEFAULT_ADF_SOURCE),
    (PRODUCT_CDP_CRM, "CDP · CRM", "Customer relationship management (coming soon).", DEFAULT_ADF_SOURCE),
    (PRODUCT_CDP_PROSPECTING, "CDP · Prospecting", "Data mining & opportunity lists (coming soon).", DEFAULT_ADF_SOURCE),
]


def init_db():
    """Ensure the default products exist (tables themselves are created by the
    one-time migrate_to_dlrpro.py). Idempotent."""
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


# ----- dealers -----

def get_dealer(dealer_id):
    return dlr.one("SELECT * FROM dealers WHERE dealer_id=%(d)s", {"d": dealer_id})


def list_dealers():
    return dlr.query("SELECT * FROM dealers ORDER BY dealer_name")


def upsert_dealer(data):
    fields = ("dealer_id", "dealer_name", "address", "city", "state",
              "zip", "phone", "lead_email_address", "banner_url")
    v = {k: (data.get(k) or None) for k in fields}
    if dlr.one("SELECT 1 AS x FROM dealers WHERE dealer_id=%(dealer_id)s", v):
        dlr.execute(
            "UPDATE dealers SET dealer_name=%(dealer_name)s, address=%(address)s, "
            "city=%(city)s, state=%(state)s, zip=%(zip)s, phone=%(phone)s, "
            "lead_email_address=%(lead_email_address)s, banner_url=%(banner_url)s, "
            f"updated_at={NOW} WHERE dealer_id=%(dealer_id)s", v)
    else:
        dlr.execute(
            "INSERT INTO dealers (dealer_id, dealer_name, address, city, state, zip, "
            "phone, lead_email_address, banner_url) VALUES (%(dealer_id)s, "
            "%(dealer_name)s, %(address)s, %(city)s, %(state)s, %(zip)s, %(phone)s, "
            "%(lead_email_address)s, %(banner_url)s)", v)


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
