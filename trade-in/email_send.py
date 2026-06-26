"""Deliver trade-in ADF/XML to dealers via the SendGrid API.

Two sends per lead: one after the contact step, an updated one (with condition
+ value) after the final step. If SendGrid isn't configured the ADF is written
to ./outbox/ so nothing is lost.

Env: SENDGRID_API_KEY, LEAD_FROM_EMAIL, LEAD_FROM_NAME.
"""

import base64
import os

import requests

SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
OUTBOX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outbox")

# Addresses BCC'd on every outbound ADF/XML lead email (env override: ADF_BCC, comma-separated).
ADF_BCC = [e.strip() for e in os.environ.get(
    "ADF_BCC",
    "justinstull@rmadataplus.com,robbazaren@rmadataplus.com,mark@rmadataplus.com",
).split(",") if e.strip()]


def _personalization(to_email):
    """One SendGrid personalization: the dealer in To, the RMA team BCC'd.
    Drops any BCC that duplicates the To address (SendGrid rejects duplicates)."""
    p = {"to": [{"email": to_email}]}
    bcc = [{"email": e} for e in ADF_BCC if e.lower() != to_email.lower()]
    if bcc:
        p["bcc"] = bcc
    return p


def send_html_email(to_email, subject, html, inline_png=None, inline_cid=None):
    """Send an HTML email to the customer (e.g. their serialized offer), with an
    optional inline PNG (the barcode) referenced via cid:. Returns (status,
    detail): 'sent' | 'failed' | 'pending'."""
    to_email = (to_email or "").strip()
    if not to_email:
        return "failed", "No recipient email."
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("LEAD_FROM_EMAIL")
    from_name = os.environ.get("LEAD_FROM_NAME", "Trade-In Widget")
    if not api_key or not from_email:
        return "pending", "SendGrid not configured."

    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [{"type": "text/html", "value": html}],
    }
    if inline_png and inline_cid:
        payload["attachments"] = [{
            "content": base64.b64encode(inline_png).decode("ascii"),
            "type": "image/png",
            "filename": f"{inline_cid}.png",
            "disposition": "inline",
            "content_id": inline_cid,
        }]
    try:
        resp = requests.post(
            SENDGRID_URL, json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=20,
        )
    except requests.RequestException as exc:
        return "failed", f"SendGrid request error: {exc}"
    if 200 <= resp.status_code < 300:
        return "sent", f"SendGrid accepted (HTTP {resp.status_code})"
    return "failed", f"SendGrid HTTP {resp.status_code}: {resp.text[:300]}"


def _save_to_outbox(dealer, adf_xml, lead_id, tag):
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    path = os.path.join(OUTBOX_DIR, f"trade-{lead_id}-{tag}-{dealer.get('dealer_id')}.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(adf_xml)
    return path


def send_adf(dealer, adf_xml, lead_id, lead=None, updated=False):
    """Send the ADF/XML to the dealer's leadEmailAddress.

    Returns (status, detail): 'sent' | 'failed' | 'pending'.
    """
    to_email = (dealer.get("lead_email_address") or "").strip()
    tag = "updated" if updated else "initial"
    if not to_email:
        return "failed", "Dealer has no leadEmailAddress configured."

    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("LEAD_FROM_EMAIL")
    from_name = os.environ.get("LEAD_FROM_NAME", "Trade-In Widget")

    if not api_key or not from_email:
        path = _save_to_outbox(dealer, adf_xml, lead_id, tag)
        return "pending", f"SendGrid not configured; ADF saved to {path}"

    name = ""
    if lead:
        name = " ".join(x for x in (lead.get("first_name"), lead.get("last_name")) if x).strip()
    subject = "Trade-In Lead" + (" (updated)" if updated else "")
    if name:
        subject += f" - {name}"
    subject += f" - {dealer.get('dealer_name', '')}"

    # The ADF/XML rides in the email body; no separate attachment is needed.
    payload = {
        "personalizations": [_personalization(to_email)],
        "from": {"email": from_email, "name": from_name},
        "subject": subject.strip(),
        "content": [{"type": "text/plain",
                     "value": ("A trade-in lead was submitted. ADF/XML included below.\n\n"
                               + adf_xml)}],
    }
    try:
        resp = requests.post(
            SENDGRID_URL, json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=20,
        )
    except requests.RequestException as exc:
        path = _save_to_outbox(dealer, adf_xml, lead_id, tag)
        return "failed", f"SendGrid request error: {exc}; ADF saved to {path}"
    if 200 <= resp.status_code < 300:
        return "sent", f"SendGrid accepted (HTTP {resp.status_code})"
    path = _save_to_outbox(dealer, adf_xml, lead_id, tag)
    return "failed", f"SendGrid HTTP {resp.status_code}: {resp.text[:300]}; ADF saved to {path}"
