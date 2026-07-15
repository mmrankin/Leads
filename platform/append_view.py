"""Append-activity stats for the admin Append page.

Counts imdatacenter phone/email append API activity (dbo.credit_append_log) and
lead deliveries (dbo.sent), by day (for a selected month) and by month (current
+ 3 back). One append API call attempts both phone (fp) and email (fe2); a
"phone/email append" is a call that returned a match (also the billable unit at
$0.015 each).
"""

import calendar
from datetime import date

import dlrpro_db as dlr

MONTHS_BACK = 3          # selector goes back this many months
APPEND_UNIT_COST = 0.025  # $ per phone match + $ per email match (cost estimate)

_HAS_PHONE = "all_phones IS NOT NULL AND all_phones<>''"
_HAS_EMAIL = "email_appended IS NOT NULL AND email_appended<>''"

_APPEND_DAY_SQL = (
    "SELECT CONVERT(date, created) AS d, COUNT(*) AS calls, "
    "SUM(CASE WHEN " + _HAS_PHONE + " THEN 1 ELSE 0 END) AS phone, "
    "SUM(CASE WHEN " + _HAS_EMAIL + " THEN 1 ELSE 0 END) AS email "
    "FROM dbo.credit_append_log WHERE created>=%(start)s AND created<%(end)s "
    "GROUP BY CONVERT(date, created)")
_SENT_DAY_SQL = ("SELECT CONVERT(date, created) AS d, COUNT(*) AS sends "
                 "FROM dbo.sent WHERE created>=%(start)s AND created<%(end)s "
                 "GROUP BY CONVERT(date, created)")
_APPEND_MONTH_SQL = (
    "SELECT (YEAR(created)*100+MONTH(created)) AS ym, COUNT(*) AS calls, "
    "SUM(CASE WHEN " + _HAS_PHONE + " THEN 1 ELSE 0 END) AS phone, "
    "SUM(CASE WHEN " + _HAS_EMAIL + " THEN 1 ELSE 0 END) AS email "
    "FROM dbo.credit_append_log WHERE created>=%(start)s "
    "GROUP BY (YEAR(created)*100+MONTH(created))")
_SENT_MONTH_SQL = ("SELECT (YEAR(created)*100+MONTH(created)) AS ym, COUNT(*) AS sends "
                   "FROM dbo.sent WHERE created>=%(start)s "
                   "GROUP BY (YEAR(created)*100+MONTH(created))")


def _add_month(y, m, delta):
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


def _q(sql, params):
    try:
        return dlr.query(sql, params)
    except Exception:
        return []


def append_stats(month=None):
    """Stats dict for the Append page. `month`='YYYY-MM' selects the day view
    (default current month)."""
    today = date.today()
    sy, sm = today.year, today.month
    if month:
        try:
            sy, sm = int(month[:4]), int(month[5:7])
        except (ValueError, IndexError):
            pass
    sel_start = date(sy, sm, 1)
    ey, em = _add_month(sy, sm, 1)
    sel_end = date(ey, em, 1)

    # --- selected month, by day ---
    ap = {r["d"]: r for r in _q(_APPEND_DAY_SQL,
                                {"start": sel_start.isoformat(), "end": sel_end.isoformat()})}
    sn = {r["d"]: r["sends"] for r in _q(_SENT_DAY_SQL,
                                         {"start": sel_start.isoformat(), "end": sel_end.isoformat()})}
    by_day = []
    for dd in range(1, calendar.monthrange(sy, sm)[1] + 1):
        d = date(sy, sm, dd)
        a = ap.get(d, {})
        by_day.append({"date": d.isoformat(), "day": dd,
                       "calls": a.get("calls") or 0, "phone": a.get("phone") or 0,
                       "email": a.get("email") or 0, "sends": sn.get(d, 0)})
    chart_max = max([1] + [max(r["phone"], r["email"]) for r in by_day])

    # --- current + MONTHS_BACK, by month ---
    m3y, m3m = _add_month(today.year, today.month, -MONTHS_BACK)
    amo = {r["ym"]: r for r in _q(_APPEND_MONTH_SQL, {"start": date(m3y, m3m, 1).isoformat()})}
    smo = {r["ym"]: r["sends"] for r in _q(_SENT_MONTH_SQL, {"start": date(m3y, m3m, 1).isoformat()})}
    by_month, months = [], []
    for back in range(MONTHS_BACK + 1):
        y, m = _add_month(today.year, today.month, -back)
        ym = y * 100 + m
        a = amo.get(ym, {})
        label = date(y, m, 1).strftime("%b %Y")
        key = "%04d-%02d" % (y, m)
        by_month.append({"ym": key, "label": label,
                         "calls": a.get("calls") or 0, "phone": a.get("phone") or 0,
                         "email": a.get("email") or 0, "sends": smo.get(ym, 0)})
        months.append({"ym": key, "label": label, "selected": (y == sy and m == sm)})

    summary = {k: sum(r[k] for r in by_day) for k in ("calls", "phone", "email", "sends")}
    summary["cost"] = round((summary["phone"] + summary["email"]) * APPEND_UNIT_COST, 2)

    return {"selected": "%04d-%02d" % (sy, sm), "selected_label": sel_start.strftime("%B %Y"),
            "months": months, "by_day": by_day, "by_month": by_month,
            "summary": summary, "chart_max": chart_max, "unit_cost": APPEND_UNIT_COST}
