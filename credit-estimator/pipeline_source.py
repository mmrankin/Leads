"""Read matched leads from the CreditPipline database.

CreditPipline lives on 10.1.4.7, which is a linked server on the dlrPro SQL
instance (10.1.1.10). We reach it read-only through OPENQUERY on the dlrPro
connection (dlrpro_db), so no separate credentials/route to 10.1.4.7 are needed.

Env:
    CREDITPIPELINE_LINKED_SERVER   linked-server name on dlrPro (default 10.1.4.7)
    CREDITPIPELINE_DB              database name (default CreditPipline)
"""

import os

import dlrpro_db as dlr

LINKED_SERVER = os.environ.get("CREDITPIPELINE_LINKED_SERVER", "10.1.4.7")
DB = os.environ.get("CREDITPIPELINE_DB", "CreditPipline")

# Runs ON the remote server (inside OPENQUERY). One row per matched result, with
# the consumer (customer_record) and dealer (retailer) joined in. Only result_ids
# above the caller's high-water mark, oldest first, capped at `limit`.
_INNER = (
    "SELECT TOP {limit} "
    " m.result_id, m.retailer_id, m.customer_record_id, m.matched_payload, "
    " m.consumer_zip, CONVERT(varchar(19), m.returned_at, 120) AS returned_at, "
    " c.first_name, c.last_name, c.email_address, "
    " c.cell_phone, c.home_phone, c.work_phone, "
    " c.year AS vehicle_year, c.make AS vehicle_make, c.model AS vehicle_model, "
    " c.vin, c.postal_code, r.retailer_name "
    "FROM {db}.dbo.match_result AS m WITH (NOLOCK) "
    "INNER JOIN {db}.dbo.customer_record AS c WITH (NOLOCK) "
    " ON c.customer_record_id = m.customer_record_id "
    "INNER JOIN {db}.dbo.retailer AS r WITH (NOLOCK) "
    " ON r.retailer_id = m.retailer_id "
    "WHERE m.result_id > {after} "
    "ORDER BY m.result_id"
)


def fetch_matches(after_result_id=0, limit=1000):
    """Return new matched-lead rows (list[dict]) with result_id > after_result_id."""
    inner = _INNER.format(limit=int(limit), db=DB, after=int(after_result_id))
    sql = f"SELECT * FROM OPENQUERY([{LINKED_SERVER}], '{inner.replace(chr(39), chr(39) * 2)}')"
    return dlr.query(sql)
