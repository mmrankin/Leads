#!/usr/bin/env python3
"""Credit Pipeline lead poller — run every minute (launchd/cron).

When the global Credit Pipeline flow switch is ON, this pulls matched, not-yet-
sent trigger leads from the CreditPipeline feed (via the 10.1.4.8 linked server;
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
import pipeline_enrich
from adf import build_adf
from email_send import send_adf

LOG = logging.getLogger("pipeline_poller")
SOURCE = "Credit Pipeline"
BATCH = int(os.environ.get("CREDITPIPELINE_BATCH", "1000"))


# Equifax streaming-trigger code -> human description, for when the payload
# carries only trigger_id (the current feed sends no trigger_desc, and the
# CreditPipeline.dbo.TriggerTypes lookup is empty). Add codes here as they appear.
_TRIGGER_DESC = {
    "AUPRQ": "Auto Prequalification Inquiry",
}


def _subsource(payload):
    """The trigger type we're sending — emitted as the ADF <id sequence="2">
    source and <provider><service>. Prefer the payload's trigger_desc; else map
    its trigger_id (e.g. AUPRQ -> 'Auto Prequalification Inquiry'); else fall
    back to the raw trigger_id."""
    try:
        data = json.loads(payload) if payload else {}
    except (ValueError, TypeError):
        return None
    desc = (data.get("trigger_desc") or "").strip()
    if desc:
        return desc
    tid = (data.get("trigger_id") or "").strip()
    if not tid:
        return None
    return _TRIGGER_DESC.get(tid, tid)


def _phone(row):
    for key in ("cell_phone", "home_phone", "work_phone"):
        val = (row.get(key) or "").strip()
        if val:
            return val
    return ""


def _s(val):
    return str(val).strip() if val is not None else ""


def resolve_contact(row):
    """Name/address for a match row. Prefers the matched customer_record; when the
    customer_record_id doesn't exist (LEFT-join miss), falls back to the name and
    address carried in the matched_payload JSON. Email/phone come only from the
    customer_record (the payload carries none)."""
    payload = {}
    try:
        payload = json.loads(row.get("matched_payload") or "{}") or {}
    except (ValueError, TypeError):
        payload = {}

    def pick(row_key, payload_key):
        return _s(row.get(row_key)) or _s(payload.get(payload_key))

    return {
        "first_name": pick("first_name", "first_name"),
        "last_name": pick("last_name", "last_name"),
        "email": _s(row.get("email_address")),
        "phone": _phone(row),
        "address": pick("address", "address_line_1"),
        "city": pick("city", "city"),
        "state": pick("state", "state"),
        "zip": pick("zip", "consumer_zip") or _s(row.get("consumer_zip")),
    }


def eligible(row):
    """A fetched row is eligible if the dealer holds an active CREDIT_PIPELINE
    grant and we have a customer name (from the matched record or, when there is
    no customer_record, from the matched_payload). Returns (ok, reason)."""
    if not pdb.dealer_has_product(row.get("dealer_id"), pdb.PRODUCT_CREDIT_PIPELINE):
        return False, "no active CREDIT_PIPELINE grant"
    c = resolve_contact(row)
    if not c["last_name"] and not c["first_name"]:
        return False, "no customer name (record or payload)"
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
    c = resolve_contact(row)

    lead = {
        "dealer_id": dealer_id,
        "first_name": c["first_name"],
        "last_name": c["last_name"],
        "email": c["email"],
        "phone": c["phone"],
        "address": c["address"] or None,
        "city": c["city"] or None,
        "state": c["state"] or None,
        "zip": c["zip"] or None,
        "vehicle_year": str(row.get("vehicle_year") or "").strip() or None,
        "vehicle_make": (row.get("vehicle_make") or "").strip() or None,
        "vehicle_model": (row.get("vehicle_model") or "").strip() or None,
        "comments": None,
        "source": SOURCE, "subsource": subsource,
        "tc_agreed": 0, "tc_agreed_at": None,   # bureau trigger; no on-platform T&C (col is NOT NULL)
        "email_verdict": None, "email_score": None,
        "adf_xml": None, "email1_status": "pending", "email1_detail": None,
    }
    # Enrich the notes (waterfall): the Equifax credit view (by consumer_id) for
    # the credit/finance fields, else panafax..tbl_ownership; tbl_ownership always
    # supplies the vehicle/VIN/mileage/phone/email. Best-effort.
    try:
        pipeline_enrich.enrich_lead(lead, consumer_id=row.get("consumer_id"))
    except Exception as e:
        LOG.warning("enrichment failed for result %s: %s",
                    row.get("result_id"), e)
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
    month_sent = {}   # dealers.id -> Credit Pipeline leads already sent this month
    max_cap = {}      # dealer_id  -> monthly cap
    for row in rows:
        ok, reason = eligible(row)
        if not ok:
            counts["skipped"] = counts.get("skipped", 0) + 1
            LOG.info("result %s (%s) -> skipped: %s", row["result_id"], row["dealer_id"], reason)
            continue

        # Monthly per-dealer cap. Count this month's sends once per dealer, then
        # track the running total locally as we send.
        did, dcode = row["dealers_id"], row["dealer_id"]
        if did not in month_sent:
            month_sent[did] = pdb.sent_this_month(did)
        if dcode not in max_cap:
            max_cap[dcode] = pdb.pipeline_max_leads(dcode)
        cap = max_cap[dcode]
        if month_sent[did] >= cap:
            counts["capped"] = counts.get("capped", 0) + 1
            LOG.info("result %s (%s) -> skipped: monthly cap reached (%d/%d)",
                     row["result_id"], dcode, month_sent[did], cap)
            continue

        if dry_run:
            counts["dry_run"] = counts.get("dry_run", 0) + 1
            LOG.info("result %s -> would send to %s <%s> (%d/%d this month)",
                     row["result_id"], dcode, row.get("lead_email_address"), month_sent[did] + 1, cap)
            month_sent[did] += 1
            continue
        status, detail, lead_id = send_lead(row)
        counts[status] = counts.get(status, 0) + 1
        if status != "skipped_no_dealer":   # a send was recorded in the ledger
            month_sent[did] += 1
        LOG.info("result %s -> %s (lead %s) %s", row["result_id"], status, lead_id, detail)

    LOG.info("Run complete (%s). Fetched %d: %s",
             "dry-run" if dry_run else "live", len(rows), counts)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    run(dry_run="--dry-run" in sys.argv[1:])
