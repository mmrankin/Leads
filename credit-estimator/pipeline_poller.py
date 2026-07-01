#!/usr/bin/env python3
"""Credit Pipeline lead poller — run every minute (launchd/cron).

When the global Credit Pipeline flow switch is ON, this pulls matched, not-yet-
sent trigger leads from the CreditPipline feed (via the 10.1.4.7 linked server;
matched by retailer_code = dealer_id and excluded via the dlrPro `sent` ledger),
and — for dealers holding an active CREDIT_PIPELINE grant — emails an ADF/XML
lead, stores it in credit_leads, and records the send in `sent`.

The same send_lead() is used by the admin "Send Lead" button. The switch
defaults OFF — nothing is sent until an admin turns it on.

Usage:
    python pipeline_poller.py             # normal run
    python pipeline_poller.py --dry-run   # resolve + log only; never send/store
"""

import json
import logging
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# Make the shared platform package importable (same pattern as app.py).
PLATFORM_DIR = os.environ.get(
    "PLATFORM_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "platform"),
)
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

import platform_db as pdb
import db
import pipeline_source
from adf import build_adf
from email_send import send_adf

LOG = logging.getLogger("pipeline_poller")
SOURCE = "Credit Pipeline"
BATCH = int(os.environ.get("CREDITPIPELINE_BATCH", "1000"))


def _subsource(payload):
    """Sub-source = matched_payload.trigger_desc (e.g. 'Auto Prequalification Inquiry')."""
    try:
        data = json.loads(payload) if payload else {}
    except (ValueError, TypeError):
        return None
    return (data.get("trigger_desc") or "").strip() or None


def _phone(row):
    for key in ("cell_phone", "home_phone", "work_phone"):
        val = (row.get(key) or "").strip()
        if val:
            return val
    return ""


def eligible(row):
    """A fetched row is eligible if the dealer holds an active CREDIT_PIPELINE
    grant and the customer has an email or phone. Returns (ok, reason)."""
    if not pdb.dealer_has_product(row.get("dealer_id"), pdb.PRODUCT_CREDIT_PIPELINE):
        return False, "no active CREDIT_PIPELINE grant"
    if not (row.get("email_address") or "").strip() and not _phone(row):
        return False, "no email or phone"
    return True, None


def send_lead(row):
    """Build + send one Credit Pipeline lead from a fetched match row, store it in
    credit_leads, and record it in the `sent` ledger. Returns (status, detail,
    lead_id). Assumes the row is already eligible()."""
    dealer_id = row["dealer_id"]
    dealer = pdb.get_dealer(dealer_id)
    if not dealer:
        return "skipped_no_dealer", f"dealer {dealer_id} not found", None
    subsource = _subsource(row.get("matched_payload"))

    lead = {
        "dealer_id": dealer_id,
        "first_name": (row.get("first_name") or "").strip(),
        "last_name": (row.get("last_name") or "").strip(),
        "email": (row.get("email_address") or "").strip(),
        "phone": _phone(row),
        "address": (row.get("address") or "").strip() or None,
        "city": (row.get("city") or "").strip() or None,
        "state": (row.get("state") or "").strip() or None,
        "zip": (row.get("zip") or "").strip() or None,
        "vehicle_year": str(row.get("vehicle_year") or "").strip() or None,
        "vehicle_make": (row.get("vehicle_make") or "").strip() or None,
        "vehicle_model": (row.get("vehicle_model") or "").strip() or None,
        "comments": f"Credit Pipeline match (result #{row['result_id']}).",
        "source": SOURCE, "subsource": subsource,
        "tc_agreed": None, "tc_agreed_at": None,
        "email_verdict": None, "email_score": None,
        "adf_xml": None, "email1_status": "pending", "email1_detail": None,
    }
    lead_id = db.insert_lead(lead)
    lead["id"] = lead_id
    adf_xml = build_adf(lead, dealer, estimate=None,
                        product_code=pdb.PRODUCT_CREDIT_PIPELINE, subsource=subsource)
    db.update_adf(lead_id, adf_xml)
    status, detail = send_adf(dealer, adf_xml, lead_id=lead_id, lead=lead)
    db.set_email_status(lead_id, 1, status, detail)
    # Record in the sent ledger keyed by dealers.id + match result_id.
    pdb.record_sent(row["dealers_id"], row["result_id"])
    return status, detail, lead_id


def run(dry_run=False):
    pdb.init_db()
    if not pdb.get_pipeline_flow():
        LOG.info("Credit Pipeline flow is OFF — nothing to do.")
        return
    rows = pipeline_source.fetch_unsent(limit=BATCH)
    if not rows:
        LOG.info("No matched, unsent trigger leads.")
        return

    counts = {}
    for row in rows:
        ok, reason = eligible(row)
        if not ok:
            counts["skipped"] = counts.get("skipped", 0) + 1
            LOG.info("result %s (%s) -> skipped: %s", row["result_id"], row["dealer_id"], reason)
            continue
        if dry_run:
            counts["dry_run"] = counts.get("dry_run", 0) + 1
            LOG.info("result %s -> would send to %s <%s>",
                     row["result_id"], row["dealer_id"], row.get("lead_email_address"))
            continue
        status, detail, lead_id = send_lead(row)
        counts[status] = counts.get(status, 0) + 1
        LOG.info("result %s -> %s (lead %s) %s", row["result_id"], status, lead_id, detail)

    LOG.info("Run complete (%s). Fetched %d: %s",
             "dry-run" if dry_run else "live", len(rows), counts)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    run(dry_run="--dry-run" in sys.argv[1:])
