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

# Addresses BCC'd on every outbound ADF/XML lead email (env override: ADF_BCC, comma-separated).
ADF_BCC = [e.strip() for e in os.environ.get(
    "ADF_BCC",
    "mark@panafax.ai",
).split(",") if e.strip()]

# Reply-To on every outbound lead email (env override: LEAD_REPLY_TO).
LEAD_REPLY_TO = os.environ.get("LEAD_REPLY_TO", "noreply@rmadataplus.com")


def _personalization(to_emails):
    """One SendGrid personalization: every configured dealer address in To, the
    RMA team BCC'd. Drops any BCC that duplicates a To address (SendGrid
    rejects duplicates)."""
    tos = [to_emails] if isinstance(to_emails, str) else list(to_emails)
    lowered = {t.lower() for t in tos}
    p = {"to": [{"email": t} for t in tos]}
    bcc = [{"email": e} for e in ADF_BCC if e.lower() not in lowered]
    if bcc:
        p["bcc"] = bcc
    return p


def _save_to_outbox(dealer, adf_xml, lead_id):
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    path = os.path.join(OUTBOX_DIR, f"lead-{lead_id}-{dealer.get('dealer_id')}.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(adf_xml)
    return path


def _lead_emails(dealer):
    """Every lead delivery address configured for the dealer (primary + the two
    optional extras). Falls back to leadEmailAddress alone if the shared helper
    isn't importable."""
    try:
        import platform_db as pdb
        return pdb.lead_emails(dealer)
    except Exception:
        e = (dealer.get("lead_email_address") or "").strip()
        return [e] if e else []


def _notify(dealer, lead, product_code):
    """Text the dealer's lead-alert numbers. Best-effort; never raises."""
    try:
        import lead_notify
        lead_notify.notify_lead_sent(dealer, lead, product_code)
    except Exception:
        pass


def send_adf(dealer, adf_xml, lead_id, lead=None, notify=False, product_code=None):
    """Send the ADF/XML to the dealer's leadEmailAddress.

    Returns (status, detail) where status is 'sent', 'failed', or 'pending'.
    """
    to_emails = _lead_emails(dealer)
    if not to_emails:
        return "failed", "Dealer has no leadEmailAddress configured."
    to_email = to_emails[0]

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
        "personalizations": [_personalization(to_emails)],
        "from": {"email": from_email, "name": from_name},
        "reply_to": {"email": LEAD_REPLY_TO},
        "subject": subject,
        "content": [
            {
                "type": "text/plain",
                "value": (
                    "A new lead was submitted. The ADF/XML payload is included "
                    "below.\n\n" + adf_xml
                ),
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
        if notify:
            _notify(dealer, lead, product_code)
        return "sent", f"SendGrid accepted (HTTP {resp.status_code})"

    path = _save_to_outbox(dealer, adf_xml, lead_id)
    return "failed", (
        f"SendGrid HTTP {resp.status_code}: {resp.text[:500]}; ADF saved to {path}"
    )
