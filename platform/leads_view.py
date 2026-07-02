"""Read-only access to the lead tables (in SQL Server `dlrPro`) for the admin.

All three lead types now live in dlrPro; this reads them and normalizes rows
into a common shape so the admin can browse them in one place.
"""

import json

import dlrpro_db as dlr


def _fetch(table, dealer_id, limit):
    top = "TOP %d " % int(limit)
    try:
        if dealer_id:
            return dlr.query(f"SELECT {top}* FROM {table} WHERE dealer_id=%(d)s "
                             "ORDER BY created_at DESC", {"d": dealer_id})
        return dlr.query(f"SELECT {top}* FROM {table} ORDER BY created_at DESC")
    except Exception:
        return []


def _vehicle(r, trim=False):
    parts = [r.get("vehicle_year"), r.get("vehicle_make"), r.get("vehicle_model")]
    if trim and r.get("vehicle_trim"):
        parts.append(r.get("vehicle_trim"))
    return " ".join(str(p) for p in parts if p)


def _name(r):
    return " ".join(x for x in (r.get("first_name"), r.get("last_name")) if x) or "—"


def lead_form_leads(dealer_id=None, limit=300):
    rows = _fetch("leads", dealer_id, limit)
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
    rows = _fetch("trade_leads", dealer_id, limit)
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
    rows = _fetch("credit_leads", dealer_id, limit)
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
    ("Customer", ["first_name", "last_name", "email", "phone",
                  "address", "city", "state", "zip", "comments",
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
                  "email2_status", "email2_detail", "source", "subsource",
                  "created_at", "updated_at"]),
]

FIELD_LABELS = {
    "first_name": "First name", "last_name": "Last name", "email": "Email",
    "phone": "Phone", "comments": "Comments", "source": "Source",
    "subsource": "Sub-source",
    "address": "Address", "city": "City", "state": "State", "zip": "Zip",
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


# ----- Trigger Leads (CreditPipeline match_result, via the 10.1.4.8 linked server) -----

_TRIGGER_LEADS_SQL = """SELECT TOP {limit}
  c.first_name AS cr_first, c.last_name AS cr_last,
  r.retailer_name AS CPName, d.dealer_name AS ADFName,
  m.result_id, m.run_group_id, m.run_id, m.subscription_id, m.bucket_id,
  m.candidate_id, m.customer_record_id, m.retailer_id,
  CAST(m.matched_payload AS NVARCHAR(MAX)) AS matched_payload,
  CONVERT(varchar(19), m.returned_at, 120) AS returned_at,
  m.stream_session_id, m.consumer_zip,
  s.id AS sent_id, CONVERT(varchar(19), s.created, 120) AS sent_at
FROM [10.1.4.8].[CreditPipeline].[dbo].[match_result] m
LEFT JOIN [10.1.4.8].[CreditPipeline].[dbo].[customer_record] c ON c.customer_record_id = m.customer_record_id
LEFT JOIN [10.1.4.8].[CreditPipeline].[dbo].[retailer] r ON r.retailer_id = m.retailer_id
LEFT JOIN dlrPro.dbo.dealers d ON d.dealer_id = r.retailer_code
LEFT JOIN dlrPro.dbo.[sent] s ON d.id = s.dealer_id AND m.result_id = s.result_id
{where}
ORDER BY m.result_id ASC"""


def trigger_leads(matching_customer=False, matching_dealer=False,
                  sent_status="unsent", limit=1000):
    """Rows from the CreditPipeline match_result feed joined to the ADF dealer and
    the sent ledger. Filters: matching_customer (c.last_name not null),
    matching_dealer (d.dealer_name not null), sent_status = unsent|sent|all."""
    conds = []
    if matching_customer:
        # "Has a customer name" from the best source: the dealer's matched
        # customer_record OR the matched_payload trigger consumer (what the ADF sends).
        conds.append("(c.last_name IS NOT NULL OR "
                     "JSON_VALUE(CAST(m.matched_payload AS NVARCHAR(MAX)),'$.last_name') IS NOT NULL)")
    if matching_dealer:
        conds.append("d.dealer_name IS NOT NULL")
    if sent_status == "unsent":
        conds.append("s.id IS NULL")
    elif sent_status == "sent":
        conds.append("s.id IS NOT NULL")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    try:
        rows = dlr.query(_TRIGGER_LEADS_SQL.format(limit=int(limit), where=where))
    except Exception:
        return []
    for r in rows:
        r["sent"] = r.get("sent_id") is not None
        raw = r.get("matched_payload") or ""
        try:
            payload = json.loads(raw) if raw else {}
            r["trigger"] = (payload.get("trigger_desc") or "").strip() or None
            r["payload_pretty"] = json.dumps(payload, indent=2) if payload else raw
        except Exception:
            payload = {}
            r["trigger"] = None
            r["payload_pretty"] = raw
        # Best customer match: the dealer's matched customer_record, else the
        # matched_payload trigger consumer (the same name the ADF/XML sends).
        cr = " ".join(x for x in (r.get("cr_first"), r.get("cr_last")) if x).strip()
        pl = " ".join(x for x in (payload.get("first_name"), payload.get("last_name")) if x).strip()
        r["CustomerName"] = cr or pl or None
    return rows


def get_lead_detail(product, lead_id):
    """Return (row_dict, groups, adf_xml) for a single lead, or (None, [], None)."""
    if product == "TRADE_IN":
        table, groups = "trade_leads", TRADE_IN_GROUPS
    elif product == "CREDIT_EST":
        table, groups = "credit_leads", CREDIT_GROUPS
    else:
        table, groups = "leads", LEAD_FORM_GROUPS
    try:
        row = dlr.one(f"SELECT * FROM {table} WHERE id=%(id)s", {"id": lead_id})
    except Exception:
        row = None
    if not row:
        return None, [], None
    return row, groups, row.get("adf_xml")
