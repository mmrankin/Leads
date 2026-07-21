"""Text a dealer's alert numbers when a lead is delivered to them.

Shared by every lead app (dealer-leads, trade-in, credit-estimator/pipeline):
each dealer may configure up to two numbers (dealers.alert_phone_1/2) that get
a short Twilio SMS as soon as a lead email goes out. Best-effort by design —
a failed or unconfigured alert must never affect lead delivery.
"""

import logging

import platform_db as pdb
import sms_alert

LOG = logging.getLogger("lead_notify")

# Friendly product names for the alert text.
PRODUCT_LABEL = {
    pdb.PRODUCT_LEAD_FORM: "Lead Form",
    pdb.PRODUCT_TRADE_IN: "Trade-In",
    pdb.PRODUCT_CREDIT_EST: "Credit Estimator",
    pdb.PRODUCT_CREDIT_PIPELINE: "Credit Pipeline",
}


def _body(dealer, lead, product_code):
    """The alert text: what kind of lead, who it is, and how to reach them."""
    lead = lead or {}
    name = " ".join(str(lead.get(k) or "").strip()
                    for k in ("first_name", "last_name")).strip()
    kind = PRODUCT_LABEL.get(product_code, "New")
    parts = ["New %s lead" % kind]
    if name:
        parts.append("for %s" % name)
    line = " ".join(parts)
    contact = " ".join(x for x in (str(lead.get("phone") or "").strip(),
                                   str(lead.get("email") or "").strip()) if x)
    tail = " (%s)" % dealer.get("dealer_name") if dealer.get("dealer_name") else ""
    return (line + (" — " + contact if contact else "") + tail).strip()


def notify_lead_sent(dealer, lead=None, product_code=None):
    """SMS every configured alert number for this dealer. Returns the count
    actually sent. Never raises — lead delivery already succeeded by this
    point, so an alert problem is logged and swallowed."""
    try:
        phones = pdb.alert_phones(dealer)
        if not phones:
            return 0
        if not sms_alert.is_configured():
            LOG.info("lead alert skipped for %s: twilio not configured",
                     (dealer or {}).get("dealer_id"))
            return 0
        body = _body(dealer or {}, lead, product_code)
        sent = 0
        for to in phones:
            ok, detail = sms_alert.send(body, to=to)
            if ok:
                sent += 1
            else:
                LOG.warning("lead alert to %s failed: %s", to, detail)
        return sent
    except Exception as e:                       # never break a delivered lead
        LOG.warning("lead alert error for %s: %s", (dealer or {}).get("dealer_id"), e)
        return 0
