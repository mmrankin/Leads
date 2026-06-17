"""Validate customer email addresses via the SendGrid Email Validation API.

Separate API/key from mail send. Fails OPEN: any error or non-2xx response
results in the email being accepted, so a validator hiccup never drops a lead.

Env: SENDGRID_VALIDATION_API_KEY, EMAIL_REJECT_LEVEL (invalid|risky).
"""

import os

import requests

VALIDATION_URL = "https://api.sendgrid.com/v3/validations/email"


def validate_email(email, source="trade in widget"):
    email = (email or "").strip()
    if not email:
        return {"accept": True, "verdict": "Unknown", "score": None,
                "detail": "No email provided."}
    api_key = os.environ.get("SENDGRID_VALIDATION_API_KEY")
    if not api_key:
        return {"accept": True, "verdict": "Unknown", "score": None,
                "detail": "Validation not configured; accepted without check."}
    reject_level = os.environ.get("EMAIL_REJECT_LEVEL", "invalid").lower()
    try:
        resp = requests.post(
            VALIDATION_URL, json={"email": email, "source": source},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=10,
        )
    except requests.RequestException as exc:
        return {"accept": True, "verdict": "Unknown", "score": None,
                "detail": f"Validation request error (fail-open): {exc}"}
    if not (200 <= resp.status_code < 300):
        return {"accept": True, "verdict": "Unknown", "score": None,
                "detail": f"Validation HTTP {resp.status_code} (fail-open)."}
    try:
        result = resp.json().get("result", {})
        verdict = result.get("verdict", "Unknown")
        score = result.get("score")
    except (ValueError, AttributeError):
        return {"accept": True, "verdict": "Unknown", "score": None,
                "detail": "Validation response unparseable (fail-open)."}
    v = (verdict or "").lower()
    if v == "invalid":
        accept = False
    elif v == "risky":
        accept = reject_level != "risky"
    else:
        accept = True
    return {"accept": accept, "verdict": verdict, "score": score,
            "detail": f"verdict={verdict} score={score}"}
