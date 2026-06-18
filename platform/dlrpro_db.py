"""Shared SQL Server data access for the dealer platform (the `dlrPro` database
on 10.1.1.10). Replaces the old per-app SQLite stores for real data (dealers,
product access, settings, and all leads). Trade-in performance caches stay in
local SQLite.

Imported by platform_db.py and each app's db.py (the platform dir is on sys.path).

Env: DLRPRO_DB_SERVER (10.1.1.10), DLRPRO_DB_USER (sa), DLRPRO_DB_PASSWORD,
     DLRPRO_DB_NAME (dlrPro).
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import pymssql

# Server-side expression for a string timestamp matching the old SQLite
# datetime('now') format ('YYYY-MM-DD HH:MM:SS', UTC).
NOW = "CONVERT(varchar(19), SYSUTCDATETIME(), 120)"


def _conn():
    return pymssql.connect(
        server=os.environ.get("DLRPRO_DB_SERVER", "10.1.1.10"),
        user=os.environ.get("DLRPRO_DB_USER", "sa"),
        password=os.environ.get("DLRPRO_DB_PASSWORD", ""),
        database=os.environ.get("DLRPRO_DB_NAME", "dlrPro"),
        timeout=30, login_timeout=10,
    )


def query(sql, params=None):
    """Return list[dict]. Use %(name)s placeholders + a dict for params."""
    c = _conn()
    try:
        cur = c.cursor(as_dict=True)
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        return cur.fetchall()
    finally:
        c.close()


def one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    c = _conn()
    try:
        cur = c.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        c.commit()
    finally:
        c.close()


def insert(sql, params=None):
    """Run an INSERT and return the new IDENTITY id."""
    c = _conn()
    try:
        cur = c.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        cur.execute("SELECT CAST(SCOPE_IDENTITY() AS INT) AS id")
        rid = cur.fetchone()[0]
        c.commit()
        return int(rid) if rid is not None else None
    finally:
        c.close()
