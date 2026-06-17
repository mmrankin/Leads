"""SQLite access helpers for the Credit Estimator.

A lead is created after page 1 (contact) and updated after page 2 (the FICO
quiz + deal inputs, plus the computed estimate).
"""

import os
import sqlite3

DB_PATH = os.environ.get(
    "CREDIT_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "credit.db"),
)
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


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
        conn.commit()
    finally:
        conn.close()


# Fields written when the lead is first created (after page 1).
CONTACT_FIELDS = (
    "dealer_id", "first_name", "last_name", "email", "phone", "comments",
    "vehicle_year", "vehicle_make", "vehicle_model", "tc_agreed", "tc_agreed_at",
    "email_verdict", "email_score", "source", "adf_xml",
    "email1_status", "email1_detail",
)

# Fields written when the estimate is produced (after page 2).
ESTIMATE_FIELDS = (
    "payment_history", "utilization", "credit_age", "derogatory",
    "vehicle_condition", "vehicle_price", "down_payment", "trade_value",
    "term_months", "annual_income",
    "est_score", "range_low", "range_high", "tier", "apr", "apr_low", "apr_high",
    "amount_financed", "monthly_payment", "max_vehicle_price", "approval",
    "adf_xml",
)


def insert_lead(data):
    """Create the lead after page 1 (contact). Returns the new id."""
    values = {k: data.get(k) for k in CONTACT_FIELDS}
    cols = ", ".join(CONTACT_FIELDS)
    params = ", ".join(f":{k}" for k in CONTACT_FIELDS)
    conn = get_conn()
    try:
        cur = conn.execute(
            f"INSERT INTO credit_leads ({cols}) VALUES ({params})", values
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_estimate(lead_id, data):
    """Apply page-2 answers + the computed estimate + refreshed ADF."""
    values = {k: data.get(k) for k in ESTIMATE_FIELDS}
    values["id"] = lead_id
    sets = ", ".join(f"{k} = :{k}" for k in ESTIMATE_FIELDS)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE credit_leads SET {sets}, stage='complete', "
            f"updated_at=datetime('now') WHERE id = :id",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def set_email_status(lead_id, which, status, detail=None):
    """which = 1 (after page 1) or 2 (after page 2)."""
    col = "email1_status" if which == 1 else "email2_status"
    dcol = "email1_detail" if which == 1 else "email2_detail"
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE credit_leads SET {col} = ?, {dcol} = ? WHERE id = ?",
            (status, detail, lead_id),
        )
        conn.commit()
    finally:
        conn.close()
