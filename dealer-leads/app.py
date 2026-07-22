"""Dealer Lead Form — Flask application.

Public routes:
    GET  /d/<dealer_id>          Customer data-collection page (the lead form)
    POST /d/<dealer_id>          Submit a lead; sends ADF/XML, then redirects
    GET  /d/<dealer_id>/thanks   Thank-you page

Dealers and product access (the LEAD_FORM grant) come from the shared platform
DB; the form is unavailable unless the dealer has an active grant. Manage
dealers + grants in the platform admin. Leads are stored locally.
"""

import os
import sys

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
    Flask, abort, jsonify, redirect, render_template, request, url_for
)

import platform_db as pdb
import contact_cookie
import db
import vin_db
from adf import build_adf
from email_send import send_adf
from email_validate import validate_email

PRODUCT_CODE = pdb.PRODUCT_LEAD_FORM

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")

# Embedded cross-site in dealer pages via <iframe>: the session cookie must be
# SameSite=None + Secure or the browser drops it on form POSTs and the session
# reads back empty. Partitioned (CHIPS) keeps it working under third-party cookie
# blocking. Overridable via env for local http development.
app.config.update(
    SESSION_COOKIE_SAMESITE=os.environ.get("SESSION_COOKIE_SAMESITE", "None"),
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1") == "1",
    SESSION_COOKIE_PARTITIONED=os.environ.get("SESSION_COOKIE_PARTITIONED", "1") == "1",
)

# Behind the Cloudflare Tunnel / reverse proxy: trust one hop of forwarded
# headers so url_for(_external=True) yields https://<public-host>.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


@app.after_request
def _share_pii(response):
    # Persist any submitted name/email/phone to the shared .dlrpro.com cookie.
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
    """Optional per-link banner image via ?IMG=<url>. Restricted to http(s) so
    it can only point at an image, not a javascript:/data: payload."""
    img = (request.values.get("IMG") or request.values.get("img") or "").strip()
    low = img.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return img
    return None


@app.errorhandler(403)
def _inactive(e):
    dealer_id = request.view_args.get("dealer_id") if request.view_args else None
    dealer = pdb.get_dealer(dealer_id) if dealer_id else None
    return render_template("inactive.html", dealer=dealer), 403


# ----- public lead form -----

@app.route("/d/<dealer_id>", methods=["GET", "POST"])
def lead_form(dealer_id):
    dealer = _require_active_dealer(dealer_id)

    if request.method == "GET":
        # Pre-fill name/email/phone from the shared cross-product cookie, then
        # allow ?year=&make=&model= (or vehicle_*-prefixed) to pre-populate the
        # vehicle dropdowns/inputs — handy for links from inventory listings.
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
            "lead_form.html", dealer=dealer, errors={}, form=prefill,
            ymm=vin_db.is_enabled(), banner_override=_banner_override(),
        )

    form = {k: (request.form.get(k) or "").strip() for k in (
        "first_name", "last_name", "email", "phone", "comments",
        "vehicle_year", "vehicle_make", "vehicle_model",
    )}
    tc = request.form.get("tc_agree") == "on"

    # Validation: email OR phone is required; T&C must be agreed.
    errors = {}
    if not form["email"] and not form["phone"]:
        errors["contact"] = "Please provide an email address or a phone number."
    if form["email"] and "@" not in form["email"]:
        errors["email"] = "Please enter a valid email address."
    if not tc:
        errors["tc"] = "Please agree to the terms and conditions."

    # Verify deliverability via SendGrid's Email Validation API (fails open).
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
            ymm=vin_db.is_enabled(), banner_override=_banner_override(),
        ), 400

    # Persist the lead first so its id can serve as the ADF source-lineage id,
    # then build + attach the ADF/XML and deliver it.
    lead = dict(form)
    lead["dealer_id"] = dealer_id
    lead["source"] = request.headers.get("Referer") or request.url
    lead["email_verdict"] = validation.get("verdict")
    lead["email_score"] = validation.get("score")
    lead["adf_xml"] = None
    lead["email_status"] = "pending"
    lead["email_detail"] = None
    lead_id = db.insert_lead(lead)

    lead["id"] = lead_id
    adf_xml = build_adf(lead, dealer, product_code=PRODUCT_CODE)
    db.update_adf(lead_id, adf_xml)

    # Deliver the ADF/XML to the dealer's leadEmailAddress.
    status, detail = send_adf(dealer, adf_xml, lead_id, lead=lead,
                              notify=True, product_code=PRODUCT_CODE)
    db.update_lead_email_status(lead_id, status, detail)

    return redirect(url_for("thank_you", dealer_id=dealer_id))


@app.route("/d/<dealer_id>/thanks")
def thank_you(dealer_id):
    dealer = _require_active_dealer(dealer_id)
    return render_template("thank_you.html", dealer=dealer)


# ----- vehicle Year/Make/Model lookup (cascading dropdowns) -----

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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            debug=debug, threaded=True)
