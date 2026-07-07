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

# Equifax trigger view on the CreditPipeline linked server (10.1.4.8), reached
# through the dlrPro connection via 4-part naming. Keyed by result_id (one row
# per match_result — better coverage than consumer_id). Its "Estimated*" finance
# columns are aliased back to the field names the notes builder already uses.
# VIN/Make/Model/Year are usually NULL here (tbl_ownership fills the vehicle).
_EQUIFAX_SQL = """SELECT TOP 1
    fico_auto_8 AS FICOAuto8, fico_8 AS FICO8,
    EstCurrentBalance AS RemainingBalance1,
    EstimatedPayment AS PaymentAmount1,
    EstimatedInterestRate AS AnnualPercentageRate1,
    EstimatedNumberOfRemainingPayments AS NumberOfRemainingPayments1,
    EstimatedTermInMonths AS TermInMonths1,
    EstimatedAmountFinanced AS AmountFinanced1,
    EstimatedOpenDate AS OpenDate1,
    EstimatedPayOffDate AS EstimatedPayOffDate1,
    VIN, Make, Model, Year,
    Email, CellPhone, HomePhone, WorkPhone, AppendedEmail, AppendedPhone
  FROM [{ls}].[{db}].[dbo].[vw_EquifaxConsumerRecordTriggers]
  WHERE result_id = %(rid)s"""


def match_equifax_trigger(result_id):
    """The Equifax trigger record for this match result_id, or None (→ the notes
    fall back to the tbl_ownership finance estimate). Returns None when there's
    no result_id, it isn't an int, the lookup errors, or the row isn't found."""
    if result_id in (None, ""):
        return None
    try:
        rid = int(result_id)
    except (TypeError, ValueError):
        return None
    try:
        rows = dlr.query(_EQUIFAX_SQL.format(ls=LINKED_SERVER, db=CP_DB), {"rid": rid})
    except Exception as e:                       # never let a lookup block a send
        LOG.warning("equifax trigger lookup failed (result_id=%s): %s", result_id, e)
        return None
    return rows[0] if rows else None


# Appended email / phone from the Equifax consumer tables — best available
# (prefer the lowest instance, but fall back to any so we don't miss a consumer
# who only has contact at instance > 1).
_EQUIFAX_EMAIL_SQL = """SELECT TOP 1 emailAddress
  FROM [{ls}].[{db}].[dbo].[ConsumerEmails]
  WHERE consumer_id = %(cid)s AND NULLIF(LTRIM(RTRIM(emailAddress)),'') IS NOT NULL
  ORDER BY instance"""
_EQUIFAX_PHONE_SQL = """SELECT TOP 1 telephoneNumber
  FROM [{ls}].[{db}].[dbo].[ConsumerTelephones]
  WHERE consumer_id = %(cid)s AND NULLIF(LTRIM(RTRIM(telephoneNumber)),'') IS NOT NULL
  ORDER BY telephoneTypeInstance, TelephoneType_ID DESC"""


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


def _first_nonempty(*vals):
    """First non-empty, stripped string from the candidates, or None."""
    for v in vals:
        s = str(v).strip() if v is not None else ""
        if s:
            return s
    return None


def _first_phone(*vals):
    """First candidate with >=10 digits, normalized to a 10-digit string (drops a
    leading US country code), or None."""
    for v in vals:
        d = _digits(v)
        if not d:
            continue
        if len(d) == 11 and d[0] == "1":
            d = d[1:]
        if len(d) >= 10:
            return d[:10]
    return None


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
    AND last_name LIKE LEFT(CAST(%(l)s AS varchar(100)), 5) + '%%'
    AND address1 LIKE LEFT(CAST(%(a)s AS varchar(200)), 8) + '%%'
  ORDER BY last_seen DESC OPTION (MAXDOP 2)"""

# Fallback when the address match misses: last + first name, zip prefix (first 4).
_OWNERSHIP_FALLBACK_SQL = """SELECT TOP 1
    year, make, model, vin, mileage, price, rate, term, primary_phone, email, last_seen
  FROM panafax..tbl_ownership WITH (NOLOCK)
  WHERE last_name = CAST(%(l)s AS varchar(100))
    AND first_name = CAST(%(f)s AS varchar(100))
    AND zip LIKE LEFT(CAST(%(z)s AS varchar(20)), 4) + '%%'
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


def match_owner(last_name, address, zip_code, first_name=None):
    """Most-recent tbl_ownership row. Primary: exact zip + last_name LIKE first-5
    + address1 LIKE first-8. If that misses and a first name is given, fall back
    to last_name + first_name + zip LIKE first-4. Returns the row, or None."""
    ln = (last_name or "").strip()
    ad = (address or "").strip()
    z = (zip_code or "").strip()
    fn = (first_name or "").strip()
    if ln and ad and z:
        try:
            rows = dlr.query(_OWNERSHIP_SQL, {"z": z, "a": ad, "l": ln})
            if rows:
                return rows[0]
        except Exception as e:                   # never let a lookup block a send
            LOG.warning("ownership primary lookup failed (%s / %s / %s): %s", ln, ad, z, e)
    if ln and fn and z:
        try:
            rows = dlr.query(_OWNERSHIP_FALLBACK_SQL, {"z": z, "f": fn, "l": ln})
            if rows:
                return rows[0]
        except Exception as e:
            LOG.warning("ownership fallback lookup failed (%s / %s / %s): %s", ln, fn, z, e)
    return None


def _months_since(d):
    """Whole months from d's month to the current month (>= 0)."""
    if not (hasattr(d, "year") and hasattr(d, "month")):
        return 0
    today = date.today()
    return max(0, (today.year - d.year) * 12 + (today.month - d.month))


def _phone_str(v):
    """A clean 10-digit phone string from a tbl_ownership primary_phone that may be
    a float (6194846463.0), an int, or an already-formatted string
    ('619-484-6463' / '1-619-484-6463'). Returns '' when there aren't 10 usable
    digits. (Floats: drop the .0 before reading digits so it doesn't add a spurious
    trailing digit; strings: just strip non-digits.)"""
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        if v <= 0:
            return ""
        v = int(round(v))
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    if len(digits) == 11 and digits[0] == "1":   # strip US country code
        digits = digits[1:]
    return digits[:10] if len(digits) >= 10 else ""


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
        lines.append(f"Estimated Loan Balance: ${bal:,.0f}")
    pay = _f(v.get("PaymentAmount1"))
    if pay > 0:
        lines.append(f"Estimated Payment: ${pay:,.0f}/mo")
    apr = _f(v.get("AnnualPercentageRate1"))
    if apr > 0:
        lines.append(f"Estimated Interest Rate (APR): {apr:.2f}%")
    rem = _int(v.get("NumberOfRemainingPayments1"))
    if rem:
        lines.append(f"Estimated Payments Remaining: {rem}")
    term = _int(v.get("TermInMonths1"))
    if term:
        lines.append(f"Estimated Loan Term: {term} months")
    amt = _f(v.get("AmountFinanced1"))
    if amt > 0:
        lines.append(f"Estimated Amount Financed: ${amt:,.0f}")
    if v.get("OpenDate1"):
        lines.append(f"Estimated Loan Open Date: {v['OpenDate1']}")
    if v.get("EstimatedPayOffDate1"):
        lines.append(f"Estimated Payoff Date: {v['EstimatedPayOffDate1']}")
    return lines


def enrich_lead(lead, result_id=None, consumer_id=None):
    """Enrich `lead` in place:

      • the Equifax trigger view (matched by result_id): the credit/finance notes
        (credit score, loan balance, payment, APR, payments remaining, …) — the
        PREFERRED source. When there is no row, the notes fall back to the
        tbl_ownership finance estimate.
      • tbl_ownership (matched by last_name / address / zip): the vehicle
        (year/make/model + VIN) and mileage, plus phone/email as a contact source.
        The trigger view's own vehicle is used when present (usually NULL), else
        tbl_ownership.
      • phone + email are pulled from EVERY source (trigger view, tbl_ownership,
        and the Equifax consumer tables by consumer_id) so a lead sends whenever a
        contact exists anywhere. (This matches the Trigger Leads PH/EM flags. An
        earlier trigger-view-only policy silently no-contact-capped ~224 sendable
        leads — reverted 2026-07-07.)

    Name/address stay as the caller resolved them (record → matched_payload JSON).
    Returns the tbl_ownership row (or None)."""
    trig = match_equifax_trigger(result_id)
    m = match_owner(lead.get("last_name"), lead.get("address"), lead.get("zip"),
                    lead.get("first_name"))   # vehicle / mileage / finance + contact fallback

    # Vehicle: prefer the trigger view (usually NULL), else tbl_ownership.
    def _first(*vals):
        for val in vals:
            s = str(val).strip() if val is not None else ""
            if s:
                return s
        return ""
    year = _first(trig.get("Year") if trig else None, m.get("year") if m else None)
    make = _first(trig.get("Make") if trig else None, m.get("make") if m else None)
    model = _first(trig.get("Model") if trig else None, m.get("model") if m else None)
    vin = _first(trig.get("VIN") if trig else None, m.get("vin") if m else None)
    mileage = _f(m.get("mileage")) if m else 0.0

    # Vehicle -> the "used" sell/trade vehicle section of the ADF/XML.
    if year:
        lead["vehicle_year"] = year
    if make:
        lead["vehicle_make"] = make
    if model:
        lead["vehicle_model"] = model

    # Contact: pull email/phone from EVERY source (best first) so the poller can
    # send a lead whose contact lives in the ownership DB or the Equifax consumer
    # tables, not just the trigger view. Keep whatever the caller already resolved
    # (payload / customer_record) if present.
    contact = match_equifax_contact(consumer_id)   # appended email/phone (equifax..Consumer*)
    email = _first_nonempty(
        lead.get("email"),                             # payload / customer_record
        trig.get("Email") if trig else None,           # trigger view — consumer
        trig.get("AppendedEmail") if trig else None,   # trigger view — appended
        m.get("email") if m else None,                 # tbl_ownership
        contact.get("email"),                          # equifax..ConsumerEmails
    )
    if email:
        lead["email"] = email
    phone = _first_phone(
        lead.get("phone"),
        trig.get("CellPhone") if trig else None,
        trig.get("HomePhone") if trig else None,
        trig.get("WorkPhone") if trig else None,
        trig.get("AppendedPhone") if trig else None,
        _phone_str(m.get("primary_phone")) if m else None,   # tbl_ownership (float or formatted)
        contact.get("phone"),                                # equifax..ConsumerTelephones
    )
    if phone:
        lead["phone"] = phone

    # Notes: vehicle (VIN + year/make/model) + mileage, the credit/finance block
    # (trigger view first, else the tbl_ownership estimate), then appended contact.
    lines = []
    if vin:
        lines.append(f"VIN: {vin}")
    veh = " ".join(p for p in (year, make, model) if p)
    if veh:
        lines.append(f"Vehicle: {veh}")
    if mileage > 0:
        est_mi = int(round(mileage)) + (1000 * _months_since(m.get("last_seen")) if m else 0)
        lines.append(f"Estimated Mileage: {est_mi:,}")

    if trig:
        lines.extend(_equifax_finance_lines(trig))
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

    if lead.get("email"):
        lines.append(f"Email Address Appended: {lead['email']}")
    if lead.get("phone"):
        lines.append(f"Phone Number Appended: {_fmt_phone(lead['phone'])}")

    if lines:
        block = "\n".join(lines)
        existing = (lead.get("comments") or "").strip()
        lead["comments"] = (existing + "\n\n" + block).strip() if existing else block
    return m
