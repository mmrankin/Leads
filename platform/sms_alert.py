"""Send an SMS via Twilio's REST API (no SDK dependency — just requests).

Auth prefers an API Key (SID + Secret); falls back to the Account Auth Token.
All config comes from the environment (set in the prod .env, never in code):

    TWILIO_ACCOUNT_SID      ACxxxx — the account (used in the request URL)
    TWILIO_API_KEY_SID      SKxxxx — preferred auth username
    TWILIO_API_KEY_SECRET   the API key secret — preferred auth password
    TWILIO_AUTH_TOKEN       fallback password (with the Account SID as username)
    TWILIO_FROM             +1XXXXXXXXXX sending number
    STALL_ALERT_TO          +1XXXXXXXXXX default recipient
"""

import logging
import os

import requests

LOG = logging.getLogger("sms_alert")
_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _auth():
    ksid, ksec = os.environ.get("TWILIO_API_KEY_SID"), os.environ.get("TWILIO_API_KEY_SECRET")
    if ksid and ksec:
        return (ksid, ksec)
    acct, tok = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")
    if acct and tok:
        return (acct, tok)
    return None


def is_configured():
    return bool(os.environ.get("TWILIO_ACCOUNT_SID") and _auth() and os.environ.get("TWILIO_FROM"))


def send(body, to=None):
    """Send an SMS; returns (ok, detail). No-op (False, reason) if unconfigured."""
    acct = os.environ.get("TWILIO_ACCOUNT_SID")
    frm = os.environ.get("TWILIO_FROM")
    to = to or os.environ.get("STALL_ALERT_TO")
    auth = _auth()
    if not (acct and frm and to and auth):
        return False, "twilio not configured"
    try:
        r = requests.post(_API.format(sid=acct), auth=auth,
                          data={"To": to, "From": frm, "Body": body[:1500]}, timeout=15)
    except requests.RequestException as e:
        LOG.warning("twilio send error: %s", e)
        return False, f"request error: {e}"
    if 200 <= r.status_code < 300:
        return True, "sent"
    LOG.warning("twilio HTTP %s: %s", r.status_code, r.text[:300])
    return False, f"HTTP {r.status_code}: {r.text[:200]}"
