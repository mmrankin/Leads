"""Year/Make/Model lookups from SQL Server: vin_decode..VIN_Data1_YMM.

Columns: year (int), make, model, trim. Powers the cascading dropdowns on the
lead form. Results are cached in-process (this is slow-changing reference data),
and every query uses WITH (NOLOCK) + a MAXDOP cap to stay safe on production.

Configuration (environment variables):
    VIN_DB_SERVER     SQL Server host (e.g. 10.1.1.10). Blank disables lookups.
    VIN_DB_USER       SQL login.
    VIN_DB_PASSWORD   SQL password.
    VIN_DB_DATABASE   Database (default: vin_decode).
"""

import os
import threading

try:
    import pymssql
except ImportError:  # lookups simply disabled if the driver isn't present
    pymssql = None

TABLE = "dbo.VIN_Data1_YMM"
_CACHE = {}
_LOCK = threading.Lock()


def is_enabled():
    return bool(pymssql and os.environ.get("VIN_DB_SERVER"))


def _connect():
    return pymssql.connect(
        server=os.environ["VIN_DB_SERVER"],
        user=os.environ.get("VIN_DB_USER"),
        password=os.environ.get("VIN_DB_PASSWORD"),
        database=os.environ.get("VIN_DB_DATABASE", "vin_decode"),
        timeout=30,
        login_timeout=10,
    )


def _query(sql, params=()):
    con = _connect()
    try:
        cur = con.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        con.close()


def _cached(key, loader):
    if key in _CACHE:
        return _CACHE[key]
    with _LOCK:
        if key in _CACHE:  # double-check after acquiring the lock
            return _CACHE[key]
        value = loader()
        _CACHE[key] = value
        return value


def get_years():
    """Distinct model years, newest first (junk year 0 excluded)."""
    def load():
        rows = _query(
            f"SELECT DISTINCT year FROM {TABLE} WITH (NOLOCK) "
            f"WHERE year > 0 ORDER BY year DESC OPTION (MAXDOP 1)"
        )
        return [r[0] for r in rows]
    return _cached("years", load)


def get_makes(year):
    """Distinct makes for a year, alphabetical."""
    try:
        year = int(year)
    except (TypeError, ValueError):
        return []

    def load():
        rows = _query(
            f"SELECT DISTINCT make FROM {TABLE} WITH (NOLOCK) "
            f"WHERE year = %s AND make IS NOT NULL AND make <> '' "
            f"ORDER BY make OPTION (MAXDOP 1)",
            (year,),
        )
        return [r[0] for r in rows]
    return _cached(("makes", year), load)


def get_models(year, make):
    """Distinct models for a year + make, alphabetical."""
    try:
        year = int(year)
    except (TypeError, ValueError):
        return []
    make = (make or "").strip()
    if not make:
        return []

    def load():
        rows = _query(
            f"SELECT DISTINCT model FROM {TABLE} WITH (NOLOCK) "
            f"WHERE year = %s AND make = %s AND model IS NOT NULL AND model <> '' "
            f"ORDER BY model OPTION (MAXDOP 1)",
            (year, make),
        )
        return [r[0] for r in rows]
    return _cached(("models", year, make), load)
