"""SQLite access helpers for the dealer lead form."""

import os
import sqlite3

DB_PATH = os.environ.get(
    "LEADS_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads.db"),
)
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_conn():
    """Return a connection with row access by column name and FK enforcement."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables if they do not yet exist."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    conn = get_conn()
    try:
        conn.executescript(schema)
        # Lightweight migrations for DBs created before these columns existed.
        for col, decl in (
            ("email_verdict", "TEXT"),
            ("email_score", "REAL"),
        ):
            try:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
    finally:
        conn.close()


# ----- leads -----

def insert_lead(data):
    """Insert a lead and return its new id."""
    fields = (
        "dealer_id", "first_name", "last_name", "email", "phone",
        "comments", "vehicle_year", "vehicle_make", "vehicle_model",
        "source", "adf_xml", "email_status", "email_detail",
        "email_verdict", "email_score",
    )
    values = {k: data.get(k) for k in fields}
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO leads
                (dealer_id, first_name, last_name, email, phone, comments,
                 vehicle_year, vehicle_make, vehicle_model, source,
                 adf_xml, email_status, email_detail, email_verdict, email_score)
            VALUES
                (:dealer_id, :first_name, :last_name, :email, :phone, :comments,
                 :vehicle_year, :vehicle_make, :vehicle_model, :source,
                 :adf_xml, :email_status, :email_detail, :email_verdict, :email_score)
            """,
            values,
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_lead_email_status(lead_id, status, detail=None):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE leads SET email_status = ?, email_detail = ? WHERE id = ?",
            (status, detail, lead_id),
        )
        conn.commit()
    finally:
        conn.close()
