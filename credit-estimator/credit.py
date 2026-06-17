"""Credit Estimator engine.

Two distinct pieces:

1. A self-reported, FICO-style score-RANGE estimator. The consumer answers a
   short set of questions modeled on the highest-impact FICO factors (payment
   history, credit utilization, length of history, derogatory marks). We turn
   those answers into an estimated score and report the FICO category it falls
   in. This is an estimate for informational purposes only — it is NOT a credit
   pull and NOT a guarantee of approval.

2. The money math the dealer cares about: mapping that estimated tier to an APR
   (from the dealer's configurable rate sheet), computing a monthly payment for
   the deal the consumer described, an approval-likelihood read, and the maximum
   vehicle price they could likely afford given their income.

The scoring weights here are fixed (they model FICO's published factor weights);
the APR rates and affordability limits are the dealer-tunable settings that come
from platform_db.get_credit_settings().
"""

# ----- 1. FICO-style range estimator -----
#
# FICO's published factor weights: payment history 35%, amounts owed
# (utilization) 30%, length of history 15%, new credit 10%, credit mix 10%.
# We collapse "new credit" + "credit mix" into the derogatory/inquiries question.
# Each question contributes points on a 300-850 scale: a 300 base plus up to
# ~550 spread across the factors in proportion to those weights.

BASE_SCORE = 300

# question_key -> { answer_value -> points }
SCORE_POINTS = {
    # 35% of 550 ≈ 192
    "payment_history": {
        "never": 192,        # never missed a payment
        "1_2_late": 120,     # 1-2 late payments in the last 2 years
        "3plus_late": 55,    # 3 or more late payments
        "collections": 0,    # currently behind / in collections
    },
    # 30% of 550 ≈ 165
    "utilization": {
        "under_10": 165,     # using under 10% of available credit
        "10_30": 135,        # 10-30%
        "30_50": 85,         # 30-50%
        "over_50": 35,       # over 50%
        "no_cards": 95,      # no credit cards (thin file — neutral-ish)
    },
    # 15% of 550 ≈ 82
    "credit_age": {
        "gt_10": 82,         # 10+ years
        "5_10": 60,          # 5-10 years
        "2_5": 36,           # 2-5 years
        "lt_2": 14,          # less than 2 years
    },
    # 20% of 550 ≈ 110 (new credit + credit mix)
    "derogatory": {
        "none": 110,         # no collections, bankruptcies, or repossessions
        "one_collection": 45,
        "major": 5,          # bankruptcy / repossession / foreclosure
    },
}

# Standard FICO score bands: (low, high, label, settings_key).
FICO_BANDS = [
    (800, 850, "Exceptional", "apr_exceptional"),
    (740, 799, "Very Good", "apr_very_good"),
    (670, 739, "Good", "apr_good"),
    (580, 669, "Fair", "apr_fair"),
    (300, 579, "Poor", "apr_poor"),
]

APPROVAL = {
    "Exceptional": "Very likely — you should qualify for the best available terms.",
    "Very Good": "Very likely — you should qualify for competitive terms.",
    "Good": "Likely — most lenders should be able to work with you.",
    "Fair": "Possible — approval and rate will vary by lender.",
    "Poor": "Challenging — a co-signer or larger down payment may help.",
}

# The questionnaire, in display order. Each: (key, prompt, [(value, label), ...]).
QUESTIONS = [
    ("payment_history",
     "In the last 2 years, have you missed any loan or credit-card payments?",
     [("never", "Never missed one"),
      ("1_2_late", "1–2 late payments"),
      ("3plus_late", "3 or more late"),
      ("collections", "Currently behind / in collections")]),
    ("utilization",
     "About how much of your available credit are you using right now?",
     [("under_10", "Under 10%"),
      ("10_30", "10–30%"),
      ("30_50", "30–50%"),
      ("over_50", "Over 50%"),
      ("no_cards", "I don't have credit cards")]),
    ("credit_age",
     "How long have you had credit?",
     [("lt_2", "Less than 2 years"),
      ("2_5", "2–5 years"),
      ("5_10", "5–10 years"),
      ("gt_10", "10+ years")]),
    ("derogatory",
     "In the last 7 years, have you had any of these?",
     [("none", "None of these"),
      ("one_collection", "A collection or charge-off"),
      ("major", "Bankruptcy, repossession, or foreclosure")]),
]

QUESTION_KEYS = [q[0] for q in QUESTIONS]


def estimate_score(answers):
    """Sum the points for the given answers and clamp to the 300-850 scale.

    answers: dict of question_key -> answer_value. Unknown/missing answers
    contribute 0 for that factor (treated as worst-case, so a half-finished
    quiz can't inflate the score).
    """
    score = BASE_SCORE
    for key, options in SCORE_POINTS.items():
        score += options.get(answers.get(key), 0)
    return max(300, min(850, int(round(score))))


def band_for_score(score):
    """Return (low, high, label, apr_key) for the band the score falls in."""
    for low, high, label, apr_key in FICO_BANDS:
        if low <= score <= high:
            return low, high, label, apr_key
    return FICO_BANDS[-1]  # defensive: lowest band


# ----- 2. APR, payment, affordability -----

def tier_apr(apr_key, settings, vehicle_new=False):
    """Base APR for the tier, with the new-vehicle delta applied if relevant."""
    apr = float(settings.get(apr_key, 0) or 0)
    if vehicle_new:
        apr += float(settings.get("new_apr_delta", 0) or 0)
    return max(0.0, round(apr, 2))


def monthly_payment(principal, annual_rate_pct, term_months):
    """Standard amortized monthly payment. Handles 0% safely."""
    principal = max(0.0, float(principal))
    n = int(term_months)
    if principal <= 0 or n <= 0:
        return 0.0
    r = float(annual_rate_pct) / 100.0 / 12.0
    if r == 0:
        return principal / n
    factor = (1 + r) ** n
    return principal * r * factor / (factor - 1)


def max_principal(payment, annual_rate_pct, term_months):
    """Inverse of monthly_payment: the largest loan a given payment supports."""
    payment = max(0.0, float(payment))
    n = int(term_months)
    if payment <= 0 or n <= 0:
        return 0.0
    r = float(annual_rate_pct) / 100.0 / 12.0
    if r == 0:
        return payment * n
    factor = (1 + r) ** n
    return payment * (factor - 1) / (r * factor)


def _to_float(v, default=0.0):
    try:
        s = str(v).replace(",", "").replace("$", "").strip()
        return float(s) if s != "" else default
    except (TypeError, ValueError):
        return default


def compute(answers, deal, settings):
    """Run the full estimate.

    answers: question_key -> answer_value (the FICO quiz).
    deal:    dict with vehicle_price, down_payment, trade_value, term_months,
             annual_income, vehicle_condition ('new'|'used').
    settings: dealer credit settings (platform_db.get_credit_settings()).

    Returns a result dict consumed by the results template and the ADF builder.
    """
    score = estimate_score(answers)
    low, high, label, apr_key = band_for_score(score)

    vehicle_new = (deal.get("vehicle_condition") or "used").lower() == "new"
    apr = tier_apr(apr_key, settings, vehicle_new)
    spread = float(settings.get("apr_spread", 1.0) or 0)
    apr_low = max(0.0, round(apr - spread, 2))
    apr_high = round(apr + spread, 2)

    monthly = _to_float(deal.get("monthly_payment"))
    down = _to_float(deal.get("down_payment"))
    trade = _to_float(deal.get("trade_value"))
    term = int(deal.get("term_months") or settings.get("max_term_months") or 72)
    income_annual = _to_float(deal.get("annual_income"))

    # The consumer tells us the monthly payment they have in mind; we back out
    # the loan that payment supports at the tier APR, then the vehicle price
    # (loan + down + trade). A higher APR buys less car, so the APR spread maps
    # to the price range inversely.
    if monthly > 0:
        financed = max_principal(monthly, apr, term)
        vehicle_price = financed + down + trade
        vehicle_low = max_principal(monthly, apr_high, term) + down + trade
        vehicle_high = max_principal(monthly, apr_low, term) + down + trade
    else:
        financed = vehicle_price = vehicle_low = vehicle_high = 0.0

    # Affordability: cap the payment at a share of gross monthly income, then
    # back out the largest loan + max vehicle price (loan + down + trade).
    affordability = None
    if income_annual > 0:
        income_monthly = income_annual / 12.0
        max_pay = income_monthly * float(settings.get("max_payment_pct", 15.0) or 0) / 100.0
        max_term = int(settings.get("max_term_months", 72) or 72)
        loan_cap = max_principal(max_pay, apr, max_term)
        affordability = {
            "max_monthly_payment": round(max_pay),
            "max_loan": round(loan_cap),
            "max_vehicle_price": round(loan_cap + down + trade),
            "term_months": max_term,
        }

    return {
        "ok": True,
        "score": score,
        "range_low": low,
        "range_high": high,
        "tier": label,
        "approval": APPROVAL.get(label, ""),
        "apr": apr,
        "apr_low": apr_low,
        "apr_high": apr_high,
        "vehicle_condition": "new" if vehicle_new else "used",
        "monthly_payment": round(monthly) if monthly > 0 else None,
        "vehicle_price": round(vehicle_price) if monthly > 0 else None,
        "vehicle_price_low": round(vehicle_low) if monthly > 0 else None,
        "vehicle_price_high": round(vehicle_high) if monthly > 0 else None,
        "down_payment": round(down),
        "trade_value": round(trade),
        "amount_financed": round(financed),
        "term_months": term,
        "affordability": affordability,
    }
