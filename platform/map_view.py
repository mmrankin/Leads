"""Lead-origin map data for the admin Map page.

Counts trigger leads by the consumer's state (JSON_VALUE of match_result's
matched_payload) over a date range, and lays them out on a US tile-grid
("statebin") map for a self-contained choropleth — no external map data.
"""

from datetime import date, timedelta

import dlrpro_db as dlr

LINKED_SERVER = "10.1.4.8"
DB = "CreditPipeline"
MAX_RANGE_DAYS = 366

# US tile-grid (statebin) layout: state -> (row, col). 8 rows x 11 cols.
STATE_GRID = {
    "AK": (0, 0), "ME": (0, 10),
    "VT": (1, 9), "NH": (1, 10),
    "WA": (2, 0), "ID": (2, 1), "MT": (2, 2), "ND": (2, 3), "MN": (2, 4),
    "IL": (2, 5), "WI": (2, 6), "MI": (2, 8), "NY": (2, 9), "MA": (2, 10),
    "OR": (3, 0), "NV": (3, 1), "WY": (3, 2), "SD": (3, 3), "IA": (3, 4),
    "IN": (3, 5), "OH": (3, 6), "PA": (3, 8), "NJ": (3, 9), "CT": (3, 10),
    "CA": (4, 0), "UT": (4, 1), "CO": (4, 2), "NE": (4, 3), "MO": (4, 4),
    "KY": (4, 5), "WV": (4, 6), "VA": (4, 7), "MD": (4, 8), "DE": (4, 9), "RI": (4, 10),
    "AZ": (5, 1), "NM": (5, 2), "KS": (5, 3), "AR": (5, 4), "TN": (5, 5),
    "NC": (5, 6), "SC": (5, 7), "DC": (5, 8),
    "OK": (6, 3), "LA": (6, 4), "MS": (6, 5), "AL": (6, 6), "GA": (6, 7),
    "HI": (7, 0), "TX": (7, 3), "FL": (7, 8),
}
GRID_ROWS = 8
GRID_COLS = 11

# Matched records: all trigger leads by consumer state (filtered by arrival).
_MATCHED_SQL = (
    "SELECT JSON_VALUE(CAST(matched_payload AS NVARCHAR(MAX)), '$.state') AS st, COUNT(*) AS c "
    "FROM [{ls}].[{db}].[dbo].[match_result] "
    "WHERE returned_at >= %(start)s AND returned_at < %(end)s "
    "GROUP BY JSON_VALUE(CAST(matched_payload AS NVARCHAR(MAX)), '$.state')"
).format(ls=LINKED_SERVER, db=DB)

# Delivered leads: only leads actually sent (in dbo.sent), by consumer state
# (filtered by send date).
_DELIVERED_SQL = (
    "SELECT JSON_VALUE(CAST(m.matched_payload AS NVARCHAR(MAX)), '$.state') AS st, COUNT(*) AS c "
    "FROM dbo.sent s "
    "JOIN [{ls}].[{db}].[dbo].[match_result] m ON m.result_id = s.result_id "
    "WHERE s.created >= %(start)s AND s.created < %(end)s "
    "GROUP BY JSON_VALUE(CAST(m.matched_payload AS NVARCHAR(MAX)), '$.state')"
).format(ls=LINKED_SERVER, db=DB)


def _parse(d, default):
    if not d:
        return default
    try:
        return date(int(d[:4]), int(d[5:7]), int(d[8:10]))
    except (ValueError, IndexError, TypeError):
        return default


def leads_map(start=None, end=None, mode=None):
    """Lead counts by state over [start, end], laid out on the tile grid.
    mode='delivered' counts only sent leads; anything else counts all matched
    records. Range is clamped to at most one year (default: last 90 days)."""
    mode = "delivered" if mode == "delivered" else "matched"
    today = date.today()
    end_d = _parse(end, today)
    start_d = _parse(start, end_d - timedelta(days=90))
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    if (end_d - start_d).days > MAX_RANGE_DAYS:       # clamp to <= 1 year
        start_d = end_d - timedelta(days=MAX_RANGE_DAYS)

    counts = {}
    sql = _DELIVERED_SQL if mode == "delivered" else _MATCHED_SQL
    try:
        for r in dlr.query(sql, {"start": start_d.isoformat(),
                                 "end": (end_d + timedelta(days=1)).isoformat()}):
            st = (r.get("st") or "").strip().upper()
            if st in STATE_GRID:
                counts[st] = counts.get(st, 0) + int(r.get("c") or 0)
    except Exception:
        counts = {}

    max_count = max(counts.values()) if counts else 0
    cells = []
    for st, (row, col) in STATE_GRID.items():
        c = counts.get(st, 0)
        cells.append({"state": st, "row": row, "col": col, "count": c,
                      "intensity": round(c / max_count, 3) if max_count else 0.0})
    by_state = sorted(({"state": s, "count": c} for s, c in counts.items()),
                      key=lambda x: x["count"], reverse=True)
    presets = [{"label": lbl, "start": (today - timedelta(days=n)).isoformat(),
                "end": today.isoformat()}
               for lbl, n in (("30 days", 30), ("90 days", 90),
                              ("6 months", 182), ("1 year", 365))]
    return {"cells": cells, "max_count": max_count, "total": sum(counts.values()),
            "by_state": by_state, "states_hit": len(counts),
            "rows": GRID_ROWS, "cols": GRID_COLS, "presets": presets, "mode": mode,
            "unit": "delivered leads" if mode == "delivered" else "matched records",
            "start": start_d.isoformat(), "end": end_d.isoformat()}
