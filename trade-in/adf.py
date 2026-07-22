"""Build ADF/XML for a trade-in lead.

The customer's vehicle is the trade-in (vehicle interest="trade-in"). Condition
answers and the computed appraisal value go in <comments> (ADF has no standard
elements for them). Sent twice: an initial version after the contact step, and
an updated version (with condition + value) after the final step.
"""

from datetime import datetime, timezone
from xml.sax.saxutils import escape

try:                       # platform/ is on sys.path in the app process
    import platform_db as _pdb
except Exception:          # keep ADF generation working even if the DB is down
    _pdb = None

DEFAULT_PRODUCT_NAME = "Trade-In Widget"
DEFAULT_SOURCE = "RMA Data Plus"

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


def _product_meta(product_code):
    """(source, product_name) for this product — the ADF source lineage. seq=1
    source is the product's configured `source` (e.g. 'RMA Data Plus'); seq=2
    source is the product name. Falls back to defaults if the lookup fails."""
    source, name = DEFAULT_SOURCE, DEFAULT_PRODUCT_NAME
    if _pdb and product_code:
        try:
            p = _pdb.get_product(product_code)
            if p:
                source = p.get("source") or source
                name = p.get("product_name") or name
        except Exception:
            pass
    return source, name


def _id_lineage(id_value, source, product_name, indent=4):
    """Two <id> elements tracking this prospect's source history:
    sequence=1 = the primary source, sequence=2 = this product."""
    return (_el("id", id_value, {"sequence": "1", "source": source}, indent=indent)
            + _el("id", id_value, {"sequence": "2", "source": product_name}, indent=indent))


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
        # Show how the offer was derived from market when the dealer has set a
        # margin: (market x %) - flat deduction, before condition adjustments.
        pct = valuation.get("market_pct")
        flat = valuation.get("flat_deduction") or 0
        if valuation.get("market_value") is not None and ((pct is not None and pct != 100) or flat):
            bits = [f"market ${valuation['market_value']:,}"]
            if pct is not None and pct != 100:
                bits.append(f"x {pct:g}% = ${valuation.get('pct_value', 0):,}")
            if flat:
                bits.append(f"less ${flat:,} dealer cost")
            lines.append(f"  - Offer basis: {' '.join(bits)} "
                         f"=> ${valuation.get('base_value', 0):,}")
        if valuation.get("adjustments"):
            adjs = ", ".join(f"{a['label']} ${a['amount']:,}" for a in valuation["adjustments"])
            lines.append(f"  - Condition adjustments: {adjs}")
    return "\n".join(lines)


def build_adf(lead, dealer, valuation=None, request_dt=None, product_code=None):
    if request_dt is None:
        request_dt = datetime.now(timezone.utc)
    requestdate = request_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    if requestdate and requestdate[-5] in "+-":
        requestdate = requestdate[:-2] + ":" + requestdate[-2:]

    _, product_name = _product_meta(product_code)
    # Primary (sequence 1) ADF source = the dealer's lead source (default
    # "Credit Pipeline"); sequence 2 = the product name.
    lead_source = (_pdb.lead_source_for(dealer) if _pdb else None) or "Credit Pipeline"
    id_value = lead.get("serial") or lead.get("id") or lead.get("lead_id")

    p = []
    # Tecobi expects the XML declaration first (the XML-valid order); everyone
    # else gets the legacy ADF-first order.
    crm = (_pdb.crm_name_for(dealer) if _pdb else None) or ""
    is_tecobi = crm.strip().lower() == "tecobi"
    if is_tecobi:
        p.append('<?xml version="1.0" encoding="UTF-8"?>\n')
        p.append('<?ADF version="1.0"?>\n')
    else:
        p.append('<?ADF version="1.0"?>\n')
        p.append('<?xml version="1.0" encoding="UTF-8"?>\n')
    p.append("<adf>\n")
    p.append("  <prospect>\n")
    p.append(_id_lineage(id_value, lead_source, product_name, indent=4))
    p.append(_el("requestdate", requestdate, indent=4))

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
    p.append(_el("name", lead_source if is_tecobi else product_name,
                 {"part": "full"}, indent=6))
    p.append("    </provider>\n")

    p.append("  </prospect>\n")
    p.append("</adf>\n")
    return "".join(p)
