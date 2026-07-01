"""Read matched, not-yet-sent trigger leads from the CreditPipline feed.

CreditPipline lives on 10.1.4.7, a linked server on the dlrPro SQL instance
(10.1.1.10). We match a CreditPipline retailer to a dlrPro dealer by
retailer_code = dealer_id, join the customer contact, and exclude anything
already in the dlrPro `sent` ledger. Runs through the dlrPro connection.

Env:
    CREDITPIPELINE_LINKED_SERVER   linked-server name on dlrPro (default 10.1.4.7)
    CREDITPIPELINE_DB              database name (default CreditPipline)
"""

import os

import dlrpro_db as dlr

LINKED_SERVER = os.environ.get("CREDITPIPELINE_LINKED_SERVER", "10.1.4.7")
DB = os.environ.get("CREDITPIPELINE_DB", "CreditPipline")

# Matched dealer (retailer_code = dealer_id) + matched customer (has a surname),
# not yet in dbo.sent. dealers_id is dealers.id (the value stored in sent).
_FETCH_SQL = """SELECT TOP {limit}
  m.result_id, m.matched_payload, m.consumer_zip,
  d.id AS dealers_id, d.dealer_id, d.dealer_name, d.lead_email_address,
  c.first_name, c.last_name, c.email_address,
  c.cell_phone, c.home_phone, c.work_phone,
  c.address_line1 AS address, c.city, c.state, c.postal_code AS zip,
  c.year AS vehicle_year, c.make AS vehicle_make, c.model AS vehicle_model
FROM [{ls}].[{db}].[dbo].[match_result] m
JOIN [{ls}].[{db}].[dbo].[customer_record] c ON c.customer_record_id = m.customer_record_id
JOIN [{ls}].[{db}].[dbo].[retailer] r ON r.retailer_id = m.retailer_id
JOIN dlrPro.dbo.dealers d ON d.dealer_id = r.retailer_code
LEFT JOIN dlrPro.dbo.[sent] s ON s.dealer_id = d.id AND s.result_id = m.result_id
WHERE s.id IS NULL AND c.last_name IS NOT NULL
ORDER BY m.result_id ASC"""


def fetch_unsent(limit=1000):
    """Return matched, not-yet-sent trigger-lead rows (list[dict])."""
    return dlr.query(_FETCH_SQL.format(limit=int(limit), ls=LINKED_SERVER, db=DB))


def fetch_one(result_id):
    """One matched, not-yet-sent row for a specific result_id, or None."""
    sql = _FETCH_SQL.format(limit=1, ls=LINKED_SERVER, db=DB).replace(
        "WHERE s.id IS NULL AND c.last_name IS NOT NULL",
        "WHERE s.id IS NULL AND c.last_name IS NOT NULL AND m.result_id = %(rid)s")
    rows = dlr.query(sql, {"rid": int(result_id)})
    return rows[0] if rows else None
