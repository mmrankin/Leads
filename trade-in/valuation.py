"""Trade-in valuation engine.

Pipeline:
  1. Resolve a squish VIN (positions 1-8 + 10-11) for the trade-in's
     year/make/model(/trim) via a TOP-1 lookup in vin_decode.vin_data1.
  2. Pull comps by squish:
       - retail   -> Inventory.dbo.tbl_inventory (current listing prices)
       - wholesale -> auction.dbo.vehicles over the last 60 days (prefer mmr,
                      else hammer price), reached via OPENQUERY linked server.
  3. Mileage adjustment: fit value-vs-mileage from the comps (least squares);
     fall back to the dealer's per-mile rate when the fit is unreliable.
  4. Apply the dealer's per-condition adjustments (in $ or %).
  5. Return a low-high range whose spread is the dealer's configured spread.

All SQL uses WITH (NOLOCK) + a MAXDOP cap. Reuses the VIN_DB_* connection env.
"""

import os

try:
    import pymssql
except ImportError:
    pymssql = None

import db

AUCTION_LINKED_SERVER = "10.1.2.17"
WHOLESALE_WINDOW_DAYS = 60
MIN_REGRESSION_POINTS = 8


def is_enabled():
    return bool(pymssql and os.environ.get("VIN_DB_SERVER"))


def _connect(timeout=60):
    return pymssql.connect(
        server=os.environ["VIN_DB_SERVER"],
        user=os.environ.get("VIN_DB_USER"),
        password=os.environ.get("VIN_DB_PASSWORD"),
        database=os.environ.get("VIN_DB_DATABASE", "vin_decode"),
        timeout=timeout,
        login_timeout=10,
    )


# VIN position-10 model-year code (post-2000 cycle; covers realistic trade-ins).
YEAR_CODE = {y: c for y, c in zip(
    list(range(2001, 2031)),
    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "A", "B", "C", "D", "E", "F",
     "G", "H", "J", "K", "L", "M", "N", "P", "R", "S", "T", "V", "W", "X", "Y"])}

MAX_PREFIXES = 100  # keep the top-N most common first-8 prefixes per make/model


def _vin_prefix_clause(prefixes, col="vin"):
    """Return (sql_fragment, params) matching any of the first-8 VIN prefixes.
    Each `col LIKE 'PREFIX%'` is a sargable index seek, so many prefixes are
    still fast. Parameterized (safe for the same-server Inventory query)."""
    prefixes = prefixes[:MAX_PREFIXES]
    frag = "(" + " OR ".join(f"{col} LIKE %s" for _ in prefixes) + ")"
    return frag, [p + "%" for p in prefixes]


def get_vin8_prefixes(cur, make, model):
    """All distinct first-8 VIN prefixes for a make/model. Local cache first;
    on a miss, a single DISTINCT scan (~3s) then cached."""
    cached = db.get_vin8_set(make, model)
    if cached:
        return cached
    # Top-N most common first-8 prefixes for this make/model.
    cur.execute(
        f"SELECT TOP {MAX_PREFIXES} LEFT(VIN,8) s8 FROM vin_data1 WITH (NOLOCK) "
        "WHERE make=%s AND model=%s AND VIN IS NOT NULL "
        "GROUP BY LEFT(VIN,8) ORDER BY COUNT_BIG(*) DESC OPTION (MAXDOP 1)",
        (make, model),
    )
    prefixes = [r[0] for r in cur.fetchall() if r[0]]
    if prefixes:
        db.bulk_put_vin8([(make, model, p) for p in prefixes])
    return prefixes


def build_vin8_map():
    """Populate the make/model -> first-8-prefix set for EVERY make/model in one
    DISTINCT scan of vin_data1. Slow once (~30-90s); makes lookups instant."""
    if not is_enabled():
        return 0
    con = _connect(timeout=600)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT make, model, LEFT(VIN,8) s8, COUNT_BIG(*) c FROM vin_data1 WITH (NOLOCK) "
            "WHERE year > 0 AND make <> '' AND model <> '' AND VIN IS NOT NULL "
            "GROUP BY make, model, LEFT(VIN,8) OPTION (MAXDOP 1)"
        )
        scanned = [(r[0], r[1], r[2], int(r[3] or 0)) for r in cur.fetchall() if r[2]]
    finally:
        con.close()
    # Keep the top-N most common prefixes per make/model.
    by_mm = {}
    for make, model, s8, c in scanned:
        by_mm.setdefault((make, model), []).append((c, s8))
    rows = []
    for (make, model), lst in by_mm.items():
        lst.sort(reverse=True)
        rows.extend((make, model, s8) for _, s8 in lst[:MAX_PREFIXES])
    db.bulk_put_vin8(rows)
    return len(rows)


def _get_squish(cur, year, make, model, trim=None):
    """Return (s8, s2) for a representative VIN, or None.

    Checks the local squish cache first (instant); on a miss, runs the live
    TOP-1 lookup against vin_data1 (a full scan of a multi-million-row table —
    slow, ~6-30s) and caches the result so it's instant next time. Run
    build_squish_cache() once to pre-populate every YMM and avoid the slow path.
    """
    cached = db.get_cached_squish(year, make, model)
    if cached:
        return cached

    base = ("SELECT TOP 1 VIN FROM vin_data1 WITH (NOLOCK) "
            "WHERE year=%s AND make=%s AND model=%s")
    row = None
    if trim:
        cur.execute(base + " AND trim=%s", (year, make, model, trim))
        row = cur.fetchone()
    if not row:
        cur.execute(base, (year, make, model))
        row = cur.fetchone()
    if not row:
        return None
    vin = row[0]
    s8, s2 = vin[0:8], vin[9:11]
    db.put_cached_squish(year, make, model, s8, s2)
    return s8, s2


def build_squish_cache():
    """Populate the local squish cache for EVERY year/make/model in one grouped
    scan of vin_data1. Slow once (~30-90s); makes all later lookups instant.
    Returns the number of rows cached.
    """
    if not is_enabled():
        return 0
    con = _connect(timeout=600)
    try:
        cur = con.cursor()
        # MIN(VIN) yields one real representative VIN per group, so s8 + s2 are
        # taken from the SAME vin (not mismatched across rows).
        cur.execute(
            "SELECT year, make, model, MIN(VIN) vin "
            "FROM vin_data1 WITH (NOLOCK) "
            "WHERE year > 0 AND make <> '' AND model <> '' "
            "GROUP BY year, make, model OPTION (MAXDOP 1)"
        )
        rows = [(r[0], r[1], r[2], r[3][0:8], r[3][9:11])
                for r in cur.fetchall() if r[3]]
    finally:
        con.close()
    db.bulk_put_squish(rows)
    return len(rows)


# regression stat columns shared by both comp sets:
# n, avg_value, avg_miles, Sx, Sy, Sxy, Sxx
_EMPTY_STATS = (0, None, None, 0, 0, 0, 0)


def _retail_stats(cur, prefixes, yearcode):
    """Retail comps from tbl_inventory: any of the make/model first-8 prefixes,
    same model year (VIN position 10)."""
    if not prefixes or not yearcode:
        return _EMPTY_STATS
    frag, params = _vin_prefix_clause(prefixes)
    cur.execute(
        f"""
        SELECT COUNT_BIG(*) n,
               AVG(CAST(price AS float)) avg_value,
               AVG(CAST(mileage AS float)) avg_miles,
               SUM(CAST(mileage AS float)) sx,
               SUM(CAST(price AS float)) sy,
               SUM(CAST(mileage AS float)*CAST(price AS float)) sxy,
               SUM(CAST(mileage AS float)*CAST(mileage AS float)) sxx
        FROM Inventory.dbo.tbl_inventory WITH (NOLOCK)
        WHERE {frag} AND SUBSTRING(vin,10,1)=%s AND price>0 AND mileage>0
        OPTION (MAXDOP 1)
        """,
        params + [yearcode],
    )
    return cur.fetchone()


def _wholesale_stats(cur, make, model, year):
    """Wholesale comps from auction.dbo.vehicles, matched on its own make/model/
    year columns (richer + more reliable than VIN matching), prefer mmr."""
    mk = (make or "").replace("'", "''")
    md = (model or "").replace("'", "''")
    inner = (
        "SELECT COUNT_BIG(*) n, AVG(CAST(val AS float)) avg_value, "
        "AVG(CAST(mileage AS float)) avg_miles, SUM(CAST(mileage AS float)) sx, "
        "SUM(CAST(val AS float)) sy, SUM(CAST(mileage AS float)*CAST(val AS float)) sxy, "
        "SUM(CAST(mileage AS float)*CAST(mileage AS float)) sxx "
        "FROM (SELECT mileage, COALESCE(NULLIF(mmr,0),price) val "
        "FROM auction.dbo.vehicles WITH (NOLOCK) "
        f"WHERE make=''{mk}'' AND model LIKE ''{md}%'' AND year={int(year)} "
        f"AND created BETWEEN GETDATE()-{WHOLESALE_WINDOW_DAYS} AND GETDATE() "
        "AND mileage>0 AND COALESCE(NULLIF(mmr,0),price)>0) t"
    )
    cur.execute(f"SELECT * FROM OPENQUERY([{AUCTION_LINKED_SERVER}], '{inner}')")
    return cur.fetchone()


def build_inv_count_cache():
    """Refresh the local tbl_inventory count-per-first-8-prefix cache in one
    GROUP BY scan. Lets the "Similar Vehicles for Sale" metric be an instant
    local sum instead of a slow 100-way OR scan per request. Run periodically."""
    if not is_enabled():
        return 0
    con = _connect(timeout=600)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT LEFT(vin,8) s8, COUNT_BIG(*) c FROM Inventory.dbo.tbl_inventory "
            "WITH (NOLOCK) WHERE vin IS NOT NULL AND LEN(vin)>=8 "
            "GROUP BY LEFT(vin,8) OPTION (MAXDOP 1)"
        )
        rows = [(r[0], int(r[1] or 0)) for r in cur.fetchall() if r[0]]
    finally:
        con.close()
    db.replace_inv_counts(rows)
    return len(rows)


def count_similar_inventory(prefixes):
    """"Similar Vehicles for Sale": # of same make/model (all years) in
    tbl_inventory, summed from the local prefix-count cache (instant). Falls
    back to 0 if the cache hasn't been built yet."""
    return db.get_inv_count(prefixes)


def _slope(n, sx, sy, sxy, sxx):
    """Least-squares slope b and intercept a for value ~ a + b*mileage."""
    if not n or n < 2:
        return None, None
    denom = n * sxx - sx * sx
    if denom == 0:
        return None, None
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    return a, b


def _to_int(x, default=None):
    try:
        return int(float(str(x).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def _to_float(x, default=0.0):
    """Float from a setting that may be None/blank/'1,500' — `default` on failure
    (so a dealer who hasn't set a margin still values at 100% / no deduction)."""
    if x is None or str(x).strip() == "":
        return default
    try:
        return float(str(x).replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return default


CONDITION_MAP = [
    # (settings_key, lead_field, triggers_when, label)
    ("adj_keys_1", "num_keys", lambda v: v == "1", "Only 1 key"),
    ("adj_keys_3plus", "num_keys", lambda v: v == "3+", "3+ keys"),
    ("adj_unrepaired_damage", "unrepaired_damage", lambda v: v == "Y", "Un-repaired damage"),
    ("adj_engine_light", "engine_light", lambda v: v == "Y", "Engine light on"),
    ("adj_airbag_light", "airbag_light", lambda v: v == "Y", "Airbag light on"),
    ("adj_brake_light", "brake_light", lambda v: v == "Y", "Brake light on"),
    ("adj_aftermarket_exhaust", "aftermarket_exhaust", lambda v: v == "Y", "Aftermarket exhaust"),
    ("adj_aftermarket_engine", "aftermarket_engine", lambda v: v == "Y", "Aftermarket engine components"),
    ("adj_aftermarket_stereo", "aftermarket_stereo", lambda v: v == "Y", "Aftermarket stereo / electronics"),
]


def compute_value(lead, settings):
    """Compute the trade-in value for a lead dict using dealer settings.

    Returns a result dict; result['ok'] is False (with result['reason']) when no
    value could be produced (e.g. no comps, or lookups disabled).
    """
    result = {"ok": False, "reason": None, "base_source": settings.get("base_source"),
              "comp_count": 0, "adjustments": []}
    if not is_enabled():
        result["reason"] = "Vehicle data lookups are not configured."
        return result

    year = _to_int(lead.get("vehicle_year"))
    make = (lead.get("vehicle_make") or "").strip()
    model = (lead.get("vehicle_model") or "").strip()
    trim = (lead.get("vehicle_trim") or "").strip() or None
    if not (year and make and model):
        result["reason"] = "Incomplete vehicle (year/make/model)."
        return result

    requested = settings.get("base_source", "retail")
    yearcode = YEAR_CODE.get(year)

    def _usable(st):
        return st and int(st[0] or 0) > 0 and st[1]

    order = (["wholesale", "retail"] if requested == "wholesale"
             else ["retail", "wholesale"])
    stats, used = None, None
    try:
        con = _connect()
        try:
            cur = con.cursor()
            prefixes = get_vin8_prefixes(cur, make, model)
            # "Similar vehicles for sale" — same make/model family, all years.
            # Instant local sum from the prefix-count cache.
            try:
                result["similar_count"] = count_similar_inventory(prefixes)
            except Exception:
                result["similar_count"] = None
            # Try the dealer's preferred source first; only query the other if
            # the first has no comps (so we skip the extra query on the common
            # path, but rare cars still fall back and get a value).
            for s in order:
                cand = (_retail_stats(cur, prefixes, yearcode) if s == "retail"
                        else _wholesale_stats(cur, make, model, year))
                if _usable(cand):
                    stats, used = cand, s
                    break
        finally:
            con.close()
    except Exception as exc:  # SQL/network failure -> no value, but never crash
        result["reason"] = f"Comp lookup failed: {exc}"
        return result

    result["base_source"] = used or requested
    if used and used != requested:
        result["source_fallback"] = f"{requested}→{used}"
    if not stats:
        result["reason"] = "No comparable sales found for this vehicle."
        return result

    n, avg_value, avg_miles, sx, sy, sxy, sxx = stats
    n = int(n or 0)
    result["comp_count"] = n
    result["comp_avg_value"] = round(avg_value) if avg_value else None
    result["comp_avg_miles"] = round(avg_miles) if avg_miles else None

    cust_miles = _to_int(lead.get("miles"))

    # ----- mileage-adjusted base -----
    a, b = _slope(n, sx or 0, sy or 0, sxy or 0, sxx or 0)
    if cust_miles is None:
        base = avg_value
        result["mileage_method"] = "none (no customer mileage)"
    elif n >= MIN_REGRESSION_POINTS and b is not None and b < 0:
        base = a + b * cust_miles
        result["mileage_method"] = "regression"
        result["mileage_per_mile"] = round(b, 4)
    else:
        rate = float(settings.get("mileage_rate") or 0.12)
        base = avg_value + rate * ((avg_miles or 0) - cust_miles)
        result["mileage_method"] = "rate"
        result["mileage_per_mile"] = -rate
    # guard against pathological extrapolation
    base = max(base, 0.0)
    result["market_value"] = round(base)          # what the comps say, pre-margin

    # ----- dealer margin: (market x market_pct%) - flat_deduction -----
    # Applied BEFORE the condition adjustments, so the dealer can offer e.g. 90%
    # of market less $1,500 of reconditioning/dealer cost, and the condition
    # adjustments then move that number up or down.
    market_pct = _to_float(settings.get("market_pct"), 100.0)
    flat_deduction = _to_float(settings.get("flat_deduction"), 0.0)
    pct_value = base * (market_pct / 100.0)
    base = max(pct_value - flat_deduction, 0.0)
    result["market_pct"] = market_pct
    result["flat_deduction"] = round(flat_deduction)
    result["pct_value"] = round(pct_value)
    result["base_value"] = round(base)            # working base for adjustments

    # ----- condition adjustments -----
    unit = settings.get("adjustment_unit", "dollar")
    total_adj = 0.0
    for key, field, trig, label in CONDITION_MAP:
        val = (lead.get(field) or "").strip()
        if not trig(val):
            continue
        amt = float(settings.get(key) or 0)
        if amt == 0:
            continue
        dollars = base * (amt / 100.0) if unit == "percent" else amt
        total_adj += dollars
        result["adjustments"].append({
            "label": label,
            "amount": round(dollars),
            "raw": amt,
            "unit": unit,
        })
    result["total_adjustment"] = round(total_adj)

    final = max(base + total_adj, 0.0)
    result["final_value"] = round(final)

    # ----- displayed range -----
    spread = float(settings.get("range_spread") or 0)
    spread_dollars = final * (spread / 100.0) if unit == "percent" else spread
    result["range_low"] = round(max(final - spread_dollars, 0))
    result["range_high"] = round(final + spread_dollars)
    result["ok"] = True
    return result
