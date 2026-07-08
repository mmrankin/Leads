"""Read matched, not-yet-sent trigger leads from the CreditPipeline feed.

CreditPipeline lives on 10.1.4.8, a linked server on the dlrPro SQL instance
(10.1.1.10). We match a CreditPipeline retailer to a dlrPro dealer by
retailer_code = dealer_id, join the customer contact, and exclude anything
already in the dlrPro `sent` ledger. Runs through the dlrPro connection.

Env:
    CREDITPIPELINE_LINKED_SERVER   linked-server name on dlrPro (default 10.1.4.8)
    CREDITPIPELINE_DB              database name (default CreditPipeline)
"""

import os

import pymssql

import dlrpro_db as dlr

LINKED_SERVER = os.environ.get("CREDITPIPELINE_LINKED_SERVER", "10.1.4.8")
DB = os.environ.get("CREDITPIPELINE_DB", "CreditPipeline")

# A matched lead is "rejected" (abandoned) once it's this old and still unsent.
REJECT_AFTER_MINUTES = int(os.environ.get("CREDITPIPELINE_REJECT_MIN", "30"))

# Matched dealer (retailer_code = dealer_id), not yet in dbo.sent. The customer
# record is LEFT-joined: when the customer_record_id doesn't exist, the poller
# falls back to the matched_payload for the name/address. dealers_id = dealers.id.
_FETCH_SQL = """SELECT TOP {limit}
  m.result_id, m.matched_payload, m.consumer_zip, m.consumer_id,
  d.id AS dealers_id, d.dealer_id, d.dealer_name, d.lead_email_address,
  c.first_name, c.last_name, c.email_address,
  c.cell_phone, c.home_phone, c.work_phone,
  c.address_line1 AS address, c.city, c.state, c.postal_code AS zip,
  c.year AS vehicle_year, c.make AS vehicle_make, c.model AS vehicle_model
FROM [{ls}].[{db}].[dbo].[match_result] m
LEFT JOIN [{ls}].[{db}].[dbo].[customer_record] c ON c.customer_record_id = m.customer_record_id
JOIN [{ls}].[{db}].[dbo].[retailer] r ON r.retailer_id = m.retailer_id
JOIN dlrPro.dbo.dealers d ON d.dealer_id = r.retailer_code
LEFT JOIN dlrPro.dbo.[sent] s ON s.dealer_id = d.id AND s.result_id = m.result_id
LEFT JOIN dlrPro.dbo.pipeline_skips sk ON sk.dealer_id = d.id AND sk.result_id = m.result_id
WHERE s.id IS NULL {skip_cond} {grant_cond}
ORDER BY m.result_id ASC"""

# A record with no phone/email is retried at most this many times, then dropped.
MAX_NO_CONTACT_ATTEMPTS = 3

# Defense-in-depth: the automatic poller must never pull a record for a dealer that
# is NOT currently on the CREDIT_PIPELINE product. This mirrors get_active_grant
# (today within [valid_from, valid_to]; NULL bounds = unbounded) at the source, so a
# dealer turned off the product stops getting automatic leads immediately — even
# independently of the poller's eligible() check. (Manual admin sends skip this.)
_ACTIVE_GRANT_COND = (
    "AND EXISTS (SELECT 1 FROM dlrPro.dbo.dealer_products dp "
    "WHERE dp.dealer_id = d.dealer_id AND dp.product_code = 'CREDIT_PIPELINE' "
    "AND (dp.valid_from IS NULL OR dp.valid_from <= CONVERT(date, GETDATE())) "
    "AND (dp.valid_to   IS NULL OR dp.valid_to   >= CONVERT(date, GETDATE())))")


# ----- Phone/email waterfall (same as the Trigger Leads detail page) -----
# Phone: match_phone(result_id) -> CellPhone -> HomePhone -> WorkPhone -> AppendedPhone
# Email: match_email(result_id) -> Email -> AppendedEmail
# All keyed by result_id from the Equifax trigger view + the match_* tables (which
# keep their value in an `email` column). Batched so a whole fetch costs 3 queries.


def _s(v):
    return str(v).strip() if v is not None else ""


def _match_values(table, result_ids):
    """{result_id: value} from a CreditPipeline match_* table (value in `email`),
    newest row per result_id. '' entries dropped. Empty on error."""
    ids = ",".join(str(int(x)) for x in result_ids if x is not None)
    if not ids:
        return {}
    sql = ("SELECT result_id, email AS val, created "
           "FROM [{ls}].[{db}].[dbo].[{t}] "
           "WHERE result_id IN ({ids}) AND email IS NOT NULL AND LTRIM(RTRIM(email)) <> ''"
           ).format(ls=LINKED_SERVER, db=DB, t=table, ids=ids)
    best = {}
    try:
        for r in dlr.query(sql):
            rid, v, cr = r.get("result_id"), _s(r.get("val")), r.get("created")
            if not v:
                continue
            if rid not in best or (cr is not None and (best[rid][1] is None or cr > best[rid][1])):
                best[rid] = (v, cr)
    except Exception:
        return {}
    return {rid: v for rid, (v, cr) in best.items()}


def _view_contacts(result_ids):
    """{result_id: {phone/email cols}} from the Equifax trigger view. Empty on error."""
    ids = ",".join(str(int(x)) for x in result_ids if x is not None)
    if not ids:
        return {}
    sql = ("SELECT result_id, CellPhone, HomePhone, WorkPhone, AppendedPhone, "
           "Email, AppendedEmail "
           "FROM [{ls}].[{db}].[dbo].[vw_EquifaxConsumerRecordTriggers] "
           "WHERE result_id IN ({ids})").format(ls=LINKED_SERVER, db=DB, ids=ids)
    try:
        return {r["result_id"]: r for r in dlr.query(sql)}
    except Exception:
        return {}


def annotate_contact(rows):
    """Set row['wf_phone'] / row['wf_email'] on each row per the waterfall above,
    keyed by result_id. Batched; best-effort (fields default to '')."""
    if not rows:
        return rows
    ids = [r.get("result_id") for r in rows]
    mp = _match_values("match_phone", ids)
    me = _match_values("match_email", ids)
    vc = _view_contacts(ids)
    for r in rows:
        rid = r.get("result_id")
        v = vc.get(rid) or {}
        r["wf_phone"] = (mp.get(rid) or _s(v.get("CellPhone")) or _s(v.get("HomePhone"))
                         or _s(v.get("WorkPhone")) or _s(v.get("AppendedPhone")))
        r["wf_email"] = (me.get(rid) or _s(v.get("Email")) or _s(v.get("AppendedEmail")))
    return rows


def _exec_autocommit(sql, params=None):
    """Run a statement in autocommit mode (no wrapping transaction). Needed for a
    linked-server write via EXEC(...) AT: inside pymssql's default transaction such
    a write promotes to a distributed transaction (MSDTC), which isn't available."""
    conn = pymssql.connect(
        server=os.environ.get("DLRPRO_DB_SERVER", "10.1.1.10"),
        user=os.environ.get("DLRPRO_DB_USER", "sa"),
        password=os.environ.get("DLRPRO_DB_PASSWORD", ""),
        database=os.environ.get("DLRPRO_DB_NAME", "dlrPro"),
        timeout=120, login_timeout=10, autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute(sql, params) if params is not None else cur.execute(sql)
    finally:
        conn.close()


def reject_stale_unsent(minutes=REJECT_AFTER_MINUTES):
    """Set match_result.rejected = 1 on every lead older than `minutes` (by
    returned_at, which is UTC) that was never sent and isn't already rejected.
    Returns the number newly rejected.

    The write goes through EXEC(...) AT [linked server] in autocommit mode — a plain
    4-part-name UPDATE needs MSDTC, which this instance can't begin. The stale set is
    found first with an ordinary cross-server read (fast), then updated remotely by
    result_id only (no cross-server join in the write)."""
    find = ("SELECT m.result_id "
            "FROM [{ls}].[{db}].[dbo].[match_result] m "
            "LEFT JOIN dlrPro.dbo.[sent] s ON s.result_id = m.result_id "
            "WHERE m.rejected = 0 AND s.id IS NULL "
            "AND m.returned_at < DATEADD(minute, -{m}, GETUTCDATE())"
            ).format(ls=LINKED_SERVER, db=DB, m=int(minutes))
    try:
        ids = [int(r["result_id"]) for r in dlr.query(find, timeout=120)
               if r.get("result_id") is not None]
    except Exception:
        return 0
    done = 0
    for i in range(0, len(ids), 500):
        idlist = ",".join(str(x) for x in ids[i:i + 500])
        remote = ("UPDATE %s.dbo.match_result SET rejected = 1 "
                  "WHERE result_id IN (%s)" % (DB, idlist))
        try:
            _exec_autocommit("EXEC (%s) AT [" + LINKED_SERVER + "]", (remote,))
            done += len(ids[i:i + 500])
        except Exception:
            pass
    return done


def fetch_unsent(limit=1000):
    """Matched, not-yet-sent trigger-lead rows for dealers CURRENTLY on the
    CREDIT_PIPELINE product, excluding records that already hit the no-contact retry
    cap OR have been rejected (too old, see reject_stale_unsent). Rows carry the
    waterfall phone/email (wf_phone / wf_email) and come back NEWEST first."""
    sql = _FETCH_SQL.format(limit=int(limit), ls=LINKED_SERVER, db=DB,
                            skip_cond="AND (sk.attempts IS NULL OR sk.attempts < %d) "
                                      "AND m.rejected = 0" % MAX_NO_CONTACT_ATTEMPTS,
                            grant_cond=_ACTIVE_GRANT_COND)
    rows = dlr.query(sql)
    # Newest-first so fresh leads send before they age past the reject window. The
    # query itself streams result_id ASC (the only order the 5-table cross-linked-
    # server join plans fast), so we flip in Python. The unsent+non-rejected pool is
    # small (<= reject window old), so the BATCH cap never hides the newest rows.
    rows.sort(key=lambda r: int(r["result_id"]) if r.get("result_id") is not None else -1,
              reverse=True)
    return annotate_contact(rows)


def fetch_one(result_id):
    """One matched, not-yet-sent row for a specific result_id, or None. Ignores the
    retry cap AND the active-grant filter so an admin can always attempt a manual
    send (the poller's automatic path still enforces both). Carries wf_phone/wf_email."""
    sql = _FETCH_SQL.format(limit=1, ls=LINKED_SERVER, db=DB,
                            skip_cond="AND m.result_id = %(rid)s", grant_cond="")
    rows = annotate_contact(dlr.query(sql, {"rid": int(result_id)}))
    return rows[0] if rows else None
