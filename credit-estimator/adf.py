"""Build ADF/XML (Auto-lead Data Format) payloads for the Credit Estimator.

Same prospect structure as the lead form, plus an optional <finance> block and a
credit-estimate summary in the customer comments once the consumer completes the
estimate on page 2 (the second, updated email).
"""

from datetime import datetime, timezone
from xml.sax.saxutils import escape


def _el(tag, value, attrs=None, indent=0):
    if value is None or str(value).strip() == "":
        return ""
    attr_str = ""
    if attrs:
        attr_str = "".join(
            f' {k}="{escape(str(v))}"' for k, v in attrs.items() if v
        )
    pad = " " * indent
    return f"{pad}<{tag}{attr_str}>{escape(str(value))}</{tag}>\n"


def _estimate_summary(est):
    """Human-readable credit summary appended to the customer comments."""
    lines = ["--- Credit Estimate (self-reported, informational only) ---"]
    lines.append(f"Estimated credit score range: {est['range_low']}-{est['range_high']} "
                 f"({est['tier']})")
    lines.append(f"Approval read: {est['approval']}")
    lines.append(f"Estimated APR: {est['apr']}% "
                 f"({est['apr_low']}%-{est['apr_high']}%, {est['vehicle_condition']})")
    if est.get("monthly_payment") and est.get("vehicle_price"):
        lines.append(
            f"Target payment: ${est['monthly_payment']:,}/mo over {est['term_months']} mo "
            f"(${est['down_payment']:,} down, ${est['trade_value']:,} trade)")
        lines.append(
            f"Estimated vehicle price at this payment: ${est['vehicle_price']:,} "
            f"(${est['amount_financed']:,} financed)")
    aff = est.get("affordability")
    if aff:
        lines.append(f"Est. max vehicle price by income (at {est['term_months']} mo): "
                     f"${aff['max_vehicle_price']:,}")
    return "\n".join(lines)


def build_adf(lead, dealer, estimate=None, request_dt=None):
    """Return an ADF/XML string. If estimate is given, include the finance
    block + the credit summary in the comments."""
    if request_dt is None:
        request_dt = datetime.now(timezone.utc)
    requestdate = request_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
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
        status = "new" if (estimate and estimate.get("vehicle_condition") == "new") else "used"
        parts.append(f'    <vehicle interest="buy" status="{status}">\n')
        parts.append(_el("year", lead.get("vehicle_year"), indent=6))
        parts.append(_el("make", lead.get("vehicle_make"), indent=6))
        parts.append(_el("model", lead.get("vehicle_model"), indent=6))
        parts.append("    </vehicle>\n")

    # Customer.
    comments = lead.get("comments") or ""
    if estimate and estimate.get("ok"):
        summary = _estimate_summary(estimate)
        comments = (comments + "\n\n" + summary).strip() if comments else summary
    parts.append("    <customer>\n")
    parts.append("      <contact>\n")
    parts.append(_el("name", lead.get("first_name"), {"part": "first"}, indent=8))
    parts.append(_el("name", lead.get("last_name"), {"part": "last"}, indent=8))
    parts.append(_el("email", lead.get("email"), indent=8))
    parts.append(_el("phone", lead.get("phone"), {"type": "voice"}, indent=8))
    parts.append("      </contact>\n")
    parts.append(_el("comments", comments, indent=6))
    parts.append("    </customer>\n")

    # Finance block (ADF supports <finance> under prospect).
    if estimate and estimate.get("ok"):
        parts.append("    <finance>\n")
        parts.append(_el("method", "finance", indent=6))
        if estimate.get("down_payment"):
            parts.append(_el("amount", estimate["down_payment"],
                             {"type": "downpayment"}, indent=6))
        if estimate.get("monthly_payment"):
            parts.append(_el("amount", estimate["monthly_payment"],
                             {"type": "monthly"}, indent=6))
        if estimate.get("term_months"):
            parts.append(_el("balance", estimate.get("amount_financed"),
                             {"type": "finance"}, indent=6))
        parts.append("    </finance>\n")

    # Vendor = the dealership receiving the lead.
    parts.append("    <vendor>\n")
    parts.append(_el("vendorname", dealer.get("dealer_name"), indent=6))
    parts.append("      <contact>\n")
    parts.append(_el("name", dealer.get("dealer_name"), {"part": "full"}, indent=8))
    if any(dealer.get(k) for k in ("address", "city", "state", "zip")):
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

    # Provider = this product.
    parts.append("    <provider>\n")
    parts.append(_el("name", "Credit Estimator", {"part": "full"}, indent=6))
    parts.append("    </provider>\n")

    parts.append("  </prospect>\n")
    parts.append("</adf>\n")
    return "".join(parts)
