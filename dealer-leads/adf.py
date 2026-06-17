"""Build ADF/XML (Auto-lead Data Format) payloads from a lead + dealer.

ADF 1.0 reference structure: a <prospect> containing the request date, the
vehicle of interest, the customer contact, the vendor (the dealer), and the
provider (this product).
"""

from datetime import datetime, timezone
from xml.sax.saxutils import escape


def _el(tag, value, attrs=None, indent=0):
    """Render a single element, skipping it entirely when value is empty."""
    if value is None or str(value).strip() == "":
        return ""
    attr_str = ""
    if attrs:
        attr_str = "".join(
            f' {k}="{escape(str(v))}"' for k, v in attrs.items() if v
        )
    pad = " " * indent
    return f"{pad}<{tag}{attr_str}>{escape(str(value))}</{tag}>\n"


def build_adf(lead, dealer, request_dt=None):
    """Return an ADF/XML string for the given lead and dealer dicts."""
    if request_dt is None:
        request_dt = datetime.now(timezone.utc)
    requestdate = request_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    # Insert the colon in the timezone offset (+0000 -> +00:00) for ISO 8601.
    if requestdate and requestdate[-5] in "+-":
        requestdate = requestdate[:-2] + ":" + requestdate[-2:]

    parts = []
    parts.append('<?ADF version="1.0"?>\n')
    parts.append('<?xml version="1.0" encoding="UTF-8"?>\n')
    parts.append("<adf>\n")
    parts.append("  <prospect>\n")
    parts.append(_el("requestdate", requestdate, indent=4))

    # Vehicle of interest (optional).
    if any(lead.get(k) for k in ("vehicle_year", "vehicle_make", "vehicle_model")):
        parts.append('    <vehicle interest="buy" status="new">\n')
        parts.append(_el("year", lead.get("vehicle_year"), indent=6))
        parts.append(_el("make", lead.get("vehicle_make"), indent=6))
        parts.append(_el("model", lead.get("vehicle_model"), indent=6))
        parts.append("    </vehicle>\n")

    # Customer.
    parts.append("    <customer>\n")
    parts.append("      <contact>\n")
    parts.append(_el("name", lead.get("first_name"), {"part": "first"}, indent=8))
    parts.append(_el("name", lead.get("last_name"), {"part": "last"}, indent=8))
    parts.append(_el("email", lead.get("email"), indent=8))
    parts.append(_el("phone", lead.get("phone"), {"type": "voice"}, indent=8))
    parts.append("      </contact>\n")
    parts.append(_el("comments", lead.get("comments"), indent=6))
    parts.append("    </customer>\n")

    # Vendor = the dealership receiving the lead.
    parts.append("    <vendor>\n")
    parts.append(_el("vendorname", dealer.get("dealer_name"), indent=6))
    parts.append("      <contact>\n")
    parts.append(_el("name", dealer.get("dealer_name"), {"part": "full"}, indent=8))
    has_addr = any(
        dealer.get(k) for k in ("address", "city", "state", "zip")
    )
    if has_addr:
        parts.append('        <address type="business">\n')
        parts.append(_el("street", dealer.get("address"), {"line": "1"}, indent=10))
        parts.append(_el("city", dealer.get("city"), indent=10))
        parts.append(_el("regioncode", dealer.get("state"), indent=10))
        parts.append(_el("postalcode", dealer.get("zip"), indent=10))
        parts.append(_el("country", "US", indent=10))
        parts.append("        </address>\n")
    parts.append(_el("phone", dealer.get("phone"), {"type": "voice"}, indent=8))
    parts.append(_el("email", dealer.get("lead_email_address"), indent=8))
    parts.append("      </contact>\n")
    parts.append(_el("id", dealer.get("dealer_id"), {"source": "DealerID"}, indent=6))
    parts.append("    </vendor>\n")

    # Provider = this lead-form product.
    parts.append("    <provider>\n")
    parts.append(_el("name", "Dealer Lead Form", {"part": "full"}, indent=6))
    parts.append("    </provider>\n")

    parts.append("  </prospect>\n")
    parts.append("</adf>\n")
    return "".join(parts)
