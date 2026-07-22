"""Trade-in data access.

HYBRID: trade leads live in SQL Server `dlrPro` (via the shared dlrpro_db
helper), while the valuation performance caches (squish_map / vin8_map /
inv_prefix_count) stay in a small LOCAL SQLite file for instant lookups.
Public function names/signatures are unchanged.
"""

import json
import os
import sqlite3

import dlrpro_db as dlr
from dlrpro_db import NOW

# Local SQLite — caches only.
DB_PATH = os.environ.get(
    "TRADEIN_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_in.db"),
)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Ensure the local cache tables exist. (trade_leads lives in dlrPro.)"""
    conn = get_conn()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS squish_map (
            year INTEGER NOT NULL, make TEXT NOT NULL, model TEXT NOT NULL,
            s8 TEXT NOT NULL, s2 TEXT NOT NULL, PRIMARY KEY (year, make, model));
        CREATE TABLE IF NOT EXISTS vin8_map (
            make TEXT NOT NULL, model TEXT NOT NULL, s8 TEXT NOT NULL,
            PRIMARY KEY (make, model, s8));
        CREATE INDEX IF NOT EXISTS idx_vin8_make_model ON vin8_map(make, model);
        CREATE TABLE IF NOT EXISTS inv_prefix_count (
            s8 TEXT PRIMARY KEY, cnt INTEGER NOT NULL);
        """)
        conn.commit()
    finally:
        conn.close()


# ----- trade leads (SQL Server / dlrPro) -----

CONTACT_FIELDS = (
    "dealer_id", "serial", "vehicle_year", "vehicle_make", "vehicle_model",
    "vehicle_trim", "first_name", "last_name", "email", "phone", "tc_agreed",
    "tc_agreed_at", "email_verdict", "email_score", "adf_xml",
    "email1_status", "email1_detail",
)

CONDITION_FIELDS = (
    "num_keys", "unrepaired_damage", "engine_light", "airbag_light",
    "brake_light", "aftermarket_exhaust", "aftermarket_engine",
    "aftermarket_stereo", "own_or_lease", "miles", "ownership_status",
    "loan_balance", "lease_months_remaining",
)


def insert_trade_lead(data):
    """Create the lead after step 2 (contact). Returns the new id."""
    v = {k: data.get(k) for k in CONTACT_FIELDS}
    cols = ", ".join(CONTACT_FIELDS)
    ph = ", ".join(f"%({k})s" for k in CONTACT_FIELDS)
    return dlr.insert(f"INSERT INTO trade_leads ({cols}) VALUES ({ph})", v)


def get_trade_lead(lead_id):
    return dlr.one("SELECT * FROM trade_leads WHERE id=%(id)s", {"id": lead_id})


def get_trade_lead_by_serial(serial):
    return dlr.one("SELECT * FROM trade_leads WHERE serial=%(s)s", {"s": serial})


def update_trade_condition(lead_id, data):
    """Apply step-3 condition answers + the refreshed ADF payload + valuation."""
    fields = CONDITION_FIELDS + ("adf_xml", "value_estimate", "value_low",
                                 "value_high", "value_source")
    v = {k: data.get(k) for k in fields}
    v["id"] = lead_id
    sets = ", ".join(f"{k}=%({k})s" for k in fields)
    dlr.execute(f"UPDATE trade_leads SET {sets}, stage='complete', "
                f"updated_at={NOW} WHERE id=%(id)s", v)


def set_email_status(lead_id, which, status, detail=None):
    """which = 1 (after step 2) or 2 (after step 3)."""
    col = "email1_status" if which == 1 else "email2_status"
    dcol = "email1_detail" if which == 1 else "email2_detail"
    dlr.execute(f"UPDATE trade_leads SET {col}=%(s)s, {dcol}=%(d)s WHERE id=%(id)s",
                {"s": status, "d": detail, "id": lead_id})


# ----- squish VIN cache (LOCAL SQLite: year/make/model -> representative squish) -----

def get_cached_squish(year, make, model):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT s8, s2 FROM squish_map WHERE year=? AND make=? AND model=?",
            (year, make, model),
        ).fetchone()
        return (row["s8"], row["s2"]) if row else None
    finally:
        conn.close()


def put_cached_squish(year, make, model, s8, s2):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO squish_map (year, make, model, s8, s2) "
            "VALUES (?, ?, ?, ?, ?)", (year, make, model, s8, s2))
        conn.commit()
    finally:
        conn.close()


def bulk_put_squish(rows):
    """rows: iterable of (year, make, model, s8, s2)."""
    conn = get_conn()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO squish_map (year, make, model, s8, s2) "
            "VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def squish_count():
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM squish_map").fetchone()[0]
    finally:
        conn.close()


# ----- vin8 prefix cache (LOCAL SQLite: make/model -> first-8 VIN prefixes) -----

def get_vin8_set(make, model):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT s8 FROM vin8_map WHERE make=? AND model=?", (make, model)
        ).fetchall()
        return [r["s8"] for r in rows]
    finally:
        conn.close()


def bulk_put_vin8(rows):
    """rows: iterable of (make, model, s8)."""
    conn = get_conn()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO vin8_map (make, model, s8) VALUES (?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def vin8_count():
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM vin8_map").fetchone()[0]
    finally:
        conn.close()


# ----- inventory prefix counts (LOCAL SQLite: first-8 VIN -> # for sale) -----

def get_inv_count(prefixes):
    """Sum of tbl_inventory counts across the given first-8 prefixes (local)."""
    if not prefixes:
        return 0
    conn = get_conn()
    try:
        qs = ",".join("?" for _ in prefixes)
        row = conn.execute(
            f"SELECT COALESCE(SUM(cnt),0) FROM inv_prefix_count WHERE s8 IN ({qs})",
            list(prefixes),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def replace_inv_counts(rows):
    """Replace the whole inventory-count cache. rows: iterable of (s8, cnt)."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM inv_prefix_count")
        conn.executemany(
            "INSERT OR REPLACE INTO inv_prefix_count (s8, cnt) VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def inv_count_rows():
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM inv_prefix_count").fetchone()[0]
    finally:
        conn.close()


# ----- wizard state (cookie-free) -----
#
# The trade widget is embedded cross-site in dealer pages, where Safari (and any
# browser blocking third-party cookies) refuses to carry the Flask session cookie
# between wizard steps — the form would bounce back to step 1. So the wizard's
# state also lives server-side, keyed by a random token ("sid") carried in the
# form POST / redirect URL instead of a cookie. Lives in dlrPro alongside the
# leads; rows are short-lived and purged by age.

def init_wizard_state():
    """Create the wizard-state table if it isn't there yet."""
    dlr.execute("""IF OBJECT_ID('dbo.trade_wizard_state','U') IS NULL
        CREATE TABLE dbo.trade_wizard_state (
            sid varchar(64) NOT NULL PRIMARY KEY,
            dealer_id varchar(50) NULL,
            data nvarchar(max) NULL,
            created datetime NOT NULL DEFAULT GETDATE(),
            updated datetime NOT NULL DEFAULT GETDATE())""")


def get_wizard_state(sid):
    """The stored dict for this token, or None when there's no row yet."""
    row = dlr.one("SELECT data FROM trade_wizard_state WHERE sid=%(s)s", {"s": sid})
    if not row or not row.get("data"):
        return None
    try:
        return json.loads(row["data"])
    except (ValueError, TypeError):
        return None


def put_wizard_state(sid, dealer_id, data):
    """Upsert the state for this token."""
    dlr.execute(
        "UPDATE trade_wizard_state SET data=%(d)s, dealer_id=%(dl)s, updated=GETDATE() "
        "WHERE sid=%(s)s "
        "IF @@ROWCOUNT = 0 "
        "INSERT INTO trade_wizard_state (sid, dealer_id, data) VALUES (%(s)s, %(dl)s, %(d)s)",
        {"s": sid, "dl": dealer_id, "d": json.dumps(data, default=str)})


def purge_wizard_state(hours=12):
    """Drop abandoned wizard state older than `hours`."""
    dlr.execute("DELETE FROM trade_wizard_state "
                "WHERE updated < DATEADD(hour, -%d, GETDATE())" % int(hours))
