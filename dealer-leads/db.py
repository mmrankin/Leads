"""Lead storage for the dealer lead form — backed by SQL Server `dlrPro`.

Uses the shared dlrpro_db helper (on the platform dir, which app.py puts on
sys.path). Public function names/signatures are unchanged from the SQLite version.
"""

import dlrpro_db as dlr

_FIELDS = (
    "dealer_id", "first_name", "last_name", "email", "phone",
    "comments", "vehicle_year", "vehicle_make", "vehicle_model",
    "source", "adf_xml", "email_status", "email_detail",
    "email_verdict", "email_score",
)


def init_db():
    """No-op: the `leads` table lives in dlrPro (created by migrate_to_dlrpro.py)."""
    return


def insert_lead(data):
    """Insert a lead and return its new id."""
    v = {k: data.get(k) for k in _FIELDS}
    cols = ", ".join(_FIELDS)
    ph = ", ".join(f"%({k})s" for k in _FIELDS)
    return dlr.insert(f"INSERT INTO leads ({cols}) VALUES ({ph})", v)


def update_lead_email_status(lead_id, status, detail=None):
    dlr.execute(
        "UPDATE leads SET email_status=%(s)s, email_detail=%(d)s WHERE id=%(id)s",
        {"s": status, "d": detail, "id": lead_id})
