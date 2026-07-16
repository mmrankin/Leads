"""Revenue report for the admin Reports section.

Revenue = delivered leads x the dealer's per-lead price for that product
(dealer_products.per_lead_price, the current grant). Leads come from the same
union as the Program stats: `leads` (Lead Form), `trade_leads` (Trade-In) and
`credit_leads` (Credit Estimator web form vs Credit Pipeline bureau leads,
split by `source`). Leads whose dealer has no per-lead price on the grant
count as $0 and are surfaced as "unpriced" so pricing gaps are visible.
"""

import calendar
from datetime import date

import dlrpro_db as dlr
from stats_view import PRODUCT_LABEL, PRODUCT_ORDER

MONTHS_BACK = 11    # chart shows current month + this many back (12 bars)

# One aggregate over every lead in the window: (dealer, product, day) with the
# lead count and the grant's current per-lead price. created_at is an ISO
# string, so LEFT(.,10) is the day and string >= is a correct date filter.
_REVENUE_SQL = """
SELECT a.dealer_id, a.product, LEFT(a.created_at,10) AS day, COUNT(*) AS n,
       MAX(d.dealer_name) AS dealer_name, MAX(dp.per_lead_price) AS price
FROM (
  SELECT dealer_id, 'LEAD_FORM' AS product, created_at FROM leads
  UNION ALL SELECT dealer_id, 'TRADE_IN', created_at FROM trade_leads
  UNION ALL SELECT dealer_id,
    CASE WHEN source = 'Credit Pipeline' THEN 'CREDIT_PIPELINE' ELSE 'CREDIT_EST' END,
    created_at FROM credit_leads
) a
LEFT JOIN dealers d ON d.dealer_id = a.dealer_id
LEFT JOIN dealer_products dp ON dp.dealer_id = a.dealer_id AND dp.product_code = a.product
WHERE a.created_at >= %(start)s
GROUP BY a.dealer_id, a.product, LEFT(a.created_at,10)
"""


def _add_month(y, m, delta):
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


def revenue_stats(month=None):
    """Stats dict for the Revenue page. `month`='YYYY-MM' selects the detail
    view (default current month); the month chart always covers the last
    MONTHS_BACK+1 months."""
    today = date.today()
    sy, sm = today.year, today.month
    if month:
        try:
            y, m = int(month[:4]), int(month[5:7])
            if 1 <= m <= 12:
                sy, sm = y, m
        except (ValueError, IndexError):
            pass
    sel_key = "%04d-%02d" % (sy, sm)

    # Window: whichever is earlier — the chart start or the selected month.
    cy, cm = _add_month(today.year, today.month, -MONTHS_BACK)
    start = min(date(cy, cm, 1), date(sy, sm, 1)).isoformat()
    try:
        rows = dlr.query(_REVENUE_SQL, {"start": start})
    except Exception:
        rows = []

    # --- aggregate in one pass ---
    months = {}                       # ym -> {"leads": n, "revenue": $}
    day_map = {}                      # day (selected month) -> {"leads","revenue"}
    dealers = {}                      # dealer_id -> {name, leads, revenue, unpriced}
    types = {p: {"leads": 0, "revenue": 0.0, "unpriced": 0} for p in PRODUCT_ORDER}
    for r in rows:
        n = int(r["n"] or 0)
        price = float(r["price"]) if r["price"] is not None else None
        rev = n * (price or 0.0)
        ym = (r["day"] or "")[:7]
        mo = months.setdefault(ym, {"leads": 0, "revenue": 0.0})
        mo["leads"] += n
        mo["revenue"] += rev
        if ym != sel_key:
            continue
        dd = day_map.setdefault(r["day"], {"leads": 0, "revenue": 0.0})
        dd["leads"] += n
        dd["revenue"] += rev
        de = dealers.setdefault(r["dealer_id"], {
            "dealer_id": r["dealer_id"], "name": r["dealer_name"] or r["dealer_id"] or "—",
            "leads": 0, "revenue": 0.0, "unpriced": 0})
        de["leads"] += n
        de["revenue"] += rev
        ty = types.get(r["product"]) or types.setdefault(
            r["product"], {"leads": 0, "revenue": 0.0, "unpriced": 0})
        ty["leads"] += n
        ty["revenue"] += rev
        if price is None:
            de["unpriced"] += n
            ty["unpriced"] += n

    # --- month bar chart (oldest -> newest, always MONTHS_BACK+1 bars) ---
    by_month = []
    for back in range(MONTHS_BACK, -1, -1):
        y, m = _add_month(today.year, today.month, -back)
        ym = "%04d-%02d" % (y, m)
        mo = months.get(ym, {"leads": 0, "revenue": 0.0})
        by_month.append({"ym": ym, "label": date(y, m, 1).strftime("%b"),
                         "year": y, "leads": mo["leads"],
                         "revenue": round(mo["revenue"], 2),
                         "selected": ym == sel_key})
    chart_max = max([1.0] + [b["revenue"] for b in by_month])

    # --- selected month, by day (every day, zero-filled) ---
    by_day = []
    for dd in range(1, calendar.monthrange(sy, sm)[1] + 1):
        d = "%s-%02d" % (sel_key, dd)
        v = day_map.get(d, {"leads": 0, "revenue": 0.0})
        by_day.append({"date": d, "day": dd, "leads": v["leads"],
                       "revenue": round(v["revenue"], 2)})
    day_max = max([1.0] + [d["revenue"] for d in by_day])

    by_dealer = sorted(dealers.values(), key=lambda d: -d["revenue"])
    for d in by_dealer:
        d["revenue"] = round(d["revenue"], 2)
    by_type = [{"product": p, "label": PRODUCT_LABEL.get(p, p),
                "leads": t["leads"], "revenue": round(t["revenue"], 2),
                "unpriced": t["unpriced"]}
               for p, t in ((p, types[p]) for p in types) if t["leads"]]
    by_type.sort(key=lambda t: -t["revenue"])

    summary = {"leads": sum(d["leads"] for d in by_day),
               "revenue": round(sum(d["revenue"] for d in by_day), 2),
               "unpriced": sum(d["unpriced"] for d in by_dealer),
               "dealers": len(by_dealer)}

    return {"selected": sel_key,
            "selected_label": date(sy, sm, 1).strftime("%B %Y"),
            "by_month": by_month, "chart_max": chart_max,
            "by_day": by_day, "day_max": day_max,
            "by_dealer": by_dealer, "by_type": by_type, "summary": summary}
