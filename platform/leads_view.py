"""Read-only access to the lead tables (in SQL Server `dlrPro`) for the admin.

All three lead types now live in dlrPro; this reads them and normalizes rows
into a common shape so the admin can browse them in one place.
"""

import calendar
import json
import logging
from datetime import date, timedelta

import dlrpro_db as dlr

LOG = logging.getLogger("leads_view")


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
        r["needed"] = max(0, mx - this_month)   # more leads to reach the monthly max
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

# NOTE: the Equifax consumer view (vw_EquifaxConsumerRecord, for eq_first/eq_last)
# is deliberately NOT joined here. Each join is fast alone, but combining this
# remote view with the other remote (customer_record/retailer) and local
# (dealers/sent) joins collapses the distributed query plan into a hard timeout
# (0.2s -> 120s+). The Equifax names are fetched separately in _equifax_name_map()
# and merged in trigger_leads(); "matching_customer" is then filtered in Python.
_TRIGGER_LEADS_SQL = """SELECT TOP {limit}
  c.first_name AS cr_first, c.last_name AS cr_last, m.consumer_id,
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


def _equifax_name_map(consumer_ids):
    """consumer_id -> (FirstName, LastName) from vw_EquifaxConsumerRecord. Fetched
    on its own (not joined into the main feed query — see the note above) and keyed
    by int(consumer_id)."""
    ids = ",".join(str(int(x)) for x in consumer_ids if x)
    if not ids:
        return {}
    out = {}
    try:
        rows = dlr.query(
            "SELECT Consumer_ID, FirstName, LastName "
            "FROM [10.1.4.8].[CreditPipeline].[dbo].[vw_EquifaxConsumerRecord] "
            "WHERE Consumer_ID IN (%s)" % ids)
    except Exception:
        return {}
    for r in rows:
        cid = r.get("Consumer_ID")
        if cid is not None:
            out[int(cid)] = (r.get("FirstName"), r.get("LastName"))
    return out


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


def _own_row_flags(r):
    """(has_email, has_phone, year, make) from an ownership query row."""
    has_em = bool((r.get("email") or "").strip())
    phone = str(r.get("primary_phone") or "").strip()
    has_ph = len("".join(ch for ch in phone if ch.isdigit())) >= 7
    return (has_em, has_ph, str(r.get("year") or "").strip(), (r.get("make") or "").strip())


def _ownership_batch(keys):
    """{(last_name, first_name, address, zip): (has_email, has_phone, year, make)}
    from panafax..tbl_ownership (~340M rows). Primary match = exact zip + last_name
    LIKE first-5 + address1 LIKE first-8; for keys with no primary hit, a fallback
    tries last_name + first_name + zip LIKE first-4. Batched via OUTER APPLY
    (zip-indexed TOP-1 seek per key). Built by concatenation (no params), so the
    LIKE '%' is a single literal; single quotes escaped in the VALUES list."""
    keys = sorted({(str(k[0]).strip(), str(k[1]).strip(), str(k[2]).strip(), str(k[3]).strip())
                   for k in keys})
    if not keys:
        return {}
    esc = lambda s: s.replace("'", "''")
    out = {}

    # Primary: batched OUTER APPLY — the exact-zip seek keeps it fast; the LIKE's
    # only filter the zip's rows.
    prim = [k for k in keys if k[0] and k[2] and k[3]]
    if prim:
        values = ",".join("('%s','%s','%s','%s')" % (esc(a), esc(b), esc(c), esc(d))
                          for (a, b, c, d) in prim)
        sql = ("SELECT k.ln, k.fn, k.ad, k.z, o.email, o.primary_phone, o.[year], o.make "
               "FROM (VALUES " + values + ") k(ln, fn, ad, z) "
               "OUTER APPLY (SELECT TOP 1 email, primary_phone, [year], make "
               "  FROM panafax..tbl_ownership WITH (NOLOCK) "
               "  WHERE zip = CAST(k.z AS varchar(20)) "
               "    AND last_name LIKE LEFT(CAST(k.ln AS varchar(100)), 5) + '%' "
               "    AND address1 LIKE LEFT(CAST(k.ad AS varchar(200)), 8) + '%' "
               "  ORDER BY last_seen DESC) o")
        try:
            for r in dlr.query(sql):
                out[(r["ln"], r["fn"], r["ad"], r["z"])] = _own_row_flags(r)
        except Exception:
            pass

    # Fallback for keys the primary didn't match: last + first name + zip prefix (4).
    # Done per-row with a parameterized query so the zip-prefix LIKE is a literal
    # (sargable seek) — a correlated APPLY with a dynamic prefix can't use the index.
    for k in keys:
        if not (k[0] and k[1] and k[3]) or any(out.get(k, ())):
            continue
        try:
            rows = dlr.query(_OWN_FALLBACK_ONE_SQL, {"l": k[0], "f": k[1], "z": k[3]})
            if rows:
                out[k] = _own_row_flags(rows[0])
        except Exception:
            pass
    return out


_OWN_FALLBACK_ONE_SQL = ("SELECT TOP 1 email, primary_phone, [year], make "
    "FROM panafax..tbl_ownership WITH (NOLOCK) "
    "WHERE last_name = CAST(%(l)s AS varchar(100)) "
    "AND first_name = CAST(%(f)s AS varchar(100)) "
    "AND zip LIKE LEFT(CAST(%(z)s AS varchar(20)), 4) + '%%' "
    "ORDER BY last_seen DESC")


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
        omap = _ownership_batch([(r.get("_ln") or "", r.get("_fn") or "",
                                  r.get("_addr") or "", r.get("_zip") or "") for r in todo])
        for r in todo:
            t_em, t_ph = tmap.get(r.get("result_id"), (False, False))
            o_em, o_ph, yr, mk = omap.get(((r.get("_ln") or "").strip(), (r.get("_fn") or "").strip(),
                                           (r.get("_addr") or "").strip(), (r.get("_zip") or "").strip()),
                                          (False, False, "", ""))
            _TRIG_CONTACT_CACHE[r.get("result_id")] = (
                t_em or (r.get("consumer_id") in em_cids) or o_em,
                t_ph or (r.get("consumer_id") in ph_cids) or o_ph,
                " ".join(p for p in (yr, mk) if p),
            )
    for r in rows:
        r["has_email"], r["has_phone"], r["veh"] = _TRIG_CONTACT_CACHE.get(
            r.get("result_id"), (False, False, ""))


def trigger_leads(matching_customer=False, matching_dealer=False,
                  sent_status="unsent", limit=200):
    """The `limit` newest rows (by result_id, descending) from the CreditPipeline
    match_result feed joined to the ADF dealer and the sent ledger. Filters:
    matching_customer (c.last_name not null), matching_dealer (d.dealer_name not
    null), sent_status = unsent|sent|all.

    Ordering note: the 4-table cross-linked-server join is only fast with
    result_id ASC (it streams the remote clustered index); ORDER BY result_id DESC
    collapses the plan into a 180s+ timeout. So we fetch ascending up to a
    generous cap, then sort newest-first and slice to `limit` in Python."""
    # Never show rows with a blank consumer_id on match_result.
    # (matching_customer is applied in Python below — its Equifax view isn't joined.)
    conds = ["m.consumer_id IS NOT NULL"]
    if matching_dealer:
        conds.append("d.dealer_name IS NOT NULL")
    if sent_status == "unsent":
        conds.append("s.id IS NULL")
    elif sent_status == "sent":
        conds.append("s.id IS NOT NULL")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    fetch_cap = max(int(limit), 1000)   # fetch ascending (fast), then trim in Python
    try:
        rows = dlr.query(_TRIGGER_LEADS_SQL.format(limit=fetch_cap, where=where))
    except Exception:
        return []
    # Newest first.
    rows.sort(key=lambda r: int(r["result_id"]) if r.get("result_id") is not None else -1,
              reverse=True)
    if len(rows) >= fetch_cap:
        LOG.warning("trigger_leads hit the fetch cap (%d); newest rows beyond it are "
                    "hidden — raise the cap or pre-filter by result_id.", fetch_cap)
    # Equifax consumer name (eq_first/eq_last), fetched separately and merged.
    emap = _equifax_name_map([r.get("consumer_id") for r in rows])
    for r in rows:
        cid = r.get("consumer_id")
        r["eq_first"], r["eq_last"] = (emap.get(int(cid)) if cid is not None else None) or (None, None)
    if matching_customer:
        # "Matching customer" = consumer_id resolved to an Equifax consumer record.
        rows = [r for r in rows if r.get("eq_last")]
    # Keep only the top `limit` (newest), after any customer filter.
    rows = rows[:int(limit)]
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
        r["_fn"] = (r.get("cr_first") or r.get("eq_first") or payload.get("first_name") or "").strip()
        r["_addr"] = (payload.get("address_line_1") or "").strip()
        r["_zip"] = (r.get("consumer_zip") or payload.get("consumer_zip") or "").strip()
    _annotate_contact_flags(rows)
    return rows


# ----- Trigger Funnel report (Equifax trigger -> matched -> enriched) -----

_CP_LS = "[10.1.4.8].[CreditPipeline].[dbo]"

# Fast single-pass counts for the funnel stages that are pure trigger/join facts:
# total triggers, matched to one of our dealers, a credit score present, and any
# "estimated" credit/loan value present (the Estimated* columns).
_FUNNEL_AGG_SQL = ("""SELECT
  COUNT(*) AS total_triggers,
  SUM(CASE WHEN m.consumer_id IS NOT NULL AND d.dealer_name IS NOT NULL THEN 1 ELSE 0 END) AS matched_dealer,
  SUM(CASE WHEN TRY_CAST(t.fico_auto_8 AS int) > 0
            OR TRY_CAST(t.fico_8 AS int) > 0 THEN 1 ELSE 0 END) AS has_score,
  SUM(CASE WHEN TRY_CAST(t.EstCurrentBalance AS float) > 0
            OR TRY_CAST(t.EstimatedPayment AS float) > 0
            OR TRY_CAST(t.EstimatedInterestRate AS float) > 0
            OR TRY_CAST(t.EstimatedAmountFinanced AS float) > 0
            OR TRY_CAST(t.EstimatedNumberOfRemainingPayments AS int) > 0
            OR TRY_CAST(t.EstimatedTermInMonths AS int) > 0 THEN 1 ELSE 0 END) AS has_estimate
FROM {cp}.[match_result] m
LEFT JOIN {cp}.[vw_EquifaxConsumerRecordTriggers] t ON t.result_id = m.result_id
LEFT JOIN {cp}.[retailer] r ON r.retailer_id = m.retailer_id
LEFT JOIN dlrPro.dbo.dealers d ON d.dealer_id = r.retailer_code
WHERE m.returned_at >= %(start)s""").format(cp=_CP_LS)

# Identity rows for the enriched stages (phone/email/vehicle). Name/address/zip
# come from customer_record when present, else the matched_payload JSON.
_FUNNEL_ROWS_SQL = ("""SELECT TOP 5000
  m.result_id, m.consumer_id, m.consumer_zip,
  c.first_name AS cr_first, c.last_name AS cr_last,
  CAST(m.matched_payload AS NVARCHAR(MAX)) AS matched_payload
FROM {cp}.[match_result] m
LEFT JOIN {cp}.[customer_record] c ON c.customer_record_id = m.customer_record_id
WHERE m.returned_at >= %(start)s
ORDER BY m.result_id ASC""").format(cp=_CP_LS)

_MATCHED_BY_DAY_SQL = ("""SELECT CONVERT(varchar(10), m.returned_at, 120) AS day, COUNT(*) AS n
FROM {cp}.[match_result] m
JOIN {cp}.[retailer] r ON r.retailer_id = m.retailer_id
JOIN dlrPro.dbo.dealers d ON d.dealer_id = r.retailer_code
WHERE m.returned_at >= %(start)s
GROUP BY CONVERT(varchar(10), m.returned_at, 120)""").format(cp=_CP_LS)

_SENT_BY_DAY_SQL = """SELECT CONVERT(varchar(10), created, 120) AS day, COUNT(*) AS n
FROM dlrPro.dbo.[sent]
WHERE created >= %(start)s
GROUP BY CONVERT(varchar(10), created, 120)"""

FUNNEL_WINDOWS = ("month", "30d")


def _funnel_window_start(window):
    """Start date for a funnel window: 'month' = first of this calendar month
    (default), '30d' = 30 days ago."""
    today = date.today()
    if window == "30d":
        return today - timedelta(days=30)
    return today.replace(day=1)


def pipeline_funnel(window="month"):
    """Trigger-pipeline funnel counts over the window. total/matched/score/estimate
    are exact single-pass SQL counts; phone/email/vehicle reuse the Trigger Leads
    enrichment (3 sources, payload identity) so they match the page's PH/EM/VEH."""
    start = _funnel_window_start(window).isoformat()
    try:
        agg = dlr.query(_FUNNEL_AGG_SQL, {"start": start}, timeout=120)[0]
    except Exception:
        return None
    try:
        rows = dlr.query(_FUNNEL_ROWS_SQL, {"start": start}, timeout=120)
    except Exception:
        rows = []
    for r in rows:
        try:
            payload = json.loads(r.get("matched_payload") or "{}") or {}
        except (ValueError, TypeError):
            payload = {}
        r["_ln"] = (r.get("cr_last") or payload.get("last_name") or "").strip()
        r["_fn"] = (r.get("cr_first") or payload.get("first_name") or "").strip()
        r["_addr"] = (payload.get("address_line_1") or "").strip()
        r["_zip"] = (r.get("consumer_zip") or payload.get("consumer_zip") or "").strip()
    _annotate_contact_flags(rows)
    return {
        "window": window,
        "start": start,
        "total_triggers": int(agg.get("total_triggers") or 0),
        "matched_dealer": int(agg.get("matched_dealer") or 0),
        "has_phone": sum(1 for r in rows if r.get("has_phone")),
        "has_email": sum(1 for r in rows if r.get("has_email")),
        "has_vehicle": sum(1 for r in rows if (r.get("veh") or "").strip()),
        "has_score": int(agg.get("has_score") or 0),
        "has_estimate": int(agg.get("has_estimate") or 0),
    }


def pipeline_by_day(window="month"):
    """Per-day series over the window: triggers matched to a dealer (by returned_at)
    and leads sent (by the sent ledger's created). One entry per calendar day from
    the window start through today, zero-filled."""
    start_d = _funnel_window_start(window)
    start = start_d.isoformat()
    try:
        md = {r["day"]: int(r["n"]) for r in dlr.query(_MATCHED_BY_DAY_SQL, {"start": start}, timeout=120)}
    except Exception:
        md = {}
    try:
        sd = {r["day"]: int(r["n"]) for r in dlr.query(_SENT_BY_DAY_SQL, {"start": start}, timeout=120)}
    except Exception:
        sd = {}
    out, d, today = [], start_d, date.today()
    while d <= today:
        key = d.isoformat()
        out.append({"day": key, "matched": md.get(key, 0), "sent": sd.get(key, 0)})
        d += timedelta(days=1)
    return out


def _nice_max(v):
    """Smallest 'round' number >= v for a chart's y-axis (e.g. 72 -> 80)."""
    v = max(1, int(round(v)))
    step = 10 ** (len(str(v)) - 1)
    return int(-(-v // step) * step)   # ceil to the step


def by_day_chart_svg(days, width=760, height=300):
    """Inline SVG for the by-day series: bars = triggers matched to a dealer, an
    overlaid line = leads sent, on a shared y-axis. Returns markup (embed |safe).
    Styling comes from .chart CSS classes on the page."""
    L, R, T, B = 44, 16, 16, 46
    plot_w, plot_h = width - L - R, height - T - B
    n = max(1, len(days))
    ymax = _nice_max(max([0] + [d["matched"] for d in days] + [d["sent"] for d in days]))
    slot = plot_w / n
    barw = max(2.0, slot * 0.55)

    def x(i):
        return L + (i + 0.5) * slot

    def y(v):
        return T + plot_h * (1 - (v / ymax if ymax else 0))

    p = ['<svg viewBox="0 0 %d %d" class="chart" preserveAspectRatio="xMidYMid meet" '
         'xmlns="http://www.w3.org/2000/svg" role="img">' % (width, height)]
    for i in range(5):                       # 4 gridlines + labels
        v = ymax * i / 4.0
        gy = y(v)
        p.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" class="grid"/>' % (L, gy, width - R, gy))
        p.append('<text x="%.1f" y="%.1f" class="ylab">%d</text>' % (L - 6, gy + 3, round(v)))
    for i, d in enumerate(days):             # bars: matched
        h = plot_h * (d["matched"] / ymax if ymax else 0)
        p.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" class="bar">'
                 '<title>%s — %d matched</title></rect>'
                 % (x(i) - barw / 2, T + plot_h - h, barw, h, d["day"], d["matched"]))
    if n >= 2:                               # line: sent
        pts = " ".join("%.1f,%.1f" % (x(i), y(d["sent"])) for i, d in enumerate(days))
        p.append('<polyline points="%s" class="line"/>' % pts)
    for i, d in enumerate(days):
        p.append('<circle cx="%.1f" cy="%.1f" r="3.2" class="dot">'
                 '<title>%s — %d sent</title></circle>' % (x(i), y(d["sent"]), d["day"], d["sent"]))
    step = max(1, n // 12)                    # thin x labels when crowded
    for i, d in enumerate(days):
        if i % step == 0:
            p.append('<text x="%.1f" y="%.1f" class="xlab">%s</text>'
                     % (x(i), height - B + 16, d["day"][5:]))
    p.append('</svg>')
    return "".join(p)


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
