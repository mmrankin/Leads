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
from datetime import date

import dlrpro_db as dlr
import platform_db as pdb

LOG = logging.getLogger("pipeline_enrich")

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


def enrich_lead(lead):
    """Look up the owner's vehicle in tbl_ownership and enrich `lead` in place:
    vehicle year/make/model (also drives the ADF <vehicle interest="sell">
    block), phone/email when the lead has none, and an estimated-finance notes
    block appended to comments. Returns the matched row, or None."""
    m = match_owner(lead.get("last_name"), lead.get("address"), lead.get("zip"))
    if not m:
        return None

    year = str(m.get("year") or "").strip()
    make = (m.get("make") or "").strip()
    model = (m.get("model") or "").strip()
    vin = (m.get("vin") or "").strip()
    mileage = _f(m.get("mileage"))
    price = _f(m.get("price"))
    rate = _f(m.get("rate"))
    term = int(_f(m.get("term")))

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

    # Notes block.
    lines = []
    veh = " ".join(p for p in (year, make, model) if p)
    if veh:
        lines.append(f"Vehicle: {veh}")
    if vin:
        lines.append(f"VIN: {vin}")
    if term > 0:
        lines.append(f"Estimated term: {term}")
    if rate > 0:
        lines.append(f"Estimated rate: {rate:.1f}%")
    if mileage > 0:
        est_mi = int(round(mileage)) + 1000 * _months_since(m.get("last_seen"))
        lines.append(f"Estimated Mileage: {est_mi:,}")
    if price > 0:
        if rate > 0 and term > 0:
            pay = estimate_payment(price, rate, term)
        else:                       # price but no rate/term -> 60mo @ benchmark+2pts
            pay = estimate_payment(price, used_car_rate() + 2.0, 60)
        if pay > 0:
            lines.append(f"Estimated Payment: ${pay:,}")

    if lines:
        block = "\n".join(lines)
        existing = (lead.get("comments") or "").strip()
        lead["comments"] = (existing + "\n\n" + block).strip() if existing else block
    return m
