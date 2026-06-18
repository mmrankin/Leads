"""Shared contact PII across the *.dlrpro.com product apps (lead / trade / credit).

When a customer enters their name/email/phone in any one form, we stash it in a
cookie scoped to the parent domain (`.dlrpro.com`) so the other subdomain apps
can read it and pre-fill their contact fields. The customer fills it once and it
follows them across products.

- read(request)         -> {first_name, last_name, email, phone} from the cookie
- persist(request, resp) -> if this request submitted any of those fields, write
                            (merged with what's already there) back to the cookie

The cookie is httponly (prefill happens server-side), Lax (sent on the top-level
navigation between subdomains), and Secure when the request is HTTPS. The domain
is only set when the host actually lives under `.dlrpro.com`, so local
http://localhost testing still works (host-only cookie).
"""

import json
import os
from urllib.parse import quote, unquote

COOKIE_NAME = "dlrpro_pii"
COOKIE_DOMAIN = os.environ.get("PII_COOKIE_DOMAIN", ".dlrpro.com")
FIELDS = ("first_name", "last_name", "email", "phone")
MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def read(request):
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return {}
    try:
        data = json.loads(unquote(raw))
    except Exception:
        return {}
    return {k: data[k] for k in FIELDS if data.get(k)}


def persist(request, response):
    """If the request carried any contact field (form post or handoff query),
    merge it into the shared cookie."""
    src = {}
    try:
        src.update(request.form.to_dict())
    except Exception:
        pass
    try:
        src.update(request.args.to_dict())
    except Exception:
        pass
    pii = {k: (src.get(k) or "").strip() for k in FIELDS if (src.get(k) or "").strip()}
    if not pii:
        return response

    merged = {**read(request), **pii}
    host = (request.host or "").split(":")[0]
    bare = COOKIE_DOMAIN.lstrip(".")
    domain = COOKIE_DOMAIN if (host == bare or host.endswith("." + bare)) else None
    response.set_cookie(
        COOKIE_NAME, quote(json.dumps(merged)),
        max_age=MAX_AGE, domain=domain, path="/",
        secure=request.is_secure, httponly=True, samesite="Lax",
    )
    return response
