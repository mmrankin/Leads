"""Validate customer email addresses via the SendGrid Email Validation API.

This is a SEPARATE API (and API key) from mail send. It returns a verdict of
"Valid", "Risky", or "Invalid" plus a 0..1 score.

Configuration (environment variables):
    SENDGRID_VALIDATION_API_KEY   Validation API key. If unset, validation is
                                  skipped (every email is accepted).
    EMAIL_REJECT_LEVEL            "invalid" (default) rejects only Invalid
                                  verdicts; "risky" rejects Risky and Invalid.

Design note: this FAILS OPEN. Any network error, non-2xx response (e.g. the
account lacks the paid Validation add-on), or unparseable body results in the
email being accepted — we never drop a real lead because the validator hiccuped.
"""

import os

import requests

VALIDATION_URL = "https://api.sendgrid.com/v3/validations/email"


def validate_email(email, source="dealer lead form"):
    """Return a result dict describing whether to accept the email.

    Keys:
        accept   bool   whether the form should accept this email
        verdict  str    "Valid" | "Risky" | "Invalid" | "Unknown"
        score    float|None
        detail   str    human-readable explanation (stored for auditing)
    """
    email = (email or "").strip()
    if not email:
        return {"accept": True, "verdict": "Unknown", "score": None,
                "detail": "No email provided; nothing to validate."}

    api_key = os.environ.get("SENDGRID_VALIDATION_API_KEY")
    if not api_key:
        return {"accept": True, "verdict": "Unknown", "score": None,
                "detail": "Validation not configured; accepted without check."}

    reject_level = os.environ.get("EMAIL_REJECT_LEVEL", "invalid").lower()

    try:
        resp = requests.post(
            VALIDATION_URL,
            json={"email": email, "source": source},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        return {"accept": True, "verdict": "Unknown", "score": None,
                "detail": f"Validation request error (fail-open): {exc}"}

    if not (200 <= resp.status_code < 300):
        return {"accept": True, "verdict": "Unknown", "score": None,
                "detail": f"Validation HTTP {resp.status_code} (fail-open): "
                          f"{resp.text[:200]}"}

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
    else:  # Valid or Unknown
        accept = True

    return {"accept": accept, "verdict": verdict, "score": score,
            "detail": f"verdict={verdict} score={score} reject_level={reject_level}"}
