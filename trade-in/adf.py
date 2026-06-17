"""Build ADF/XML for a trade-in lead.

The customer's vehicle is the trade-in (vehicle interest="trade-in"). Condition
answers and the computed appraisal value go in <comments> (ADF has no standard
elements for them). Sent twice: an initial version after the contact step, and
an updated version (with condition + value) after the final step.
"""

from datetime import datetime, timezone
from xml.sax.saxutils import escape

_CONDITION_LABELS = [
    ("num_keys", "Number of keys"),
    ("unrepaired_damage", "Un-repaired damage"),
    ("engine_light", "Engine light on"),
    ("airbag_light", "Airbag light on"),
    ("brake_light", "Brake light on"),
    ("aftermarket_exhaust", "Aftermarket exhaust"),
    ("aftermarket_engine", "Aftermarket engine components"),
    ("aftermarket_stereo", "Aftermarket stereo/electronics"),
    ("own_or_lease", "Own or lease"),
    ("ownership_status", "Loan/lease/title status"),
    ("loan_balance", "Outstanding loan balance"),
    ("lease_months_remaining", "Months remaining on lease"),
]


def _el(tag, value, attrs=None, indent=0):
    if value is None or str(value).strip() == "":
        return ""
    attr_str = ""
    if attrs:
        attr_str = "".join(f' {k}="{escape(str(v))}"' for k, v in attrs.items() if v)
    pad = " " * indent
    return f"{pad}<{tag}{attr_str}>{escape(str(value))}</{tag}>\n"


def _comments(lead, valuation):
    lines = ["Trade-in inquiry."]
    if lead.get("serial"):
        lines.append(f"Offer #: {lead.get('serial')}")
    if lead.get("offer_url"):
        lines.append(f"View this offer: {lead.get('offer_url')}")
    has_condition = any(lead.get(k) for k, _ in _CONDITION_LABELS)
    if has_condition:
        lines.append("")
        lines.append("Vehicle condition:")
        for key, label in _CONDITION_LABELS:
            val = lead.get(key)
            if val is None or str(val).strip() == "":
                continue
            lines.append(f"  - {label}: {val}")
    if valuation and valuation.get("ok"):
        lines.append("")
        lines.append("Estimated trade-in value:")
        lines.append(f"  - Range: ${valuation['range_low']:,} - ${valuation['range_high']:,}")
        lines.append(f"  - Point estimate: ${valuation['final_value']:,}")
        lines.append(f"  - Basis: {valuation.get('base_source')} "
                     f"({valuation.get('comp_count')} comps, "
                     f"mileage via {valuation.get('mileage_method')})")
        if valuation.get("adjustments"):
            adjs = ", ".join(f"{a['label']} ${a['amount']:,}" for a in valuation["adjustments"])
            lines.append(f"  - Condition adjustments: {adjs}")
    return "\n".join(lines)


def build_adf(lead, dealer, valuation=None, request_dt=None):
    if request_dt is None:
        request_dt = datetime.now(timezone.utc)
    requestdate = request_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    if requestdate and requestdate[-5] in "+-":
        requestdate = requestdate[:-2] + ":" + requestdate[-2:]

    p = []
    p.append('<?ADF version="1.0"?>\n')
    p.append('<?xml version="1.0" encoding="UTF-8"?>\n')
    p.append("<adf>\n")
    p.append("  <prospect>\n")
    p.append(_el("requestdate", requestdate, indent=4))
    p.append(_el("id", lead.get("serial"), {"sequence": "1", "source": "TradeInOffer"}, indent=4))

    # The trade-in vehicle.
    p.append('    <vehicle interest="trade-in" status="used">\n')
    p.append(_el("year", lead.get("vehicle_year"), indent=6))
    p.append(_el("make", lead.get("vehicle_make"), indent=6))
    p.append(_el("model", lead.get("vehicle_model"), indent=6))
    p.append(_el("trim", lead.get("vehicle_trim"), indent=6))
    if lead.get("miles"):
        p.append(_el("odometer", str(lead.get("miles")).replace(",", ""),
                     {"status": "original", "units": "miles"}, indent=6))
    p.append("    </vehicle>\n")

    # Customer.
    p.append("    <customer>\n")
    p.append("      <contact>\n")
    p.append(_el("name", lead.get("first_name"), {"part": "first"}, indent=8))
    p.append(_el("name", lead.get("last_name"), {"part": "last"}, indent=8))
    p.append(_el("email", lead.get("email"), indent=8))
    p.append(_el("phone", lead.get("phone"), {"type": "voice"}, indent=8))
    p.append("      </contact>\n")
    p.append(_el("comments", _comments(lead, valuation), indent=6))
    p.append("    </customer>\n")

    # Vendor = the dealership.
    p.append("    <vendor>\n")
    p.append(_el("vendorname", dealer.get("dealer_name"), indent=6))
    p.append("      <contact>\n")
    p.append(_el("name", dealer.get("dealer_name"), {"part": "full"}, indent=8))
    if any(dealer.get(k) for k in ("address", "city", "state", "zip")):
        p.append('        <address type="business">\n')
        p.append(_el("street", dealer.get("address"), {"line": "1"}, indent=10))
        p.append(_el("city", dealer.get("city"), indent=10))
        p.append(_el("regioncode", dealer.get("state"), indent=10))
        p.append(_el("postalcode", dealer.get("zip"), indent=10))
        p.append(_el("country", "US", indent=10))
        p.append("        </address>\n")
    p.append(_el("phone", dealer.get("phone"), {"type": "voice"}, indent=8))
    p.append(_el("email", dealer.get("lead_email_address"), indent=8))
    p.append("      </contact>\n")
    p.append(_el("id", dealer.get("dealer_id"), {"source": "DealerID"}, indent=6))
    p.append("    </vendor>\n")

    p.append("    <provider>\n")
    p.append(_el("name", "Trade-In Widget", {"part": "full"}, indent=6))
    p.append("    </provider>\n")

    p.append("  </prospect>\n")
    p.append("</adf>\n")
    return "".join(p)
