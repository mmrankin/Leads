"""Credit Estimator lead storage — backed by SQL Server `dlrPro`.

A lead is created after page 1 (contact) and updated after the deal page (quiz +
deal inputs + computed estimate). Uses the shared dlrpro_db helper (platform dir
is on sys.path). Public function names/signatures unchanged from the SQLite version.
"""

import logging

import dlrpro_db as dlr
from dlrpro_db import NOW

# Fields written when the lead is first created (after page 1).
CONTACT_FIELDS = (
    "dealer_id", "first_name", "last_name", "email", "phone", "comments",
    "vehicle_year", "vehicle_make", "vehicle_model", "tc_agreed", "tc_agreed_at",
    "email_verdict", "email_score", "source", "subsource", "adf_xml",
    "email1_status", "email1_detail",
)

# Fields written when the estimate is produced (after the deal page).
ESTIMATE_FIELDS = (
    "payment_history", "utilization", "credit_age", "derogatory",
    "vehicle_condition", "vehicle_price", "down_payment", "trade_value",
    "term_months", "annual_income",
    "est_score", "range_low", "range_high", "tier", "apr", "apr_low", "apr_high",
    "amount_financed", "monthly_payment", "max_vehicle_price", "approval",
    "adf_xml",
)


def init_db():
    """`credit_leads` lives in dlrPro (created by migrate_to_dlrpro.py). Ensure
    the `subsource` column exists (added after the initial migration)."""
    try:
        dlr.execute(
            "IF COL_LENGTH('dbo.credit_leads','subsource') IS NULL "
            "ALTER TABLE dbo.credit_leads ADD subsource NVARCHAR(255) NULL"
        )
    except Exception as exc:  # don't block startup if DDL can't run
        logging.getLogger(__name__).warning(
            "Could not ensure credit_leads.subsource column: %s", exc)


def insert_lead(data):
    """Create the lead after page 1 (contact). Returns the new id."""
    v = {k: data.get(k) for k in CONTACT_FIELDS}
    cols = ", ".join(CONTACT_FIELDS)
    ph = ", ".join(f"%({k})s" for k in CONTACT_FIELDS)
    return dlr.insert(f"INSERT INTO credit_leads ({cols}) VALUES ({ph})", v)


def update_adf(lead_id, adf_xml):
    """Attach the initial ADF/XML after insert (the lead id is part of the ADF)."""
    dlr.execute("UPDATE credit_leads SET adf_xml=%(a)s WHERE id=%(id)s",
                {"a": adf_xml, "id": lead_id})


def update_estimate(lead_id, data):
    """Apply deal-page answers + computed estimate + refreshed ADF."""
    v = {k: data.get(k) for k in ESTIMATE_FIELDS}
    v["id"] = lead_id
    sets = ", ".join(f"{k}=%({k})s" for k in ESTIMATE_FIELDS)
    dlr.execute(f"UPDATE credit_leads SET {sets}, stage='complete', "
                f"updated_at={NOW} WHERE id=%(id)s", v)


def set_email_status(lead_id, which, status, detail=None):
    """which = 1 (after page 1) or 2 (after the deal page)."""
    col = "email1_status" if which == 1 else "email2_status"
    dcol = "email1_detail" if which == 1 else "email2_detail"
    dlr.execute(f"UPDATE credit_leads SET {col}=%(s)s, {dcol}=%(d)s WHERE id=%(id)s",
                {"s": status, "d": detail, "id": lead_id})
