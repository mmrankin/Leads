"""Aggregate statistics for the admin Statistics section.

program_stats()  -> volume/velocity across all lead products (Program page).
lead_stats()     -> summarized credit + finance + vehicle averages (Leads page).

All leads live in three dlrPro tables: `leads` (Lead Form), `trade_leads` (Trade-In)
and `credit_leads` (Credit Estimator web form + Credit Pipeline bureau leads, split by
`source`). created_at is an ISO 'YYYY-MM-DD HH:MM:SS' string, so LEFT(.,10) is the day
and string comparison against a 'YYYY-MM-DD' cutoff is a correct date filter.
"""
import datetime
import dlrpro_db as dlr

PRODUCT_LABEL = {
    "LEAD_FORM": "Dealer Lead Form", "TRADE_IN": "Trade-In",
    "CREDIT_EST": "Credit Estimator", "CREDIT_PIPELINE": "Credit Pipeline",
}
PRODUCT_ORDER = ["LEAD_FORM", "TRADE_IN", "CREDIT_EST", "CREDIT_PIPELINE"]

# Every lead unioned to (dealer_id, product, created_at). credit_leads splits into the
# bureau pipeline vs the web estimator by its source string.
_ALL_LEADS = """
  SELECT dealer_id, 'LEAD_FORM' AS product, created_at FROM leads
  UNION ALL SELECT dealer_id, 'TRADE_IN', created_at FROM trade_leads
  UNION ALL SELECT dealer_id,
    CASE WHEN source = 'Credit Pipeline' THEN 'CREDIT_PIPELINE' ELSE 'CREDIT_EST' END,
    created_at FROM credit_leads
"""


def _q(sql, params=None):
    try:
        return dlr.query(sql, params)
    except Exception:
        return []


def program_stats(days=30):
    cutoff = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()

    by_product = {r["product"]: r["n"] for r in
                  _q("SELECT product, COUNT(*) n FROM (%s) a GROUP BY product" % _ALL_LEADS)}
    products = [{"product": p, "label": PRODUCT_LABEL[p], "n": by_product.get(p, 0)}
                for p in PRODUCT_ORDER]
    total = sum(by_product.values())

    dealers = _q("""SELECT a.dealer_id, MAX(d.dealer_name) dealer_name, COUNT(*) n
        FROM (%s) a LEFT JOIN dealers d ON d.dealer_id = a.dealer_id
        GROUP BY a.dealer_id ORDER BY n DESC""" % _ALL_LEADS)

    by_day = _q("""SELECT LEFT(created_at,10) day, COUNT(*) n FROM (%s) a
        WHERE created_at >= %%(c)s GROUP BY LEFT(created_at,10) ORDER BY day""" % _ALL_LEADS,
                {"c": cutoff})
    # fill missing days with 0 so the graph is continuous
    have = {r["day"]: r["n"] for r in by_day}
    series = []
    d0 = datetime.date.today() - datetime.timedelta(days=days - 1)
    for i in range(days):
        d = (d0 + datetime.timedelta(days=i)).isoformat()
        series.append({"day": d, "n": have.get(d, 0)})

    last30 = sum(s["n"] for s in series)
    return {
        "total": total, "last_period": last30, "days": days,
        "dealer_count": len(dealers), "products": products,
        "by_dealer": dealers, "by_day": series,
    }


def lead_stats():
    row = _q("""SELECT
        COUNT(*) total,
        AVG(CASE WHEN vehicle_price>0 THEN vehicle_price END) price_avg,      SUM(CASE WHEN vehicle_price>0 THEN 1 ELSE 0 END) price_n,
        AVG(CASE WHEN amount_financed>0 THEN amount_financed END) fin_avg,    SUM(CASE WHEN amount_financed>0 THEN 1 ELSE 0 END) fin_n,
        AVG(CASE WHEN monthly_payment>0 THEN monthly_payment END) pay_avg,    SUM(CASE WHEN monthly_payment>0 THEN 1 ELSE 0 END) pay_n,
        AVG(CASE WHEN down_payment>0 THEN down_payment END) down_avg,         SUM(CASE WHEN down_payment>0 THEN 1 ELSE 0 END) down_n,
        AVG(CASE WHEN trade_value>0 THEN trade_value END) trade_avg,         SUM(CASE WHEN trade_value>0 THEN 1 ELSE 0 END) trade_n,
        AVG(CASE WHEN max_vehicle_price>0 THEN max_vehicle_price END) maxp_avg, SUM(CASE WHEN max_vehicle_price>0 THEN 1 ELSE 0 END) maxp_n,
        AVG(CASE WHEN term_months>0 THEN CAST(term_months AS float) END) term_avg, SUM(CASE WHEN term_months>0 THEN 1 ELSE 0 END) term_n,
        AVG(CASE WHEN apr>0 THEN apr END) apr_avg,                            SUM(CASE WHEN apr>0 THEN 1 ELSE 0 END) apr_n,
        AVG(CASE WHEN est_score>0 THEN CAST(est_score AS float) END) score_avg, SUM(CASE WHEN est_score>0 THEN 1 ELSE 0 END) score_n,
        AVG(CASE WHEN annual_income>0 THEN annual_income END) inc_avg,        SUM(CASE WHEN annual_income>0 THEN 1 ELSE 0 END) inc_n
        FROM credit_leads""")
    a = row[0] if row else {}

    def money(v):
        return "$%s" % format(int(round(v)), ",") if v is not None else "—"

    finance = [
        ("Avg vehicle price", money(a.get("price_avg")), a.get("price_n") or 0),
        ("Avg amount financed", money(a.get("fin_avg")), a.get("fin_n") or 0),
        ("Avg monthly payment", money(a.get("pay_avg")), a.get("pay_n") or 0),
        ("Avg down payment", money(a.get("down_avg")), a.get("down_n") or 0),
        ("Avg trade-in value", money(a.get("trade_avg")), a.get("trade_n") or 0),
        ("Avg max affordable price", money(a.get("maxp_avg")), a.get("maxp_n") or 0),
        ("Avg loan term", ("%d mo" % round(a["term_avg"])) if a.get("term_avg") else "—", a.get("term_n") or 0),
    ]
    credit = [
        ("Avg credit score", ("%d" % round(a["score_avg"])) if a.get("score_avg") else "—", a.get("score_n") or 0),
        ("Avg APR", ("%.2f%%" % a["apr_avg"]) if a.get("apr_avg") else "—", a.get("apr_n") or 0),
        ("Avg annual income", money(a.get("inc_avg")), a.get("inc_n") or 0),
    ]

    tiers = _q("SELECT tier, COUNT(*) n FROM credit_leads WHERE tier IS NOT NULL "
               "AND LTRIM(RTRIM(tier))<>'' GROUP BY tier ORDER BY n DESC")
    makes = _q("SELECT TOP 12 vehicle_make make, COUNT(*) n FROM credit_leads "
               "WHERE vehicle_make IS NOT NULL AND LTRIM(RTRIM(vehicle_make))<>'' "
               "GROUP BY vehicle_make ORDER BY n DESC")
    years = _q("SELECT vehicle_year yr, COUNT(*) n FROM credit_leads "
               "WHERE vehicle_year IS NOT NULL AND LTRIM(RTRIM(vehicle_year))<>'' "
               "GROUP BY vehicle_year ORDER BY vehicle_year DESC")

    return {
        "total": a.get("total") or 0,
        "credit": [{"label": l, "value": v, "n": n} for l, v, n in credit],
        "finance": [{"label": l, "value": v, "n": n} for l, v, n in finance],
        "tiers": tiers, "by_make": makes, "by_year": years,
    }


# ---- All Equifax trigger consumers vs the subset delivered to dealers ----
# The bureau population and its credit/estimated-finance profile live in the
# Equifax trigger view; "delivered" = the result_id appears in the dbo.sent ledger.
_CONSUMER_VIEW = "[10.1.4.8].[CreditPipeline].[dbo].[vw_EquifaxConsumerRecordTriggers]"
_CONSUMER_METRICS = [   # (label, float-expr on `t`, kind)
    ("Avg credit score", "COALESCE(TRY_CAST(t.fico_auto_8 AS float), TRY_CAST(t.fico_8 AS float))", "int"),
    ("Avg estimated APR", "TRY_CAST(t.EstimatedInterestRate AS float)", "pct"),
    ("Avg estimated payment", "TRY_CAST(t.EstimatedPayment AS float)", "money"),
    ("Avg estimated amount financed", "TRY_CAST(t.EstimatedAmountFinanced AS float)", "money"),
    ("Avg estimated loan term", "TRY_CAST(t.EstimatedTermInMonths AS float)", "months"),
    ("Avg estimated balance", "TRY_CAST(t.EstCurrentBalance AS float)", "money"),
]


def _consumer_fmt(kind, v):
    if v is None:
        return "—"
    if kind == "pct":
        return "%.2f%%" % v
    if kind == "months":
        return "%d mo" % round(v)
    if kind == "money":
        return "$%s" % format(int(round(v)), ",")
    return "%d" % round(v)   # int (credit score)


def consumer_stats():
    """Credit + estimated-finance averages for the whole Equifax trigger pool
    ("all") and the subset delivered to a dealer ("delivered"), in one pass.
    Each average counts only rows carrying a positive value (shown as n)."""
    sel = ["COUNT(*) AS n_all",
           "SUM(CASE WHEN dl.result_id IS NOT NULL THEN 1 ELSE 0 END) AS n_deliv"]
    for i, (_, expr, _kind) in enumerate(_CONSUMER_METRICS):
        sel += [
            "AVG(CASE WHEN %s>0 THEN %s END) AS a_all_%d" % (expr, expr, i),
            "SUM(CASE WHEN %s>0 THEN 1 ELSE 0 END) AS na_all_%d" % (expr, i),
            "AVG(CASE WHEN dl.result_id IS NOT NULL AND %s>0 THEN %s END) AS a_dl_%d" % (expr, expr, i),
            "SUM(CASE WHEN dl.result_id IS NOT NULL AND %s>0 THEN 1 ELSE 0 END) AS na_dl_%d" % (expr, i),
        ]
    sql = ("SELECT " + ", ".join(sel) + " FROM " + _CONSUMER_VIEW + " t "
           "LEFT JOIN (SELECT DISTINCT result_id FROM dlrPro.dbo.[sent]) dl "
           "ON dl.result_id = t.result_id")
    try:
        r = dlr.query(sql, timeout=120)[0]
    except Exception:
        return {"n_all": 0, "n_delivered": 0, "all": [], "delivered": []}

    def cards(pfx):
        out = []
        for i, (label, _expr, kind) in enumerate(_CONSUMER_METRICS):
            out.append({"label": label,
                        "value": _consumer_fmt(kind, r.get("a_%s_%d" % (pfx, i))),
                        "n": r.get("na_%s_%d" % (pfx, i)) or 0})
        return out

    return {"n_all": r.get("n_all") or 0, "n_delivered": r.get("n_deliv") or 0,
            "all": cards("all"), "delivered": cards("dl")}
