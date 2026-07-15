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
    "AND (dp.paused IS NULL OR dp.paused = 0) "        # paused dealers get no auto sends
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
    col = "phone" if table == "match_phone" else "email"   # value column differs per table
    sql = ("SELECT result_id, {c} AS val, created "
           "FROM [{ls}].[{db}].[dbo].[{t}] "
           "WHERE result_id IN ({ids}) AND {c} IS NOT NULL AND LTRIM(RTRIM({c})) <> ''"
           ).format(c=col, ls=LINKED_SERVER, db=DB, t=table, ids=ids)
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


def _autocommit_conn(timeout=280):
    """A pymssql connection in autocommit mode (no wrapping transaction) — required
    for linked-server writes via EXEC(...) AT, which inside pymssql's default
    transaction promote to a distributed transaction (MSDTC) that isn't available."""
    return pymssql.connect(
        server=os.environ.get("DLRPRO_DB_SERVER", "10.1.1.10"),
        user=os.environ.get("DLRPRO_DB_USER", "sa"),
        password=os.environ.get("DLRPRO_DB_PASSWORD", ""),
        database=os.environ.get("DLRPRO_DB_NAME", "dlrPro"),
        timeout=timeout, login_timeout=10, autocommit=True)


def _exec_autocommit(sql, params=None):
    """Run one statement in autocommit mode. See _autocommit_conn."""
    conn = _autocommit_conn(timeout=120)
    try:
        cur = conn.cursor()
        cur.execute(sql, params) if params is not None else cur.execute(sql)
    finally:
        conn.close()


def _sql_str(v):
    """A SQL string literal for embedding a value inside an EXEC('...') AT batch."""
    return "'" + str(v).replace("'", "''") + "'"


def normalize_phone(v):
    """A tbl_ownership phone -> a clean 10-digit string, or '' if it isn't a usable
    phone. Strips formatting (dashes/parens/spaces), a trailing '.0' from float-typed
    values, and a leading country '1'. Scientific-notation values (e.g. '7.7337e+009',
    stored with lost precision) are rejected — the same values the address match
    already excludes with NOT LIKE '%e%'."""
    s = str(v or "").strip()
    if "e" in s.lower():
        return ""
    if s.endswith(".0"):
        s = s[:-2]
    d = "".join(ch for ch in s if ch.isdigit())
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return d if len(d) == 10 else ""


def _push_matches(cur, table, srccol, valcol, rows, existing, normalize=None):
    """Insert (result_id, value) rows into a remote match_* table via EXEC(...) AT,
    one value per result_id, skipping result_ids already present (in `existing`).
    `rows` are dicts with keys 'rid' and 'val'. `normalize`, if given, cleans each
    value and drops rows it maps to ''. Returns the number inserted."""
    seen = set(existing)
    todo = []
    for r in rows:
        rid, val = r.get("rid"), r.get("val")
        v = str(val).strip() if val is not None else ""
        if normalize:
            v = normalize(v)
        if rid is None or not v or rid in seen:
            continue
        seen.add(rid)
        todo.append((int(rid), v))
    n = 0
    for i in range(0, len(todo), 200):
        chunk = todo[i:i + 200]
        vals = ",".join("(%d,1,%s,getdate())" % (rid, _sql_str(v)) for rid, v in chunk)
        remote = ("INSERT INTO %s.dbo.%s (result_id,%s,%s,created) VALUES %s"
                  % (DB, table, srccol, valcol, vals))
        try:
            cur.execute("EXEC (%s) AT [" + LINKED_SERVER + "]", (remote,))
            n += len(chunk)
        except Exception:
            pass
    return n


def populate_match_tables():
    """Enrich contact by matching Equifax trigger-view rows that carry no
    AppendedPhone to panafax tbl_ownership, writing any phone/email found into
    match_phone / match_email (which the lead waterfall then prefers). Mirrors the
    operator matching query:

      * phone from tbl_ownership.primary_phone, matched by appended-email, or by
        last name + address1 + zip;
      * email from tbl_ownership.email, matched by last name + address1(8) + zip.

    Idempotent: a result_id already present in a target table is skipped, so
    re-running never duplicates. Returns (phones_inserted, emails_inserted). Writes
    go through EXEC(...) AT in autocommit (a cross-server INSERT needs MSDTC)."""
    view = "[%s].%s.dbo.vw_EquifaxConsumerRecordTriggers" % (LINKED_SERVER, DB)
    own = "panafax.dbo.tbl_ownership"
    try:
        conn = _autocommit_conn()
    except Exception:
        return (0, 0)
    cur = conn.cursor(as_dict=True)

    def rows_of(sql):
        cur.execute(sql)
        return cur.fetchall()

    try:
        # Pull the un-appended-phone trigger rows into local temp tables (one remote
        # read each), then join locally against the 340M-row ownership table.
        cur.execute("IF OBJECT_ID('tempdb..#mpEmail') IS NOT NULL DROP TABLE #mpEmail")
        cur.execute("SELECT result_id, AppendedEmail INTO #mpEmail FROM %s WITH (NOLOCK) "
                    "WHERE AppendedPhone IS NULL AND AppendedEmail IS NOT NULL" % view)
        cur.execute("IF OBJECT_ID('tempdb..#mpAddr') IS NOT NULL DROP TABLE #mpAddr")
        cur.execute("SELECT DISTINCT result_id, LastName, Address1, ZipCode INTO #mpAddr "
                    "FROM %s WITH (NOLOCK) WHERE AppendedPhone IS NULL" % view)

        phone_rows = rows_of(
            "SELECT DISTINCT e.result_id rid, o.primary_phone val "
            "FROM %s o WITH (NOLOCK) JOIN #mpEmail e ON e.AppendedEmail = o.email "
            "WHERE o.primary_phone IS NOT NULL" % own)
        phone_rows += rows_of(
            "SELECT DISTINCT a.result_id rid, o.primary_phone val "
            "FROM %s o WITH (NOLOCK) JOIN #mpAddr a "
            "ON a.Address1 = o.address1 AND a.ZipCode = o.zip AND a.LastName = o.last_name "
            "WHERE o.primary_phone IS NOT NULL AND o.primary_phone NOT LIKE '%%e%%' "
            "AND o.primary_phone <> ''" % own)
        email_rows = rows_of(
            "SELECT DISTINCT a.result_id rid, o.email val "
            "FROM %s o WITH (NOLOCK) JOIN #mpAddr a "
            "ON LEFT(a.Address1, 8) = LEFT(o.address1, 8) AND a.ZipCode = o.zip "
            "AND a.LastName = o.last_name "
            "WHERE o.email IS NOT NULL AND o.email <> ''" % own)

        have_ph = {r["rid"] for r in rows_of(
            "SELECT DISTINCT result_id rid FROM [%s].%s.dbo.match_phone" % (LINKED_SERVER, DB))}
        have_em = {r["rid"] for r in rows_of(
            "SELECT DISTINCT result_id rid FROM [%s].%s.dbo.match_email" % (LINKED_SERVER, DB))}

        n_ph = _push_matches(cur, "match_phone", "source", "phone", phone_rows, have_ph,
                             normalize=normalize_phone)
        n_em = _push_matches(cur, "match_email", "source_id", "email", email_rows, have_em)
        return (n_ph, n_em)
    except Exception:
        return (0, 0)
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
