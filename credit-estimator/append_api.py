"""Phone + email append via the Integrated Marketing Datacenter API.

POST https://api.imdatacenter.com/1.0/address with header `x-api-key`, a name +
address, and process codes ["fp", "fe2"] (forward phone + forward email w/
validation — the only two we use). `immediate: true` asks for the result inline;
if the job is still queued we poll `GET /address/{id}` a few times.

Cost: $0.015 per phone match (fp) + $0.015 per email match (fe2). Every call is
logged (platform_db.record_append) for billing reconciliation, and the poller
calls it at most ONCE per record — so this module just does the HTTP.

Config (env, so no secrets in code):
    IMDC_API_KEY     the x-api-key value        (required — no key => disabled)
    IMDC_CLIENT_ID   your client_id             (required)
    IMDC_BASE_URL    override the endpoint       (default the address endpoint)
"""

import logging
import os
import time

import requests

LOG = logging.getLogger("append_api")

BASE_URL = os.environ.get("IMDC_BASE_URL", "https://api.imdatacenter.com/1.0/address")
API_KEY = os.environ.get("IMDC_API_KEY")
CLIENT_ID = os.environ.get("IMDC_CLIENT_ID")

_POLL_TRIES = 6          # ~ _POLL_TRIES * _POLL_WAIT seconds for a queued job
_POLL_WAIT = 2.0


def is_configured():
    """True when the API key + client_id are set (else append() no-ops)."""
    return bool(API_KEY and CLIENT_ID)


def _headers():
    return {"x-api-key": API_KEY, "Content-Type": "application/json"}


def _parse(data):
    """Pull the appended email + phone list out of a completed result. Keeps the
    API's phone order (first = primary)."""
    append = ((data or {}).get("result") or {}).get("append") or {}
    email = ((append.get("email") or {}).get("email") or "").strip() or None
    phones = []
    for p in ((append.get("phone") or {}).get("phones") or []):
        num = (p.get("number") or "").strip()
        if num:
            phones.append(num)
    return {"email": email, "phones": phones, "status": (data or {}).get("status")}


def _await_complete(data):
    """Return the result once complete. If `immediate` already returned it, use
    it; otherwise poll GET /address/{id}."""
    for _ in range(_POLL_TRIES):
        append = ((data or {}).get("result") or {}).get("append")
        if data.get("status") == "Complete" or append:
            return data
        job_id = data.get("id")
        if not job_id:
            return data
        time.sleep(_POLL_WAIT)
        try:
            r = requests.get(BASE_URL.rstrip("/") + "/" + str(job_id), headers=_headers(), timeout=30)
            data = r.json()
        except Exception as e:                       # noqa: BLE001 — best-effort poll
            LOG.warning("append poll failed (%s): %s", job_id, e)
            return data
    return data


def append(first_name, last_name, address, city, state, zip_code, middle_name=""):
    """Run the fp+fe2 append for a name + address. Returns
    {"email": str|None, "phones": [str, …], "status": str} or None when the API
    is not configured or the call fails."""
    if not is_configured():
        return None
    payload = {
        "address": {"address1": address or "", "address2": "",
                    "city": city or "", "state": state or "", "zip": zip_code or ""},
        "client_id": CLIENT_ID,
        "name": {"first_name": first_name or "", "last_name": last_name or "",
                 "middle_name": middle_name or "", "prefix_name": "", "suffix_name": ""},
        "process": ["fp", "fe2"],
        "immediate": True,
    }
    try:
        resp = requests.post(BASE_URL, json=payload, headers=_headers(), timeout=30)
    except requests.RequestException as e:
        LOG.warning("append request error: %s", e)
        return None
    if not (200 <= resp.status_code < 300):
        LOG.warning("append API HTTP %s: %s", resp.status_code, resp.text[:300])
        return None
    try:
        data = resp.json()
    except ValueError as e:
        LOG.warning("append API non-JSON response: %s", e)
        return None
    return _parse(_await_complete(data))
