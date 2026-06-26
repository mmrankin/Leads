#!/usr/bin/env python3
"""Credit Pipeline lead poller — run every minute (launchd/cron).

When the global Credit Pipeline flow switch is ON, this pulls new matched
results from the CreditPipline DB (via the 10.1.4.7 linked server), maps each to
the platform dealer whose name matches the retailer, and emails an ADF/XML lead
to that dealer (source "Credit Pipeline"; sub-source from the matched payload's
trigger_desc). Each match_result.result_id is sent at most once.

The switch defaults OFF — nothing is sent until an admin turns it on.

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
    """Sub-source = matched_payload.trigger_desc (e.g. 'Auto Inquiry (combined)')."""
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


def process_row(row, dry_run=False):
    """Resolve + (unless dry-run) send one matched result.

    Returns (status, detail, dealer_id, lead_id, subsource). status is the
    send status ('sent'/'pending'/'failed') or a 'skipped_*' reason.
    """
    result_id = row["result_id"]
    retailer = (row.get("retailer_name") or "").strip()
    subsource = _subsource(row.get("matched_payload"))

    dealer = pdb.find_dealer_by_name(retailer)
    if not dealer:
        return "skipped_no_dealer", f"No platform dealer named '{retailer}'.", None, None, subsource
    dealer_id = dealer["dealer_id"]
    if not pdb.dealer_has_product(dealer_id, pdb.PRODUCT_CREDIT_EST):
        return ("skipped_no_grant",
                f"Dealer {dealer_id} has no active CREDIT_EST grant.",
                dealer_id, None, subsource)

    email = (row.get("email_address") or "").strip()
    phone = _phone(row)
    if not email and not phone:
        return "skipped_no_contact", "No email or phone on customer_record.", dealer_id, None, subsource

    lead = {
        "dealer_id": dealer_id,
        "first_name": (row.get("first_name") or "").strip(),
        "last_name": (row.get("last_name") or "").strip(),
        "email": email, "phone": phone,
        "vehicle_year": str(row.get("vehicle_year") or "").strip() or None,
        "vehicle_make": (row.get("vehicle_make") or "").strip() or None,
        "vehicle_model": (row.get("vehicle_model") or "").strip() or None,
        "comments": f"Credit Pipeline match (result #{result_id}).",
        "source": SOURCE, "subsource": subsource,
        "tc_agreed": None, "tc_agreed_at": None,
        "email_verdict": None, "email_score": None,
    }

    if dry_run:
        return ("dry_run",
                f"Would send to {dealer_id} <{dealer.get('lead_email_address')}>, "
                f"sub-source={subsource!r}",
                dealer_id, None, subsource)

    adf_xml = build_adf(lead, dealer, estimate=None, source=SOURCE, subsource=subsource)
    lead["adf_xml"] = adf_xml
    status, detail = send_adf(dealer, adf_xml, lead_id=f"cp-{result_id}", lead=lead)
    lead["email1_status"], lead["email1_detail"] = status, detail
    lead_id = db.insert_lead(lead)
    db.set_email_status(lead_id, 1, status, detail)
    return status, detail, dealer_id, lead_id, subsource


def run(dry_run=False):
    pdb.init_db()  # ensure products + pipeline tables exist
    if not pdb.get_pipeline_flow():
        LOG.info("Credit Pipeline flow is OFF — nothing to do.")
        return

    after = pdb.get_pipeline_watermark()
    rows = pipeline_source.fetch_matches(after_result_id=after, limit=BATCH)
    if not rows:
        LOG.info("No new matches after result_id %s.", after)
        return

    already = pdb.pipeline_sent_result_ids([r["result_id"] for r in rows])
    max_id, counts = after, {}
    for row in rows:
        rid = int(row["result_id"])
        max_id = max(max_id, rid)
        if rid in already:
            continue
        # Claim the result_id first so an interrupted run can't double-send it.
        if not dry_run and not pdb.pipeline_claim(rid, (row.get("retailer_name") or "").strip()):
            continue
        status, detail, dealer_id, lead_id, subsource = process_row(row, dry_run=dry_run)
        counts[status] = counts.get(status, 0) + 1
        if not dry_run:
            pdb.pipeline_finalize(rid, dealer_id, lead_id, subsource, status, detail)
        LOG.info("result %s -> %s (%s)", rid, status, detail)

    if not dry_run:
        pdb.set_pipeline_watermark(max_id)
    LOG.info("Run complete (%s). Fetched %d, new %d: %s",
             "dry-run" if dry_run else "live", len(rows), sum(counts.values()), counts)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    run(dry_run="--dry-run" in sys.argv[1:])
