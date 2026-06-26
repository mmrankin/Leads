"""Credit Estimator — Flask application.

Two-page consumer flow per dealer:
    GET/POST /c/<dealer_id>            page 1 — contact + vehicle (the lead form).
                                       Creates the lead and sends ADF #1.
    GET/POST /c/<dealer_id>/estimate   page 2 — short FICO-style quiz + deal inputs.
                                       Computes the estimate, updates the lead,
                                       sends the updated ADF #2.
    GET      /c/<dealer_id>/results    Shows the estimated FICO range, APR,
                                       monthly payment, approval read, and the
                                       max-affordable figure.

Dealers, the CREDIT_EST product grant, and the APR/affordability settings all
come from the shared platform DB; the form is unavailable without an active
grant. Leads are stored locally.
"""

import os
import sys
from datetime import datetime
from urllib.parse import urlencode

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# Make the shared platform package importable.
PLATFORM_DIR = os.environ.get(
    "PLATFORM_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "platform"),
)
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from flask import (
    Flask, abort, jsonify, redirect, render_template, request, session, url_for
)

import platform_db as pdb
import contact_cookie
import db
import credit
import vin_db
from adf import build_adf
from email_send import send_adf
from email_validate import validate_email

PRODUCT_CODE = pdb.PRODUCT_CREDIT_EST

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")

# Behind the Cloudflare Tunnel / reverse proxy: trust one hop of forwarded
# headers so url_for(_external=True) yields https://<public-host>.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


@app.after_request
def _share_pii(response):
    return contact_cookie.persist(request, response)


@app.before_request
def _ensure_db():
    if not getattr(app, "_db_ready", False):
        pdb.init_db()
        db.init_db()
        app._db_ready = True


def _require_active_dealer(dealer_id):
    dealer = pdb.get_dealer(dealer_id)
    if not dealer:
        abort(404, description="Unknown dealer.")
    if not pdb.dealer_has_product(dealer_id, PRODUCT_CODE):
        abort(403)
    return dealer


def _banner_override():
    """Optional per-link banner image via ?IMG=<url>; http(s) only."""
    img = (request.values.get("IMG") or request.values.get("img") or "").strip()
    low = img.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return img
    return None


def _session_key(dealer_id):
    return f"credit:{dealer_id}"


@app.errorhandler(403)
def _inactive(e):
    dealer_id = request.view_args.get("dealer_id") if request.view_args else None
    dealer = pdb.get_dealer(dealer_id) if dealer_id else None
    return render_template("inactive.html", dealer=dealer), 403


# ----- page 1: contact + vehicle (creates lead + sends ADF #1) -----

@app.route("/c/<dealer_id>", methods=["GET", "POST"])
def lead_form(dealer_id):
    dealer = _require_active_dealer(dealer_id)

    if request.method == "GET":
        # Pre-fill name/email/phone from the shared cross-product cookie.
        return render_template(
            "lead_form.html", dealer=dealer, errors={},
            form=contact_cookie.read(request),
            banner_override=_banner_override(), step=1,
        )

    form = {k: (request.form.get(k) or "").strip() for k in (
        "first_name", "last_name", "email", "phone",
    )}
    tc = request.form.get("tc_agree") == "on"

    errors = {}
    if not form["email"] and not form["phone"]:
        errors["contact"] = "Please provide an email address or a phone number."
    if form["email"] and "@" not in form["email"]:
        errors["email"] = "Please enter a valid email address."
    if not tc:
        errors["tc"] = "Please agree to the terms and conditions."

    validation = {"verdict": None, "score": None}
    if not errors and form["email"]:
        validation = validate_email(form["email"])
        if not validation["accept"]:
            errors["email"] = (
                "That email address doesn't appear to be deliverable. "
                "Please check it, or leave it blank and provide a phone number."
            )

    if errors:
        return render_template(
            "lead_form.html", dealer=dealer, errors=errors, form=form,
            banner_override=_banner_override(), step=1,
        ), 400

    # Persist the lead and send the initial ADF (no estimate yet).
    lead = dict(form)
    lead["dealer_id"] = dealer_id
    lead["source"] = request.headers.get("Referer") or request.url
    lead["tc_agreed"] = 1
    lead["tc_agreed_at"] = datetime.utcnow().isoformat()
    lead["email_verdict"] = validation.get("verdict")
    lead["email_score"] = validation.get("score")
    adf_xml = build_adf(lead, dealer, estimate=None)
    lead["adf_xml"] = adf_xml
    status, detail = send_adf(dealer, adf_xml, lead_id="new", lead=lead)
    lead["email1_status"], lead["email1_detail"] = status, detail
    lead_id = db.insert_lead(lead)
    db.set_email_status(lead_id, 1, status, detail)

    # Carry the contact/vehicle context into page 2 via the session.
    session[_session_key(dealer_id)] = {
        "first_name": form["first_name"], "last_name": form["last_name"],
        "email": form["email"], "phone": form["phone"], "lead_id": lead_id,
    }
    return redirect(url_for("estimate", dealer_id=dealer_id))


# ----- page 2: credit quiz (answers stored in session) -----

DEAL_FIELDS = ("vehicle_condition", "monthly_payment", "down_payment",
               "trade_value", "term_months", "annual_income")


@app.route("/c/<dealer_id>/estimate", methods=["GET", "POST"])
def estimate(dealer_id):
    dealer = _require_active_dealer(dealer_id)
    data = session.get(_session_key(dealer_id), {})
    if not data.get("lead_id"):
        return redirect(url_for("lead_form", dealer_id=dealer_id))

    if request.method == "GET":
        return render_template(
            "estimate.html", dealer=dealer, form=data, errors={},
            questions=credit.QUESTIONS, step=2,
        )

    answers = {k: (request.form.get(k) or "").strip() for k in credit.QUESTION_KEYS}
    errors = {key: "Please choose an answer." for key in credit.QUESTION_KEYS
              if not answers[key]}
    if errors:
        return render_template(
            "estimate.html", dealer=dealer, form={**data, **answers},
            errors=errors, questions=credit.QUESTIONS, step=2,
        ), 400

    # Stash the answers and move on to the deal page (the estimate is computed
    # there, once we know the deal terms).
    data.update(answers)
    session[_session_key(dealer_id)] = data
    return redirect(url_for("deal", dealer_id=dealer_id))


# ----- page 3: the deal (computes estimate, sends ADF #2) -----

@app.route("/c/<dealer_id>/deal", methods=["GET", "POST"])
def deal(dealer_id):
    dealer = _require_active_dealer(dealer_id)
    data = session.get(_session_key(dealer_id), {})
    if not data.get("lead_id"):
        return redirect(url_for("lead_form", dealer_id=dealer_id))
    # Require the quiz to have been answered first.
    if not all(data.get(k) for k in credit.QUESTION_KEYS):
        return redirect(url_for("estimate", dealer_id=dealer_id))
    settings = pdb.get_credit_settings(dealer_id)

    if request.method == "GET":
        return render_template("deal.html", dealer=dealer, form=data, step=3)

    deal_input = {k: (request.form.get(k) or "").strip() for k in DEAL_FIELDS}
    answers = {k: data.get(k) for k in credit.QUESTION_KEYS}
    result = credit.compute(answers, deal_input, settings)

    # Update the lead with answers, deal, and the computed estimate; refresh ADF.
    lead = {**data, **deal_input, "dealer_id": dealer_id}
    full = build_adf(lead, dealer, estimate=result)
    aff = result.get("affordability") or {}
    db.update_estimate(data["lead_id"], {
        **answers, **deal_input,
        "term_months": result["term_months"],
        "est_score": result["score"], "range_low": result["range_low"],
        "range_high": result["range_high"], "tier": result["tier"],
        "apr": result["apr"], "apr_low": result["apr_low"],
        "apr_high": result["apr_high"], "amount_financed": result["amount_financed"],
        "monthly_payment": result["monthly_payment"],
        "vehicle_price": result["vehicle_price"],
        "max_vehicle_price": aff.get("max_vehicle_price"),
        "approval": result["approval"], "adf_xml": full,
    })
    status, detail = send_adf(dealer, full, lead_id=data["lead_id"], lead=lead)
    db.set_email_status(data["lead_id"], 2, status, detail)

    session[_session_key(dealer_id)]["estimate"] = result
    session.modified = True
    return redirect(url_for("results", dealer_id=dealer_id))


# ----- trade-in handoff (send ADF, then jump into the trade-in app) -----

TRADE_IN_BASE_URL = os.environ.get("TRADE_IN_BASE_URL", "http://localhost:5001")


@app.route("/c/<dealer_id>/trade-in")
def trade_in_handoff(dealer_id):
    """Send a trade-in-interest ADF to the dealer, then redirect into the
    trade-in app with the contact details we already collected so it can skip
    its name/email/phone step."""
    dealer = _require_active_dealer(dealer_id)
    data = session.get(_session_key(dealer_id), {})
    if not data.get("lead_id"):
        return redirect(url_for("lead_form", dealer_id=dealer_id))

    # Notify the dealer now (the trade-in app will follow up with its own ADFs).
    lead = {
        "dealer_id": dealer_id,
        "first_name": data.get("first_name"), "last_name": data.get("last_name"),
        "email": data.get("email"), "phone": data.get("phone"),
        "comments": "Customer requested a trade-in appraisal (from the Credit Estimator).",
    }
    adf_xml = build_adf(lead, dealer, estimate=None)
    send_adf(dealer, adf_xml, lead_id=data["lead_id"], lead=lead)

    # Hand the contact off to the trade-in app. These are first-party apps on the
    # same host; the contact rides in the query string so the trade-in widget can
    # pre-fill it and bypass its contact step (ho=1 = "contact already collected").
    params = urlencode({
        "ho": "1",
        "fn": data.get("first_name") or "",
        "ln": data.get("last_name") or "",
        "email": data.get("email") or "",
        "phone": data.get("phone") or "",
    })
    return redirect(f"{TRADE_IN_BASE_URL.rstrip('/')}/t/{dealer_id}?{params}")


# ----- page 4: results -----

@app.route("/c/<dealer_id>/results")
def results(dealer_id):
    dealer = _require_active_dealer(dealer_id)
    data = session.get(_session_key(dealer_id), {})
    result = data.get("estimate")
    if not result:
        return redirect(url_for("lead_form", dealer_id=dealer_id))
    return render_template("results.html", dealer=dealer, est=result, step=4)


# ----- test lead (a copy of the Dealer Lead Form capture page) -----
#
# A staff tool to fire a real ADF/XML lead at the dealer so they can confirm
# end-to-end delivery for the Credit Estimator product. The form is a copy of
# the dealer-leads capture page (name/email/phone, Year/Make/Model, comments,
# T&C). These leads carry the "Credit Pipeline" source; the sub-source is chosen
# on the form (the dealer's CRM reads them off the ADF <provider> block).
LEAD_SOURCE = "Credit Pipeline"
SUBSOURCES = ("Trigger Lead", "Auto Inquiry (combined)")


@app.route("/c/<dealer_id>/test-lead", methods=["GET", "POST"])
def test_lead(dealer_id):
    dealer = _require_active_dealer(dealer_id)

    if request.method == "GET":
        # Pre-fill contact from the shared cookie; allow ?year=&make=&model=.
        prefill = dict(contact_cookie.read(request))
        for short, full in (
            ("year", "vehicle_year"),
            ("make", "vehicle_make"),
            ("model", "vehicle_model"),
        ):
            val = (request.args.get(short) or request.args.get(full) or "").strip()
            if val:
                prefill[full] = val
        return render_template(
            "test_lead.html", dealer=dealer, errors={}, form=prefill,
            ymm=vin_db.is_enabled(), banner_override=_banner_override(),
            subsources=SUBSOURCES, sent=request.args.get("sent") == "1",
        )

    form = {k: (request.form.get(k) or "").strip() for k in (
        "first_name", "last_name", "email", "phone", "comments",
        "vehicle_year", "vehicle_make", "vehicle_model", "subsource",
    )}
    tc = request.form.get("tc_agree") == "on"

    # Validation: email OR phone is required; a sub-source and T&C are required.
    errors = {}
    if not form["email"] and not form["phone"]:
        errors["contact"] = "Please provide an email address or a phone number."
    if form["email"] and "@" not in form["email"]:
        errors["email"] = "Please enter a valid email address."
    if form["subsource"] not in SUBSOURCES:
        errors["subsource"] = "Please choose a sub-source."
    if not tc:
        errors["tc"] = "Please agree to the terms and conditions."

    validation = {"verdict": None, "score": None}
    if not errors and form["email"]:
        validation = validate_email(form["email"])
        if not validation["accept"]:
            errors["email"] = (
                "That email address doesn't appear to be deliverable. "
                "Please check it, or leave it blank and provide a phone number."
            )

    if errors:
        return render_template(
            "test_lead.html", dealer=dealer, errors=errors, form=form,
            ymm=vin_db.is_enabled(), banner_override=_banner_override(),
            subsources=SUBSOURCES, sent=False,
        ), 400

    # Build + send a real ADF to the dealer, then store the lead.
    lead = dict(form)
    lead["dealer_id"] = dealer_id
    lead["source"] = LEAD_SOURCE
    lead["tc_agreed"] = 1
    lead["tc_agreed_at"] = datetime.utcnow().isoformat()
    lead["email_verdict"] = validation.get("verdict")
    lead["email_score"] = validation.get("score")
    adf_xml = build_adf(lead, dealer, estimate=None,
                        source=LEAD_SOURCE, subsource=form["subsource"])
    lead["adf_xml"] = adf_xml
    status, detail = send_adf(dealer, adf_xml, lead_id="new", lead=lead)
    lead["email1_status"], lead["email1_detail"] = status, detail
    lead_id = db.insert_lead(lead)
    db.set_email_status(lead_id, 1, status, detail)

    return redirect(url_for("test_lead", dealer_id=dealer_id, sent="1"))


# ----- vehicle Year/Make/Model lookup (powers the test-lead dropdowns) -----

@app.route("/api/vehicle/years")
def api_years():
    if not vin_db.is_enabled():
        return jsonify([])
    try:
        return jsonify(vin_db.get_years())
    except Exception as exc:  # never break the form on a lookup failure
        app.logger.warning("YMM years lookup failed: %s", exc)
        return jsonify([])


@app.route("/api/vehicle/makes")
def api_makes():
    if not vin_db.is_enabled():
        return jsonify([])
    try:
        return jsonify(vin_db.get_makes(request.args.get("year")))
    except Exception as exc:
        app.logger.warning("YMM makes lookup failed: %s", exc)
        return jsonify([])


@app.route("/api/vehicle/models")
def api_models():
    if not vin_db.is_enabled():
        return jsonify([])
    try:
        return jsonify(
            vin_db.get_models(request.args.get("year"), request.args.get("make"))
        )
    except Exception as exc:
        app.logger.warning("YMM models lookup failed: %s", exc)
        return jsonify([])


if __name__ == "__main__":
    pdb.init_db()
    db.init_db()
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5003)),
            debug=debug, threaded=True)
