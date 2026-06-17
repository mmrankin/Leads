"""Shared platform data access: dealers + product access grants.

Imported by both the dealer-leads app and the trade-in app (each adds this
directory to sys.path). The database file is shared so a dealer is configured
once and granted whichever products apply.

Configuration:
    PLATFORM_DB_PATH   Path to the shared SQLite file. Defaults to platform.db
                       next to this module.
"""

import os
import sqlite3
from datetime import date

DB_PATH = os.environ.get(
    "PLATFORM_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform.db"),
)
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# Canonical product codes.
PRODUCT_LEAD_FORM = "LEAD_FORM"
PRODUCT_TRADE_IN = "TRADE_IN"
PRODUCT_CREDIT_EST = "CREDIT_EST"
DEFAULT_PRODUCTS = [
    (PRODUCT_LEAD_FORM, "Dealer Lead Form", "Customer lead-capture form with ADF/XML delivery."),
    (PRODUCT_TRADE_IN, "Trade-In Widget", "Multi-step trade-in appraisal capture with ADF/XML delivery."),
    (PRODUCT_CREDIT_EST, "Credit Estimator", "Lead form + FICO-range estimate, APR/payment, and affordability."),
]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    conn = get_conn()
    try:
        conn.executescript(schema)
        for code, name, desc in DEFAULT_PRODUCTS:
            conn.execute(
                "INSERT INTO products (product_code, product_name, description) "
                "VALUES (?, ?, ?) ON CONFLICT(product_code) DO UPDATE SET "
                "product_name = excluded.product_name, description = excluded.description",
                (code, name, desc),
            )
        conn.commit()
    finally:
        conn.close()


# ----- dealers -----

def get_dealer(dealer_id):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM dealers WHERE dealer_id = ?", (dealer_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_dealers():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM dealers ORDER BY dealer_name").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_dealer(data):
    fields = (
        "dealer_id", "dealer_name", "address", "city", "state",
        "zip", "phone", "lead_email_address", "banner_url",
    )
    values = {k: (data.get(k) or None) for k in fields}
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO dealers
                (dealer_id, dealer_name, address, city, state, zip,
                 phone, lead_email_address, banner_url)
            VALUES
                (:dealer_id, :dealer_name, :address, :city, :state, :zip,
                 :phone, :lead_email_address, :banner_url)
            ON CONFLICT(dealer_id) DO UPDATE SET
                dealer_name        = excluded.dealer_name,
                address            = excluded.address,
                city               = excluded.city,
                state              = excluded.state,
                zip                = excluded.zip,
                phone              = excluded.phone,
                lead_email_address = excluded.lead_email_address,
                banner_url         = excluded.banner_url,
                updated_at         = datetime('now')
            """,
            values,
        )
        conn.commit()
    finally:
        conn.close()


# ----- products -----

def list_products():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM products ORDER BY product_name").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ----- grants -----

def list_grants(dealer_id=None):
    conn = get_conn()
    try:
        if dealer_id:
            rows = conn.execute(
                "SELECT * FROM dealer_products WHERE dealer_id = ? ORDER BY product_code",
                (dealer_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dealer_products ORDER BY dealer_id, product_code"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_grant(data):
    fields = (
        "dealer_id", "product_code", "valid_from", "valid_to",
        "monthly_price", "per_lead_price",
    )
    values = {k: (data.get(k) if data.get(k) not in ("",) else None) for k in fields}
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO dealer_products
                (dealer_id, product_code, valid_from, valid_to,
                 monthly_price, per_lead_price)
            VALUES
                (:dealer_id, :product_code, :valid_from, :valid_to,
                 :monthly_price, :per_lead_price)
            ON CONFLICT(dealer_id, product_code) DO UPDATE SET
                valid_from     = excluded.valid_from,
                valid_to       = excluded.valid_to,
                monthly_price  = excluded.monthly_price,
                per_lead_price = excluded.per_lead_price,
                updated_at     = datetime('now')
            """,
            values,
        )
        conn.commit()
    finally:
        conn.close()


def delete_grant(grant_id):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM dealer_products WHERE id = ?", (grant_id,))
        conn.commit()
    finally:
        conn.close()


def get_active_grant(dealer_id, product_code, on_date=None):
    """Return the grant row if the dealer's product is active on the given date
    (default: today), else None. NULL bounds are treated as unbounded.
    ISO 'YYYY-MM-DD' strings compare correctly lexicographically.
    """
    today = on_date or date.today().isoformat()
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT * FROM dealer_products
            WHERE dealer_id = ? AND product_code = ?
              AND (valid_from IS NULL OR valid_from <= ?)
              AND (valid_to   IS NULL OR valid_to   >= ?)
            """,
            (dealer_id, product_code, today, today),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def dealer_has_product(dealer_id, product_code, on_date=None):
    return get_active_grant(dealer_id, product_code, on_date) is not None


# ----- valuation settings -----

# The per-condition adjustment fields, in column order.
CONDITION_ADJ_FIELDS = (
    "adj_keys_1", "adj_keys_3plus", "adj_unrepaired_damage", "adj_engine_light",
    "adj_airbag_light", "adj_brake_light", "adj_aftermarket_exhaust",
    "adj_aftermarket_engine", "adj_aftermarket_stereo",
)

# Recommended adjustments (our assessment of value impact), per unit.
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
    """Recommended values for the given unit (dollar|percent)."""
    return dict(RECOMMENDED.get(unit, RECOMMENDED["dollar"]))


def get_valuation_settings(dealer_id):
    """Return the dealer's valuation settings, or sensible defaults if unset."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM dealer_valuation_settings WHERE dealer_id = ?",
            (dealer_id,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        return dict(row)
    defaults = {"dealer_id": dealer_id, "base_source": "retail",
                "adjustment_unit": "dollar"}
    defaults.update(RECOMMENDED["dollar"])
    return defaults


def upsert_valuation_settings(data):
    values = {"dealer_id": data["dealer_id"]}
    for f in ALL_SETTING_FIELDS:
        values[f] = data.get(f)
    cols = ", ".join(("dealer_id",) + ALL_SETTING_FIELDS)
    params = ", ".join(f":{c}" for c in ("dealer_id",) + ALL_SETTING_FIELDS)
    updates = ", ".join(f"{f} = excluded.{f}" for f in ALL_SETTING_FIELDS)
    conn = get_conn()
    try:
        conn.execute(
            f"INSERT INTO dealer_valuation_settings ({cols}) VALUES ({params}) "
            f"ON CONFLICT(dealer_id) DO UPDATE SET {updates}, updated_at=datetime('now')",
            values,
        )
        conn.commit()
    finally:
        conn.close()


# ----- credit estimator settings -----
#
# The financial knobs the Credit Estimator uses to turn an estimated FICO range
# into an APR, a monthly payment, and a max-affordable figure. The FICO-range
# scoring model itself lives in the credit-estimator app (credit.py) and is not
# dealer-configurable; only these lender-facing rates/limits are. Pattern mirrors
# dealer_valuation_settings: edit per dealer in admin, reset to RECOMMENDED.

# APR by estimated FICO tier (used-vehicle base rate, annual percent).
CREDIT_APR_FIELDS = (
    "apr_exceptional",  # 800-850
    "apr_very_good",    # 740-799
    "apr_good",         # 670-739
    "apr_fair",         # 580-669
    "apr_poor",         # 300-579
)

CREDIT_SETTING_FIELDS = CREDIT_APR_FIELDS + (
    "new_apr_delta",     # added to the tier APR when the vehicle is new (usually negative)
    "apr_spread",        # +/- percentage points shown around the estimated APR
    "max_term_months",   # longest term offered, used for the affordability calc
    "max_payment_pct",   # share of gross monthly income allotted to the car payment
)

# Our recommended starting points (a dealer can override any of them).
RECOMMENDED_CREDIT = {
    "apr_exceptional": 6.49, "apr_very_good": 7.49, "apr_good": 9.99,
    "apr_fair": 14.49, "apr_poor": 19.99,
    "new_apr_delta": -1.0, "apr_spread": 1.0,
    "max_term_months": 72, "max_payment_pct": 15.0,
}


def recommended_credit_settings():
    return dict(RECOMMENDED_CREDIT)


def get_credit_settings(dealer_id):
    """Return the dealer's credit settings, or RECOMMENDED defaults if unset."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM dealer_credit_settings WHERE dealer_id = ?",
            (dealer_id,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        return dict(row)
    defaults = {"dealer_id": dealer_id}
    defaults.update(RECOMMENDED_CREDIT)
    return defaults


def upsert_credit_settings(data):
    values = {"dealer_id": data["dealer_id"]}
    for f in CREDIT_SETTING_FIELDS:
        values[f] = data.get(f)
    cols = ", ".join(("dealer_id",) + CREDIT_SETTING_FIELDS)
    params = ", ".join(f":{c}" for c in ("dealer_id",) + CREDIT_SETTING_FIELDS)
    updates = ", ".join(f"{f} = excluded.{f}" for f in CREDIT_SETTING_FIELDS)
    conn = get_conn()
    try:
        conn.execute(
            f"INSERT INTO dealer_credit_settings ({cols}) VALUES ({params}) "
            f"ON CONFLICT(dealer_id) DO UPDATE SET {updates}, updated_at=datetime('now')",
            values,
        )
        conn.commit()
    finally:
        conn.close()
