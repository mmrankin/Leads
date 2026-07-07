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

import dlrpro_db as dlr

LINKED_SERVER = os.environ.get("CREDITPIPELINE_LINKED_SERVER", "10.1.4.8")
DB = os.environ.get("CREDITPIPELINE_DB", "CreditPipeline")

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


def fetch_unsent(limit=1000):
    """Matched, not-yet-sent trigger-lead rows for dealers CURRENTLY on the
    CREDIT_PIPELINE product, excluding records that already hit the no-contact
    retry cap."""
    sql = _FETCH_SQL.format(limit=int(limit), ls=LINKED_SERVER, db=DB,
                            skip_cond="AND (sk.attempts IS NULL OR sk.attempts < %d)"
                            % MAX_NO_CONTACT_ATTEMPTS,
                            grant_cond=_ACTIVE_GRANT_COND)
    return dlr.query(sql)


def fetch_one(result_id):
    """One matched, not-yet-sent row for a specific result_id, or None. Ignores the
    retry cap AND the active-grant filter so an admin can always attempt a manual
    send (the poller's automatic path still enforces both)."""
    sql = _FETCH_SQL.format(limit=1, ls=LINKED_SERVER, db=DB,
                            skip_cond="AND m.result_id = %(rid)s", grant_cond="")
    rows = dlr.query(sql, {"rid": int(result_id)})
    return rows[0] if rows else None
