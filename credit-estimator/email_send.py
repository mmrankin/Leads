"""Deliver ADF/XML leads to dealers via the SendGrid API.

Configuration (environment variables):
    SENDGRID_API_KEY   SendGrid API key. If unset, sends are skipped and the
                       ADF is saved to ./outbox/ so nothing is lost.
    LEAD_FROM_EMAIL    Verified sender address (required by SendGrid).
    LEAD_FROM_NAME     Optional sender display name.
"""

import os

import requests

SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
OUTBOX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outbox")

# BCC'd on Credit Pipeline lead emails (env override: ADF_BCC, comma-separated).
ADF_BCC = [e.strip() for e in os.environ.get(
    "ADF_BCC",
    "justinstull@rmadataplus.com,robbazaren@rmadataplus.com,mark@rmadataplus.com",
).split(",") if e.strip()]

# BCC'd on Credit Estimator (/c/) lead emails — this app serves both products
# (env override: CREDIT_EST_BCC). Estimator drops the RMA-team BCC.
ESTIMATOR_BCC = [e.strip() for e in os.environ.get(
    "CREDIT_EST_BCC", "mark@panafax.ai").split(",") if e.strip()]

# Reply-To on every outbound lead email (env override: LEAD_REPLY_TO).
LEAD_REPLY_TO = os.environ.get("LEAD_REPLY_TO", "noreply@rmadataplus.com")


def _personalization(to_email, bcc_list):
    """One SendGrid personalization: the dealer in To, the given list BCC'd.
    Drops any BCC that duplicates the To address (SendGrid rejects duplicates)."""
    p = {"to": [{"email": to_email}]}
    bcc = [{"email": e} for e in bcc_list if e.lower() != to_email.lower()]
    if bcc:
        p["bcc"] = bcc
    return p


def _save_to_outbox(dealer, adf_xml, lead_id):
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    path = os.path.join(OUTBOX_DIR, f"lead-{lead_id}-{dealer.get('dealer_id')}.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(adf_xml)
    return path


def _lead_bcc():
    """Admin-editable BCC list from the DB (platform_settings), falling back to
    the built-in ADF_BCC default if the DB isn't reachable."""
    try:
        import platform_db as _pdb
        return _pdb.get_lead_bcc()
    except Exception:
        return ADF_BCC


def send_adf(dealer, adf_xml, lead_id, lead=None, bcc=None):
    """Send the ADF/XML to the dealer's leadEmailAddress. bcc overrides the BCC
    list (defaults to the admin-editable Credit Pipeline lead BCC).

    Returns (status, detail) where status is 'sent', 'failed', or 'pending'.
    """
    bcc_list = _lead_bcc() if bcc is None else bcc
    to_email = (dealer.get("lead_email_address") or "").strip()
    if not to_email:
        return "failed", "Dealer has no leadEmailAddress configured."

    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("LEAD_FROM_EMAIL")
    from_name = os.environ.get("LEAD_FROM_NAME", "Dealer Lead Form")

    # Not configured yet: persist the ADF so it can be sent later.
    if not api_key or not from_email:
        path = _save_to_outbox(dealer, adf_xml, lead_id)
        return "pending", f"SendGrid not configured; ADF saved to {path}"

    name = ""
    if lead:
        name = " ".join(
            x for x in (lead.get("first_name"), lead.get("last_name")) if x
        ).strip()
    subject = f"New Lead{(' - ' + name) if name else ''} - {dealer.get('dealer_name', '')}".strip()

    # The ADF/XML rides in the email body; no separate attachment is needed.
    payload = {
        "personalizations": [_personalization(to_email, bcc_list)],
        "from": {"email": from_email, "name": from_name},
        "reply_to": {"email": LEAD_REPLY_TO},
        "subject": subject,
        "content": [
            {
                "type": "text/plain",
                "value": adf_xml,
            }
        ],
    }

    try:
        resp = requests.post(
            SENDGRID_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        path = _save_to_outbox(dealer, adf_xml, lead_id)
        return "failed", f"SendGrid request error: {exc}; ADF saved to {path}"

    if 200 <= resp.status_code < 300:
        return "sent", f"SendGrid accepted (HTTP {resp.status_code})"

    path = _save_to_outbox(dealer, adf_xml, lead_id)
    return "failed", (
        f"SendGrid HTTP {resp.status_code}: {resp.text[:500]}; ADF saved to {path}"
    )
