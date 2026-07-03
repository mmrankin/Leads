"""Read-only access to the lead tables (in SQL Server `dlrPro`) for the admin.

All three lead types now live in dlrPro; this reads them and normalizes rows
into a common shape so the admin can browse them in one place.
"""

import calendar
import json
from datetime import date, timedelta

import dlrpro_db as dlr


# ----- Credit Pipeline volume report (per-dealer counts from the dbo.sent ledger) -----

_VOLUME_SQL = """SELECT d.dealer_id, d.dealer_name, dp.max_leads_per_month,
  SUM(CASE WHEN s.created >= %(lm)s AND s.created < %(tm)s THEN 1 ELSE 0 END) AS last_month,
  SUM(CASE WHEN s.created >= %(tm)s THEN 1 ELSE 0 END) AS this_month,
  SUM(CASE WHEN s.created >= %(yst)s AND s.created < %(td)s THEN 1 ELSE 0 END) AS yesterday,
  SUM(CASE WHEN s.created >= %(td)s THEN 1 ELSE 0 END) AS today
FROM dlrPro.dbo.dealers d
JOIN dlrPro.dbo.dealer_products dp ON dp.dealer_id = d.dealer_id AND dp.product_code = 'CREDIT_PIPELINE'
LEFT JOIN dlrPro.dbo.[sent] s ON s.dealer_id = d.id
GROUP BY d.dealer_id, d.dealer_name, dp.max_leads_per_month
ORDER BY d.dealer_name"""

_DEFAULT_MAX_LEADS = 10


def pipeline_volume():
    """Per-dealer Credit Pipeline lead volume from the dbo.sent ledger: last
    month, this month, yesterday, today, plus a month-end projection ("tracking")
    at the current pace and the requested daily volume (max_leads_per_month / days
    in the month)."""
    today = date.today()
    tm = today.replace(day=1)                       # first of this month
    lm = (tm - timedelta(days=1)).replace(day=1)    # first of last month
    yst = today - timedelta(days=1)
    try:
        rows = dlr.query(_VOLUME_SQL, {"td": today.isoformat(), "yst": yst.isoformat(),
                                       "tm": tm.isoformat(), "lm": lm.isoformat()})
    except Exception:
        return []
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    dom = today.day
    for r in rows:
        mx = r.get("max_leads_per_month")
        mx = int(mx) if mx is not None else _DEFAULT_MAX_LEADS
        r["max_leads_per_month"] = mx
        this_month = int(r.get("this_month") or 0)
        r["tracking"] = round(this_month / dom * days_in_month) if dom else this_month
        r["requested_daily"] = round(mx / days_in_month, 1)
    return rows


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
  e.FirstName AS eq_first, e.LastName AS eq_last, m.consumer_id,
  r.retailer_name AS CPName, d.dealer_name AS ADFName,
  m.result_id, m.run_group_id, m.run_id, m.subscription_id, m.bucket_id,
  m.candidate_id, m.customer_record_id, m.retailer_id,
  CAST(m.matched_payload AS NVARCHAR(MAX)) AS matched_payload,
  CONVERT(varchar(19), m.returned_at, 120) AS returned_at,
  m.stream_session_id, m.consumer_zip,
  s.id AS sent_id, CONVERT(varchar(19), s.created, 120) AS sent_at
FROM [10.1.4.8].[CreditPipeline].[dbo].[match_result] m
LEFT JOIN [10.1.4.8].[CreditPipeline].[dbo].[customer_record] c ON c.customer_record_id = m.customer_record_id
LEFT JOIN [10.1.4.8].[CreditPipeline].[dbo].[vw_EquifaxConsumerRecord] e ON e.Consumer_ID = m.consumer_id
LEFT JOIN [10.1.4.8].[CreditPipeline].[dbo].[retailer] r ON r.retailer_id = m.retailer_id
LEFT JOIN dlrPro.dbo.dealers d ON d.dealer_id = r.retailer_code
LEFT JOIN dlrPro.dbo.[sent] s ON d.id = s.dealer_id AND m.result_id = s.result_id
{where}
ORDER BY m.result_id ASC"""


def _has_text(*vals):
    return any((str(v).strip() if v is not None else "") for v in vals)


def _trigger_contact_map(result_ids):
    """result_id -> (has_email, has_phone) from the trigger view's own contact
    columns (source 1). One batched query."""
    ids = ",".join(str(int(x)) for x in result_ids if x is not None)
    if not ids:
        return {}
    out = {}
    try:
        rows = dlr.query(
            "SELECT result_id, Email, CellPhone, HomePhone, WorkPhone, AppendedEmail, AppendedPhone "
            "FROM [10.1.4.8].[CreditPipeline].[dbo].[vw_EquifaxConsumerRecordTriggers] "
            "WHERE result_id IN (%s)" % ids)
    except Exception:
        return {}
    for r in rows:
        out[r["result_id"]] = (
            _has_text(r.get("Email"), r.get("AppendedEmail")),
            _has_text(r.get("CellPhone"), r.get("HomePhone"), r.get("WorkPhone"), r.get("AppendedPhone")),
        )
    return out


def _equifax_contact_sets(consumer_ids):
    """(cids-with-email, cids-with-phone) from equifax..Consumer* (source 2).
    Two batched queries."""
    ids = ",".join(str(int(x)) for x in consumer_ids if x)
    if not ids:
        return set(), set()
    try:
        em = {r["consumer_id"] for r in dlr.query(
            "SELECT DISTINCT consumer_id FROM [10.1.4.8].equifax.dbo.ConsumerEmails "
            "WHERE consumer_id IN (%s) AND NULLIF(LTRIM(RTRIM(emailAddress)),'') IS NOT NULL" % ids)}
        ph = {r["consumer_id"] for r in dlr.query(
            "SELECT DISTINCT consumer_id FROM [10.1.4.8].equifax.dbo.ConsumerTelephones "
            "WHERE consumer_id IN (%s) AND NULLIF(LTRIM(RTRIM(telephoneNumber)),'') IS NOT NULL" % ids)}
    except Exception:
        return set(), set()
    return em, ph


def _ownership_batch(keys):
    """{(last_name, address, zip): (has_email, has_phone, year, make)} from
    panafax..tbl_ownership for a set of keys, in ONE query — OUTER APPLY does a
    zip-indexed TOP-1 (most-recent) seek per key (the table is ~340M rows). Params
    CAST to varchar for the seek; single quotes escaped in the VALUES list."""
    keys = sorted({(k[0].strip(), k[1].strip(), k[2].strip()) for k in keys
                   if k[0] and k[1] and k[2] and k[0].strip() and k[1].strip() and k[2].strip()})
    if not keys:
        return {}
    esc = lambda s: s.replace("'", "''")
    values = ",".join("('%s','%s','%s')" % (esc(ln), esc(ad), esc(z)) for (ln, ad, z) in keys)
    # Fuzzy match: exact zip, last_name LIKE first-5 + '%', address1 LIKE first-8 + '%'.
    sql = ("SELECT k.ln, k.ad, k.z, o.email, o.primary_phone, o.[year], o.make "
           "FROM (VALUES %s) k(ln, ad, z) "
           "OUTER APPLY (SELECT TOP 1 email, primary_phone, [year], make "
           "  FROM panafax..tbl_ownership WITH (NOLOCK) "
           "  WHERE zip = CAST(k.z AS varchar(20)) "
           "    AND last_name LIKE LEFT(CAST(k.ln AS varchar(100)), 5) + '%%' "
           "    AND address1 LIKE LEFT(CAST(k.ad AS varchar(200)), 8) + '%%' "
           "  ORDER BY last_seen DESC) o" % values)
    out = {}
    try:
        for r in dlr.query(sql):
            has_em = bool((r.get("email") or "").strip())
            phone = str(r.get("primary_phone") or "").strip()
            has_ph = len("".join(ch for ch in phone if ch.isdigit())) >= 7
            out[(r["ln"], r["ad"], r["z"])] = (
                has_em, has_ph, str(r.get("year") or "").strip(), (r.get("make") or "").strip())
    except Exception:
        return {}
    return out


# Per-result_id cache of (has_email, has_phone, veh). These derive from immutable
# source data (the match record + Equifax + tbl_ownership), so once computed for a
# result_id they never change — cache for the process lifetime. Only the first page
# load pays the (heavy) tbl_ownership cost; later loads only resolve new rows.
_TRIG_CONTACT_CACHE = {}


def _annotate_contact_flags(rows):
    """Set r['has_phone']/r['has_email'] and r['veh'] (year + make) per row from:
    (1) the trigger view, (2) equifax..Consumer* by consumer_id, (3) tbl_ownership
    (which also provides the vehicle). Batched, and cached per result_id."""
    if not rows:
        return
    todo = [r for r in rows if r.get("result_id") not in _TRIG_CONTACT_CACHE]
    if todo:
        tmap = _trigger_contact_map([r.get("result_id") for r in todo])
        em_cids, ph_cids = _equifax_contact_sets([r.get("consumer_id") for r in todo])
        omap = _ownership_batch([(r.get("_ln") or "", r.get("_addr") or "", r.get("_zip") or "")
                                 for r in todo])
        for r in todo:
            t_em, t_ph = tmap.get(r.get("result_id"), (False, False))
            o_em, o_ph, yr, mk = omap.get(((r.get("_ln") or "").strip(), (r.get("_addr") or "").strip(),
                                           (r.get("_zip") or "").strip()), (False, False, "", ""))
            _TRIG_CONTACT_CACHE[r.get("result_id")] = (
                t_em or (r.get("consumer_id") in em_cids) or o_em,
                t_ph or (r.get("consumer_id") in ph_cids) or o_ph,
                " ".join(p for p in (yr, mk) if p),
            )
    for r in rows:
        r["has_email"], r["has_phone"], r["veh"] = _TRIG_CONTACT_CACHE.get(
            r.get("result_id"), (False, False, ""))


def trigger_leads(matching_customer=False, matching_dealer=False,
                  sent_status="unsent", limit=1000):
    """Rows from the CreditPipeline match_result feed joined to the ADF dealer and
    the sent ledger. Filters: matching_customer (c.last_name not null),
    matching_dealer (d.dealer_name not null), sent_status = unsent|sent|all."""
    # Never show rows with a blank consumer_id on match_result.
    conds = ["m.consumer_id IS NOT NULL"]
    if matching_customer:
        # "Matching customer" = the consumer_id resolved to an Equifax consumer
        # record (the definitive match).
        conds.append("e.Consumer_ID IS NOT NULL")
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
        # Best customer match, in order: the dealer's matched customer_record,
        # the Equifax consumer matched by consumer_id, then the matched_payload
        # trigger consumer (the name the ADF/XML sends).
        cr = " ".join(x for x in (r.get("cr_first"), r.get("cr_last")) if x).strip()
        eq = " ".join(x for x in (r.get("eq_first"), r.get("eq_last")) if x).strip()
        pl = " ".join(x for x in (payload.get("first_name"), payload.get("last_name")) if x).strip()
        r["CustomerName"] = cr or eq or pl or None
        r["customer_matched"] = bool(r.get("eq_last"))   # consumer_id -> Equifax view
        # Stashed for the tbl_ownership contact check (source 3).
        r["_ln"] = (r.get("cr_last") or r.get("eq_last") or payload.get("last_name") or "").strip()
        r["_addr"] = (payload.get("address_line_1") or "").strip()
        r["_zip"] = (r.get("consumer_zip") or payload.get("consumer_zip") or "").strip()
    _annotate_contact_flags(rows)
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
