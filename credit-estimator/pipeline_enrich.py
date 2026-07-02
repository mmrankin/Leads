"""Enrich a Credit Pipeline lead with the matched vehicle-owner record from
`panafax..tbl_ownership` on 10.1.1.10 (reached through the same dlrPro
connection). For each lead we look up the owner by (last_name, address, zip) and
add: the current vehicle (year/make/model + VIN), phone/email when the lead has
none, and an estimated-finance notes block (term, rate, mileage, payment).

Perf note: tbl_ownership is ~340M rows indexed on zip. pymssql sends str params
as NVARCHAR, which forces an implicit-conversion full scan (~30s+). Casting each
param to VARCHAR restores the index seek (~0.2s) — hence the CAST(...) below.
"""

import logging
import os
from datetime import date

import dlrpro_db as dlr
import platform_db as pdb
from pipeline_source import LINKED_SERVER, DB as CP_DB

LOG = logging.getLogger("pipeline_enrich")

# The Equifax curated DB on the same (10.1.4.8) linked server — appended
# consumer email/phone, keyed by the same Consumer_ID as the credit view.
EQUIFAX_DB = os.environ.get("CREDITPIPELINE_EQUIFAX_DB", "equifax")

# Equifax consumer-credit view on the CreditPipeline linked server (10.1.4.8),
# reached through the dlrPro connection via 4-part naming (same as the source
# feed). Keyed by Consumer_ID (= match_result.consumer_id). Trade-1 = the
# consumer's primary/most-relevant auto tradeline.
_EQUIFAX_SQL = """SELECT TOP 1
    FICO8, FICOAuto8, OpenTrade1, LoanTrade1, OpenDate1, AmountFinanced1,
    RemainingBalance1, TermInMonths1, PaymentAmount1, EstimatedPayOffDate1,
    NumberOfRemainingPayments1, AnnualPercentageRate1, PercentagePaid1, NumberOfLatePayments1
  FROM [{ls}].[{db}].[dbo].[vw_EquifaxConsumerRecord]
  WHERE Consumer_ID = %(cid)s"""


def match_equifax_consumer(consumer_id):
    """The Equifax consumer-credit record for this Consumer_ID, or None. Returns
    None (→ waterfall falls through to tbl_ownership) when there is no
    consumer_id, it isn't an int, the lookup errors, or the consumer isn't in
    the view."""
    if consumer_id in (None, ""):
        return None
    try:
        cid = int(consumer_id)
    except (TypeError, ValueError):
        return None
    try:
        rows = dlr.query(_EQUIFAX_SQL.format(ls=LINKED_SERVER, db=CP_DB), {"cid": cid})
    except Exception as e:                       # never let a lookup block a send
        LOG.warning("equifax view lookup failed (consumer_id=%s): %s", consumer_id, e)
        return None
    return rows[0] if rows else None


# Appended email / phone from the Equifax consumer tables (instance/type 1).
_EQUIFAX_EMAIL_SQL = """SELECT TOP 1 emailAddress
  FROM [{ls}].[{db}].[dbo].[ConsumerEmails]
  WHERE consumer_id = %(cid)s AND instance = 1"""
_EQUIFAX_PHONE_SQL = """SELECT TOP 1 telephoneNumber
  FROM [{ls}].[{db}].[dbo].[ConsumerTelephones]
  WHERE consumer_id = %(cid)s AND telephoneTypeInstance = 1
  ORDER BY TelephoneType_ID DESC"""


def _digits(v):
    """Digits-only string from a raw phone value, or None."""
    if v is None:
        return None
    s = "".join(ch for ch in str(v) if ch.isdigit())
    return s or None


def _fmt_phone(v):
    """###-###-#### for a 10-digit number; otherwise the digits as-is (or None)."""
    d = _digits(v)
    if d and len(d) == 10:
        return f"{d[:3]}-{d[3:6]}-{d[6:]}"
    return d


def match_equifax_contact(consumer_id):
    """Appended email + phone for this Consumer_ID from the Equifax consumer
    tables (equifax..ConsumerEmails / ConsumerTelephones). Returns
    {"email": str|None, "phone": <digits>|None}; empty when there's no
    consumer_id or a lookup errors."""
    out = {"email": None, "phone": None}
    if consumer_id in (None, ""):
        return out
    try:
        cid = int(consumer_id)
    except (TypeError, ValueError):
        return out
    try:
        rows = dlr.query(_EQUIFAX_EMAIL_SQL.format(ls=LINKED_SERVER, db=EQUIFAX_DB), {"cid": cid})
        if rows:
            out["email"] = (rows[0].get("emailAddress") or "").strip() or None
    except Exception as e:
        LOG.warning("equifax email lookup failed (consumer_id=%s): %s", consumer_id, e)
    try:
        rows = dlr.query(_EQUIFAX_PHONE_SQL.format(ls=LINKED_SERVER, db=EQUIFAX_DB), {"cid": cid})
        if rows:
            out["phone"] = _digits(rows[0].get("telephoneNumber"))
    except Exception as e:
        LOG.warning("equifax phone lookup failed (consumer_id=%s): %s", consumer_id, e)
    return out

# Benchmark used-car finance rate (%), cached daily in platform_settings. Used
# only for the payment fallback (a matched record with a price but no rate/term).
_RATE_KEY = "pipeline_used_car_rate"
_RATE_DATE_KEY = "pipeline_used_car_rate_date"
DEFAULT_USED_RATE = 10.0            # sane fallback if the benchmark is unavailable
_RATE_MIN, _RATE_MAX = 1.0, 30.0    # sanity band for the computed benchmark

_OWNERSHIP_SQL = """SELECT TOP 1
    year, make, model, vin, mileage, price, rate, term, primary_phone, email, last_seen
  FROM panafax..tbl_ownership WITH (NOLOCK)
  WHERE zip = CAST(%(z)s AS varchar(20))
    AND address1 = CAST(%(a)s AS varchar(200))
    AND last_name = CAST(%(l)s AS varchar(100))
  ORDER BY last_seen DESC OPTION (MAXDOP 2)"""

# Average finance rate for recently-sold used vehicles — the "current used-car
# rate from panasight". Heavy (~45s), so it runs at most once a day (see below).
_BENCHMARK_SQL = """SELECT AVG(CAST(NULLIF(rate,0) AS float)) AS avg_rate
  FROM panafax..tbl_ownership WITH (NOLOCK)
  WHERE sold_date >= DATEADD(month,-6,GETDATE()) AND rate > 0
    AND TRY_CAST(year AS int) < YEAR(sold_date)
  OPTION (MAXDOP 2)"""


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def match_owner(last_name, address, zip_code):
    """Most-recent tbl_ownership row for (last_name, address, zip), or None."""
    ln = (last_name or "").strip()
    ad = (address or "").strip()
    z = (zip_code or "").strip()
    if not (ln and ad and z):
        return None
    try:
        rows = dlr.query(_OWNERSHIP_SQL, {"z": z, "a": ad, "l": ln})
    except Exception as e:                       # never let a lookup block a send
        LOG.warning("ownership lookup failed (%s / %s / %s): %s", ln, ad, z, e)
        return None
    return rows[0] if rows else None


def _months_since(d):
    """Whole months from d's month to the current month (>= 0)."""
    if not (hasattr(d, "year") and hasattr(d, "month")):
        return 0
    today = date.today()
    return max(0, (today.year - d.year) * 12 + (today.month - d.month))


def _phone_str(v):
    """primary_phone is stored as a float; render it as a 10+ digit string."""
    n = _f(v)
    if n <= 0:
        return ""
    digits = str(int(round(n)))
    return digits if len(digits) >= 10 else ""


def used_car_rate():
    """Current benchmark used-car finance rate (%), cached daily in
    platform_settings. Falls back to the last cached value, then to
    DEFAULT_USED_RATE, if the (heavy) benchmark query is unavailable."""
    today = date.today().isoformat()
    cached = _f(pdb.get_setting(_RATE_KEY))
    if cached and pdb.get_setting(_RATE_DATE_KEY) == today:
        return cached
    try:
        row = dlr.query(_BENCHMARK_SQL, timeout=120)
        rate = _f(row[0].get("avg_rate")) if row else 0.0
        if _RATE_MIN <= rate <= _RATE_MAX:
            pdb.set_setting(_RATE_KEY, f"{rate:.2f}")
            pdb.set_setting(_RATE_DATE_KEY, today)
            return rate
    except Exception as e:
        LOG.warning("used-car rate benchmark failed: %s", e)
    return cached or DEFAULT_USED_RATE


def estimate_payment(price, annual_rate_pct, term_months):
    """Amortized monthly payment, rounded to the nearest $10 (0 if not derivable)."""
    P = _f(price)
    r = _f(annual_rate_pct) / 100.0 / 12.0
    n = int(_f(term_months))
    if P <= 0 or n <= 0:
        return 0
    m = P / n if r <= 0 else P * r / (1 - (1 + r) ** (-n))
    return int(round(m / 10.0) * 10)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _equifax_finance_lines(v):
    """Notes lines from the Equifax view row — the consumer's credit score plus
    their current auto loan (trade 1: balance, payment, APR, months remaining,
    etc.). Only non-empty values are shown."""
    lines = []
    score = v.get("FICOAuto8") or v.get("FICO8")   # auto-enhanced FICO 8, else general FICO 8
    if score:
        lines.append(f"Estimated Credit Score: {int(score)}")
    bal = _f(v.get("RemainingBalance1"))
    if bal > 0:
        lines.append(f"Loan balance: ${bal:,.0f}")
    pay = _f(v.get("PaymentAmount1"))
    if pay > 0:
        lines.append(f"Payment: ${pay:,.0f}/mo")
    apr = _f(v.get("AnnualPercentageRate1"))
    if apr > 0:
        lines.append(f"Interest rate (APR): {apr:.2f}%")
    rem = _int(v.get("NumberOfRemainingPayments1"))
    if rem:
        lines.append(f"Months remaining: {rem}")
    term = _int(v.get("TermInMonths1"))
    if term:
        lines.append(f"Loan term: {term} months")
    amt = _f(v.get("AmountFinanced1"))
    if amt > 0:
        lines.append(f"Amount financed: ${amt:,.0f}")
    if v.get("OpenDate1"):
        lines.append(f"Loan opened: {v['OpenDate1']}")
    if v.get("EstimatedPayOffDate1"):
        lines.append(f"Est. payoff: {v['EstimatedPayOffDate1']}")
    pct = _f(v.get("PercentagePaid1"))
    if pct > 0:
        lines.append(f"Percent paid: {pct:.0f}%")
    late = _int(v.get("NumberOfLatePayments1"))
    if late:
        lines.append(f"Late payments: {late}")
    return lines


def enrich_lead(lead, consumer_id=None):
    """Enrich `lead` in place from two sources (a waterfall):

      • tbl_ownership (matched by last_name / address / zip): vehicle year/make/
        model, VIN, mileage, and phone/email when the lead has none. Always used
        for these — the vehicle identity/contact always comes from here.
      • the Equifax view (matched by consumer_id): the credit/finance notes
        (credit score, loan balance, payment, APR, months remaining, …). PREFERRED
        for those fields. When the record has no consumer_id or the consumer isn't
        in the view, we fall through to the tbl_ownership finance estimate instead.

    Name/address stay as the caller resolved them (record → matched_payload JSON).
    Returns the tbl_ownership row (or None)."""
    m = match_owner(lead.get("last_name"), lead.get("address"), lead.get("zip"))
    equifax = match_equifax_consumer(consumer_id)
    contact = match_equifax_contact(consumer_id)   # appended email/phone (equifax..Consumer*)

    year = make = model = vin = ""
    mileage = 0.0
    if m:
        year = str(m.get("year") or "").strip()
        make = (m.get("make") or "").strip()
        model = (m.get("model") or "").strip()
        vin = (m.get("vin") or "").strip()
        mileage = _f(m.get("mileage"))

        # Vehicle -> the "used" sell/trade vehicle section of the ADF/XML.
        if year:
            lead["vehicle_year"] = year
        if make:
            lead["vehicle_make"] = make
        if model:
            lead["vehicle_model"] = model

        # Contact -> fill only when the lead is missing it.
        if not (lead.get("phone") or "").strip():
            ph = _phone_str(m.get("primary_phone"))
            if ph:
                lead["phone"] = ph
        if not (lead.get("email") or "").strip():
            em = (m.get("email") or "").strip()
            if em:
                lead["email"] = em

    # Appended Equifax contact fills the ADF contact block when still empty.
    if not (lead.get("email") or "").strip() and contact.get("email"):
        lead["email"] = contact["email"]
    if not (lead.get("phone") or "").strip() and contact.get("phone"):
        lead["phone"] = contact["phone"]

    # Notes: vehicle (VIN + year/make/model) + mileage (always from tbl_ownership),
    # the credit/finance block (Equifax view first, else the tbl_ownership
    # estimate), then the appended Equifax email/phone.
    lines = []
    if vin:
        lines.append(f"VIN: {vin}")
    veh = " ".join(p for p in (year, make, model) if p)
    if veh:
        lines.append(f"Vehicle: {veh}")
    if mileage > 0:
        est_mi = int(round(mileage)) + (1000 * _months_since(m.get("last_seen")) if m else 0)
        lines.append(f"Estimated Mileage: {est_mi:,}")

    if equifax:
        lines.extend(_equifax_finance_lines(equifax))
    elif m:
        rate = _f(m.get("rate"))
        term = int(_f(m.get("term")))
        price = _f(m.get("price"))
        if term > 0:
            lines.append(f"Estimated term: {term}")
        if rate > 0:
            lines.append(f"Estimated rate: {rate:.1f}%")
        if price > 0:
            if rate > 0 and term > 0:
                pay = estimate_payment(price, rate, term)
            else:                       # price but no rate/term -> 60mo @ benchmark+2pts
                pay = estimate_payment(price, used_car_rate() + 2.0, 60)
            if pay > 0:
                lines.append(f"Estimated Payment: ${pay:,}")

    if contact.get("email"):
        lines.append(f"Email Address Appended: {contact['email']}")
    if contact.get("phone"):
        lines.append(f"Phone Number Appended: {_fmt_phone(contact['phone'])}")

    if lines:
        block = "\n".join(lines)
        existing = (lead.get("comments") or "").strip()
        lead["comments"] = (existing + "\n\n" + block).strip() if existing else block
    return m
