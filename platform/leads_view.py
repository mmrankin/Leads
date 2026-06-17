"""Read-only access to the two product lead databases for the admin.

Leads live in each app's own SQLite file; this opens them read-only and
normalizes rows into a common shape so the admin can browse them in one place.

Config (env, with sensible defaults relative to this package):
    LEADS_DB_PATH      dealer-leads leads.db
    TRADEIN_DB_PATH    trade-in trade_in.db
    CREDIT_DB_PATH     credit-estimator credit.db
"""

import os
import sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))
LEADS_DB_PATH = os.environ.get(
    "LEADS_DB_PATH", os.path.join(_HERE, "..", "dealer-leads", "leads.db"))
TRADEIN_DB_PATH = os.environ.get(
    "TRADEIN_DB_PATH", os.path.join(_HERE, "..", "trade-in", "trade_in.db"))
CREDIT_DB_PATH = os.environ.get(
    "CREDIT_DB_PATH", os.path.join(_HERE, "..", "credit-estimator", "credit.db"))


def _query(path, sql, params=()):
    if not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _vehicle(r, trim=False):
    parts = [r.get("vehicle_year"), r.get("vehicle_make"), r.get("vehicle_model")]
    if trim and r.get("vehicle_trim"):
        parts.append(r.get("vehicle_trim"))
    return " ".join(str(p) for p in parts if p)


def _name(r):
    return " ".join(x for x in (r.get("first_name"), r.get("last_name")) if x) or "—"


def lead_form_leads(dealer_id=None, limit=300):
    where = "WHERE dealer_id = ?" if dealer_id else ""
    args = (dealer_id,) if dealer_id else ()
    rows = _query(
        LEADS_DB_PATH,
        f"SELECT * FROM leads {where} ORDER BY created_at DESC LIMIT {int(limit)}",
        args,
    )
    out = []
    for r in rows:
        out.append({
            "product": "LEAD_FORM", "id": r.get("id"), "dealer_id": r.get("dealer_id"),
            "name": _name(r), "email": r.get("email"), "phone": r.get("phone"),
            "vehicle": _vehicle(r), "value": None, "stage": None,
            "email_status": r.get("email_status"),
            "verdict": r.get("email_verdict"), "created_at": r.get("created_at"),
        })
    return out


def trade_leads(dealer_id=None, limit=300):
    where = "WHERE dealer_id = ?" if dealer_id else ""
    args = (dealer_id,) if dealer_id else ()
    rows = _query(
        TRADEIN_DB_PATH,
        f"SELECT * FROM trade_leads {where} ORDER BY created_at DESC LIMIT {int(limit)}",
        args,
    )
    out = []
    for r in rows:
        value = None
        if r.get("value_low") is not None and r.get("value_high") is not None:
            value = f"${int(r['value_low']):,} - ${int(r['value_high']):,}"
        email_status = r.get("email2_status") or r.get("email1_status")
        out.append({
            "product": "TRADE_IN", "id": r.get("id"), "dealer_id": r.get("dealer_id"),
            "name": _name(r), "email": r.get("email"), "phone": r.get("phone"),
            "vehicle": _vehicle(r, trim=True), "value": value, "stage": r.get("stage"),
            "email_status": email_status,
            "verdict": r.get("email_verdict"), "created_at": r.get("created_at"),
        })
    return out


def credit_leads(dealer_id=None, limit=300):
    where = "WHERE dealer_id = ?" if dealer_id else ""
    args = (dealer_id,) if dealer_id else ()
    rows = _query(
        CREDIT_DB_PATH,
        f"SELECT * FROM credit_leads {where} ORDER BY created_at DESC LIMIT {int(limit)}",
        args,
    )
    out = []
    for r in rows:
        value = None
        if r.get("range_low") is not None and r.get("range_high") is not None:
            value = f"{r['range_low']}-{r['range_high']}"
            if r.get("tier"):
                value += f" ({r['tier']})"
        email_status = r.get("email2_status") or r.get("email1_status")
        out.append({
            "product": "CREDIT_EST", "id": r.get("id"), "dealer_id": r.get("dealer_id"),
            "name": _name(r), "email": r.get("email"), "phone": r.get("phone"),
            "vehicle": _vehicle(r), "value": value, "stage": r.get("stage"),
            "email_status": email_status,
            "verdict": r.get("email_verdict"), "created_at": r.get("created_at"),
        })
    return out


def all_leads(dealer_id=None, limit=300):
    combined = (lead_form_leads(dealer_id, limit) + trade_leads(dealer_id, limit)
                + credit_leads(dealer_id, limit))
    combined.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return combined[:limit]


# Field groups for the detail page, in display order. Keys not present on a row
# are simply skipped, so this is safe across both products.
LEAD_FORM_GROUPS = [
    ("Customer", ["first_name", "last_name", "email", "phone"]),
    ("Vehicle of interest", ["vehicle_year", "vehicle_make", "vehicle_model"]),
    ("Message", ["comments"]),
    ("Delivery", ["email_status", "email_detail", "email_verdict", "email_score",
                  "source", "created_at"]),
]
TRADE_IN_GROUPS = [
    ("Customer", ["first_name", "last_name", "email", "phone",
                  "tc_agreed", "tc_agreed_at"]),
    ("Trade-in vehicle", ["vehicle_year", "vehicle_make", "vehicle_model",
                          "vehicle_trim", "miles"]),
    ("Condition", ["num_keys", "unrepaired_damage", "engine_light",
                   "airbag_light", "brake_light", "aftermarket_exhaust",
                   "aftermarket_engine", "aftermarket_stereo"]),
    ("Ownership", ["own_or_lease", "ownership_status", "loan_balance",
                   "lease_months_remaining"]),
    ("Valuation", ["value_source", "value_low", "value_estimate", "value_high"]),
    ("Delivery", ["stage", "email_verdict", "email_score",
                  "email1_status", "email1_detail",
                  "email2_status", "email2_detail", "created_at", "updated_at"]),
]
CREDIT_GROUPS = [
    ("Customer", ["first_name", "last_name", "email", "phone", "comments",
                  "tc_agreed", "tc_agreed_at"]),
    ("Credit questionnaire", ["payment_history", "utilization", "credit_age",
                              "derogatory"]),
    ("Deal", ["vehicle_condition", "vehicle_price", "down_payment", "trade_value",
              "term_months", "annual_income"]),
    ("Estimate", ["est_score", "range_low", "range_high", "tier", "approval",
                  "apr", "apr_low", "apr_high", "amount_financed",
                  "monthly_payment", "max_vehicle_price"]),
    ("Delivery", ["stage", "email_verdict", "email_score",
                  "email1_status", "email1_detail",
                  "email2_status", "email2_detail", "source",
                  "created_at", "updated_at"]),
]

FIELD_LABELS = {
    "first_name": "First name", "last_name": "Last name", "email": "Email",
    "phone": "Phone", "comments": "Comments", "source": "Source",
    "tc_agreed": "Agreed to T&C", "tc_agreed_at": "T&C agreed at",
    "vehicle_year": "Year", "vehicle_make": "Make", "vehicle_model": "Model",
    "vehicle_trim": "Trim", "miles": "Mileage",
    "num_keys": "Number of keys", "unrepaired_damage": "Un-repaired damage",
    "engine_light": "Engine light on", "airbag_light": "Airbag light on",
    "brake_light": "Brake light on", "aftermarket_exhaust": "Aftermarket exhaust",
    "aftermarket_engine": "Aftermarket engine components",
    "aftermarket_stereo": "Aftermarket stereo/electronics",
    "own_or_lease": "Own or lease", "ownership_status": "Loan/lease/title",
    "loan_balance": "Outstanding loan balance",
    "lease_months_remaining": "Months left on lease",
    "value_source": "Value basis", "value_low": "Value (low)",
    "value_estimate": "Value (estimate)", "value_high": "Value (high)",
    "stage": "Stage", "email_status": "Email status", "email_detail": "Email detail",
    "email_verdict": "Email verdict", "email_score": "Email score",
    "email1_status": "Email #1 status", "email1_detail": "Email #1 detail",
    "email2_status": "Email #2 status", "email2_detail": "Email #2 detail",
    "created_at": "Created", "updated_at": "Updated",
    # credit estimator
    "payment_history": "Payment history", "utilization": "Credit utilization",
    "credit_age": "Length of credit", "derogatory": "Derogatory marks",
    "vehicle_condition": "New or used", "vehicle_price": "Vehicle price",
    "down_payment": "Down payment", "trade_value": "Trade-in value",
    "term_months": "Loan term (months)", "annual_income": "Annual income",
    "est_score": "Estimated score", "range_low": "Range (low)",
    "range_high": "Range (high)", "tier": "Tier", "approval": "Approval read",
    "apr": "Estimated APR", "apr_low": "APR (low)", "apr_high": "APR (high)",
    "amount_financed": "Amount financed", "monthly_payment": "Monthly payment",
    "max_vehicle_price": "Max affordable price",
}


def get_lead_detail(product, lead_id):
    """Return (row_dict, groups, adf_xml) for a single lead, or (None, [], None)."""
    if product == "TRADE_IN":
        path, table, groups = TRADEIN_DB_PATH, "trade_leads", TRADE_IN_GROUPS
    elif product == "CREDIT_EST":
        path, table, groups = CREDIT_DB_PATH, "credit_leads", CREDIT_GROUPS
    else:
        path, table, groups = LEADS_DB_PATH, "leads", LEAD_FORM_GROUPS
    rows = _query(path, f"SELECT * FROM {table} WHERE id = ?", (lead_id,))
    if not rows:
        return None, [], None
    row = rows[0]
    return row, groups, row.get("adf_xml")
