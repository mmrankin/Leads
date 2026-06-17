"""SQLite access helpers for the trade-in widget."""

import os
import sqlite3

DB_PATH = os.environ.get(
    "TRADEIN_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_in.db"),
)
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    conn = get_conn()
    try:
        conn.executescript(schema)
        # Migrations for DBs created before the valuation columns existed.
        for col, decl in (("value_estimate", "REAL"), ("value_low", "REAL"),
                          ("value_high", "REAL"), ("value_source", "TEXT"),
                          ("serial", "TEXT")):
            try:
                conn.execute(f"ALTER TABLE trade_leads ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # already exists
        conn.commit()
    finally:
        conn.close()


# ----- trade leads -----

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
    values = {k: data.get(k) for k in CONTACT_FIELDS}
    cols = ", ".join(CONTACT_FIELDS)
    params = ", ".join(f":{k}" for k in CONTACT_FIELDS)
    conn = get_conn()
    try:
        cur = conn.execute(
            f"INSERT INTO trade_leads ({cols}) VALUES ({params})", values
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_trade_lead(lead_id):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM trade_leads WHERE id = ?", (lead_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_trade_lead_by_serial(serial):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM trade_leads WHERE serial = ?", (serial,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_trade_condition(lead_id, data):
    """Apply step-3 condition answers + the refreshed ADF payload + valuation."""
    fields = CONDITION_FIELDS + ("adf_xml", "value_estimate", "value_low",
                                 "value_high", "value_source")
    values = {k: data.get(k) for k in fields}
    values["id"] = lead_id
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE trade_leads SET {sets}, stage='complete', "
            f"updated_at=datetime('now') WHERE id = :id",
            values,
        )
        conn.commit()
    finally:
        conn.close()


# ----- squish VIN cache (year/make/model -> representative squish) -----

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


# ----- vin8 prefix cache (make/model -> set of distinct first-8 VIN prefixes) -----

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


# ----- inventory prefix counts (first-8 VIN -> # for sale) -----

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


def set_email_status(lead_id, which, status, detail=None):
    """which = 1 (after step 2) or 2 (after step 3)."""
    col = "email1_status" if which == 1 else "email2_status"
    dcol = "email1_detail" if which == 1 else "email2_detail"
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE trade_leads SET {col} = ?, {dcol} = ? WHERE id = ?",
            (status, detail, lead_id),
        )
        conn.commit()
    finally:
        conn.close()
