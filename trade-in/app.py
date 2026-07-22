"""Trade-In Widget — Flask application (mobile-first, 3-step wizard).

Flow per dealer at /t/<dealer_id>:
    step 1  vehicle  (Year / Make / Model / Trim)         -> session
    step 2  contact  (name / email / phone / T&C)         -> create lead + ADF email #1
    step 3  condition (keys, lights, mods, ownership...)  -> value + updated ADF email #2
    thanks  value + map + QR + email/text the value

Dealers, product access (TRADE_IN) and valuation settings come from the shared
platform DB. The widget is unavailable unless the dealer has an active grant.
"""

import os
import secrets
import sys
import urllib.parse
from datetime import datetime

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
import vin_db
import valuation as valuation_mod
from adf import build_adf
from email_send import send_adf, send_html_email
from email_validate import validate_email
from qr import qr_svg
from barcode_gen import barcode_svg, barcode_png

PRODUCT_CODE = pdb.PRODUCT_TRADE_IN

# Serial: TI-<DEALER>-<YYMMDD>-<6 chars>. Alphabet excludes 0/O/1/I/etc. for
# legibility on a printed/scanned certificate.
SERIAL_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def gen_serial(dealer_id):
    day = datetime.utcnow().strftime("%y%m%d")
    safe = "".join(c for c in (dealer_id or "").upper() if c.isalnum())[:8] or "TRADE"
    for _ in range(12):
        suffix = "".join(secrets.choice(SERIAL_ALPHABET) for _ in range(6))
        serial = f"TI-{safe}-{day}-{suffix}"
        if not db.get_trade_lead_by_serial(serial):
            return serial
    return serial

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")

# This widget is embedded in dealer sites via a CROSS-SITE <iframe>, so the
# wizard's session cookie must be SameSite=None (+ Secure, which the browser
# requires whenever SameSite=None). With Flask's default SameSite=Lax the browser
# refuses to send the cookie on the step POSTs, the session reads back empty, and
# the form bounces back to step 1 (year/make/model) — while working fine when the
# page is opened top-level. Partitioned (CHIPS) gives the embed its own cookie jar
# keyed to the embedding site, so it keeps working under Chrome's third-party
# cookie blocking. Overridable via env for local http development.
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
    return contact_cookie.persist(request, response)


@app.before_request
def _ensure_db():
    if not getattr(app, "_db_ready", False):
        pdb.init_db()
        db.init_db()
        try:
            db.init_wizard_state()          # cookie-free wizard state (see below)
            db.purge_wizard_state(WIZARD_TTL_HOURS)
        except Exception:                   # never block a lead over housekeeping
            pass
        app._db_ready = True


def _require_active_dealer(dealer_id):
    """Return dealer dict or abort with the right page."""
    dealer = pdb.get_dealer(dealer_id)
    if not dealer:
        abort(404, description="Unknown dealer.")
    if not pdb.dealer_has_product(dealer_id, PRODUCT_CODE):
        # 403 with a friendly inactive page (handled below).
        abort(403)
    return dealer


@app.errorhandler(403)
def _inactive(e):
    dealer_id = request.view_args.get("dealer_id") if request.view_args else None
    dealer = pdb.get_dealer(dealer_id) if dealer_id else None
    return render_template("inactive.html", dealer=dealer), 403


def _session_key(dealer_id):
    return f"tradein:{dealer_id}"


# ----- wizard state: server-side token, so no cookie is required -----
#
# Embedded cross-site in a dealer page, Safari (and any browser blocking
# third-party cookies) won't carry the session cookie between steps, so the form
# would bounce back to step 1. The wizard therefore keys its state off a random
# token ("sid") that rides in the form POST and the redirect URL. The cookie
# session is still written/read as a fallback, so nothing regresses where cookies
# do work (and for links that arrive with no token).

WIZARD_TTL_HOURS = int(os.environ.get("TRADE_WIZARD_TTL_HOURS", "12"))


def _new_sid():
    return secrets.token_urlsafe(24)


def _sid_or_new():
    """The wizard token from this request (POST body or query string), else a new one."""
    return (request.form.get("sid") or request.args.get("sid") or "").strip() or _new_sid()


def _load_state(dealer_id, sid):
    """Wizard state: server-side store first (works with NO cookies at all), then
    the cookie session as a fallback."""
    if sid:
        try:
            st = db.get_wizard_state(sid)
            if st is not None:
                return st
        except Exception:                            # store hiccup -> fall back to cookie
            pass
    try:
        return session.get(_session_key(dealer_id), {}) or {}
    except Exception:
        return {}


def _save_state(dealer_id, sid, data):
    """Persist server-side (cookie-free) AND to the cookie session (harmless)."""
    if sid:
        try:
            db.put_wizard_state(sid, dealer_id, data)
        except Exception:
            pass
    try:
        session[_session_key(dealer_id)] = data
    except Exception:
        pass


def dealer_maps_url(dealer):
    parts = [dealer.get("address"), dealer.get("city"), dealer.get("state"), dealer.get("zip")]
    addr = ", ".join(p for p in parts if p) or dealer.get("dealer_name", "")
    return addr


def _open_trade_lead(dealer_id, dealer, data, validation=None, sid=None):
    """Create the trade lead and send the initial ADF (#1), recording the new
    lead_id in the session. Shared by the normal contact step and the
    Credit-Estimator handoff (which skips the contact step)."""
    validation = validation or {"verdict": None, "score": None}
    serial = data.get("serial") or gen_serial(dealer_id)
    lead = {
        "dealer_id": dealer_id, "serial": serial,
        "vehicle_year": data.get("vehicle_year"), "vehicle_make": data.get("vehicle_make"),
        "vehicle_model": data.get("vehicle_model"), "vehicle_trim": data.get("vehicle_trim"),
        "first_name": data.get("first_name"), "last_name": data.get("last_name"),
        "email": data.get("email"), "phone": data.get("phone"),
        "tc_agreed": 1, "tc_agreed_at": datetime.utcnow().isoformat(),
        "email_verdict": validation.get("verdict"), "email_score": validation.get("score"),
    }
    try:
        lead["offer_url"] = url_for("offer", serial=serial, _external=True)
    except Exception:
        lead["offer_url"] = None
    adf_xml = build_adf(lead, dealer, valuation=None, product_code=PRODUCT_CODE)
    lead["adf_xml"] = adf_xml
    status, detail = send_adf(dealer, adf_xml, lead_id="new", lead=lead, updated=False,
                              notify=True, product_code=PRODUCT_CODE)
    lead["email1_status"], lead["email1_detail"] = status, detail
    lead_id = db.insert_trade_lead(lead)
    db.set_email_status(lead_id, 1, status, detail)
    data["lead_id"] = lead_id
    data["serial"] = serial
    _save_state(dealer_id, sid, data)
    return lead_id


# ----- step 1: vehicle -----

@app.route("/t/<dealer_id>", methods=["GET", "POST"])
def step_vehicle(dealer_id):
    dealer = _require_active_dealer(dealer_id)
    sid = _sid_or_new()
    if request.method == "GET":
        # allow ?year=&make=&model=&trim= prefill
        prefill = {}
        for short, full in (("year", "vehicle_year"), ("make", "vehicle_make"),
                            ("model", "vehicle_model"), ("trim", "vehicle_trim")):
            v = (request.args.get(short) or "").strip()
            if v:
                prefill[full] = v
        saved = _load_state(dealer_id, sid)
        # Handoff from the Credit Estimator (ho=1): contact was already collected,
        # so stash it and flag the session to bypass the contact step.
        if request.args.get("ho") == "1":
            saved.update({
                "first_name": (request.args.get("fn") or "").strip(),
                "last_name": (request.args.get("ln") or "").strip(),
                "email": (request.args.get("email") or "").strip(),
                "phone": (request.args.get("phone") or "").strip(),
                "tc_agreed": 1, "handoff": True,
            })
            _save_state(dealer_id, sid, saved)
        form = {**saved, **prefill}
        return render_template("step_vehicle.html", dealer=dealer, form=form,
                               ymm=vin_db.is_enabled(), step=1, sid=sid)

    form = {k: (request.form.get(k) or "").strip() for k in (
        "vehicle_year", "vehicle_make", "vehicle_model", "vehicle_trim")}
    errors = {}
    if not (form["vehicle_year"] and form["vehicle_make"] and form["vehicle_model"]):
        errors["vehicle"] = "Please choose your vehicle's year, make, and model."
    if errors:
        return render_template("step_vehicle.html", dealer=dealer, form=form,
                               errors=errors, ymm=vin_db.is_enabled(), step=1, sid=sid), 400
    data = _load_state(dealer_id, sid)
    data.update(form)
    _save_state(dealer_id, sid, data)
    # Contact handed off from the Credit Estimator → skip the contact step.
    if data.get("handoff") and (data.get("email") or data.get("phone")):
        _open_trade_lead(dealer_id, dealer, data, sid=sid)
        return redirect(url_for("step_condition", dealer_id=dealer_id, sid=sid))
    return redirect(url_for("step_contact", dealer_id=dealer_id, sid=sid))


# ----- step 2: contact (creates lead + sends ADF #1) -----

@app.route("/t/<dealer_id>/contact", methods=["GET", "POST"])
def step_contact(dealer_id):
    dealer = _require_active_dealer(dealer_id)
    sid = _sid_or_new()
    data = _load_state(dealer_id, sid)
    if not data.get("vehicle_model"):
        return redirect(url_for("step_vehicle", dealer_id=dealer_id, sid=sid))

    if request.method == "GET":
        # Pre-fill from the shared cross-product cookie; session data wins.
        form = {**contact_cookie.read(request), **data}
        return render_template("step_contact.html", dealer=dealer, form=form,
                               errors={}, step=2, sid=sid)

    form = {k: (request.form.get(k) or "").strip() for k in
            ("first_name", "last_name", "email", "phone")}
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
            errors["email"] = ("That email address doesn't appear to be deliverable. "
                               "Please check it, or leave it blank and provide a phone.")
    if errors:
        return render_template("step_contact.html", dealer=dealer,
                               form={**data, **form}, errors=errors, step=2, sid=sid), 400

    data.update(form)
    data["tc_agreed"] = 1
    _save_state(dealer_id, sid, data)

    # Create the lead now and send the initial ADF.
    _open_trade_lead(dealer_id, dealer, data, validation, sid=sid)
    return redirect(url_for("step_condition", dealer_id=dealer_id, sid=sid))


# ----- step 3: condition (value + sends updated ADF #2) -----

CONDITION_FORM_FIELDS = (
    "num_keys", "unrepaired_damage", "engine_light", "airbag_light", "brake_light",
    "aftermarket_exhaust", "aftermarket_engine", "aftermarket_stereo",
    "own_or_lease", "miles", "ownership_status", "loan_balance", "lease_months_remaining",
)


@app.route("/t/<dealer_id>/condition", methods=["GET", "POST"])
def step_condition(dealer_id):
    dealer = _require_active_dealer(dealer_id)
    sid = _sid_or_new()
    data = _load_state(dealer_id, sid)
    if not data.get("lead_id"):
        return redirect(url_for("step_vehicle", dealer_id=dealer_id, sid=sid))

    if request.method == "GET":
        return render_template("step_condition.html", dealer=dealer, form=data,
                               lease_months=[str(i) for i in range(1, 37)] + ["37+"],
                               step=3, sid=sid)

    form = {k: (request.form.get(k) or "").strip() for k in CONDITION_FORM_FIELDS}
    data.update(form)
    _save_state(dealer_id, sid, data)
    lead_id = data["lead_id"]

    # Build the full lead record for valuation + ADF.
    lead = {**data, "dealer_id": dealer_id}
    if lead.get("serial"):
        try:
            lead["offer_url"] = url_for("offer", serial=lead["serial"], _external=True)
        except Exception:
            lead["offer_url"] = None
    settings = pdb.get_valuation_settings(dealer_id)
    result = valuation_mod.compute_value(lead, settings)

    adf_xml = build_adf(lead, dealer, valuation=result if result.get("ok") else None,
                        product_code=PRODUCT_CODE)
    val_cols = {}
    if result.get("ok"):
        val_cols = {"value_estimate": result.get("final_value"),
                    "value_low": result.get("range_low"),
                    "value_high": result.get("range_high"),
                    "value_source": result.get("base_source")}
    db.update_trade_condition(lead_id, {**form, "adf_xml": adf_xml, **val_cols})
    status, detail = send_adf(dealer, adf_xml, lead_id=lead_id, lead=lead, updated=True)
    db.set_email_status(lead_id, 2, status, detail)

    # Stash the valuation for the thank-you page.
    data["valuation"] = result
    _save_state(dealer_id, sid, data)
    return redirect(url_for("thank_you", dealer_id=dealer_id, sid=sid))


# ----- thank you -----

@app.route("/t/<dealer_id>/thanks")
def thank_you(dealer_id):
    dealer = _require_active_dealer(dealer_id)
    data = _load_state(dealer_id, _sid_or_new())
    result = data.get("valuation")

    # Vehicle description (Year Make Model Trim) for the market-units section.
    vehicle_desc = " ".join(str(x) for x in (
        data.get("vehicle_year"), data.get("vehicle_make"),
        data.get("vehicle_model"), data.get("vehicle_trim")) if x)

    serial = data.get("serial")
    offer_url = (url_for("offer", serial=serial, _external=True) if serial else None)
    barcode = barcode_svg(serial) if serial else None

    addr = dealer_maps_url(dealer)
    addr_q = urllib.parse.quote_plus(addr)
    maps_link = f"https://www.google.com/maps/search/?api=1&query={addr_q}"
    maps_embed = f"https://www.google.com/maps?q={addr_q}&output=embed"
    qr = qr_svg(maps_link)

    sms = _sms_link(dealer, vehicle_desc,
                    (result.get("range_low") if result and result.get("ok") else None),
                    (result.get("range_high") if result and result.get("ok") else None),
                    serial, offer_url)
    email_post = url_for("email_offer", serial=serial) if serial else None

    return render_template("thank_you.html", dealer=dealer, valuation=result,
                           vehicle_desc=vehicle_desc, serial=serial,
                           barcode_svg=barcode, offer_url=offer_url,
                           maps_link=maps_link, maps_embed=maps_embed, qr_svg=qr,
                           sms=sms, email_post=email_post,
                           has_email=bool(data.get("email")),
                           emailed=request.args.get("emailed"), step=4)


def _sms_link(dealer, vehicle_desc, value_low, value_high, serial, offer_url):
    """Plain-text SMS device link carrying the vehicle, value, offer #, and URL."""
    lines = [f"My trade-in offer from {dealer.get('dealer_name','')}:"]
    if vehicle_desc:
        lines.append(vehicle_desc)
    if value_low is not None and value_high is not None:
        lines.append(f"${value_low:,} - ${value_high:,}")
    if serial:
        lines.append(f"Offer #: {serial}")
    if offer_url:
        lines.append(f"View your offer: {offer_url}")
    return "sms:?&body=" + urllib.parse.quote("\n".join(lines))


def _render_offer_email(dealer, lead, serial, offer_url, maps_link):
    """Render the HTML offer email body for a lead (used by /offer/<serial>/email)."""
    vehicle_desc = " ".join(str(x) for x in (
        lead.get("vehicle_year"), lead.get("vehicle_make"),
        lead.get("vehicle_model"), lead.get("vehicle_trim")) if x)
    # Similar-for-sale: instant local sum from the prefix caches (no SQL).
    try:
        prefixes = db.get_vin8_set(lead.get("vehicle_make"), lead.get("vehicle_model"))
        similar = db.get_inv_count(prefixes) if prefixes else None
    except Exception:
        similar = None
    addr = ", ".join(str(x) for x in (dealer.get("address"), dealer.get("city"),
                                      dealer.get("state"), dealer.get("zip")) if x)
    banner = dealer.get("banner_url") or ""
    banner_abs = banner if banner[:4].lower() == "http" else None
    html = render_template(
        "email_offer.html", dealer=dealer, vehicle_desc=vehicle_desc,
        value_low=lead.get("value_low"), value_high=lead.get("value_high"),
        similar_count=similar, serial=serial, offer_url=offer_url,
        maps_link=maps_link, address=addr, banner_abs=banner_abs,
        barcode_cid="offerbarcode")
    return html


# ----- serialized offer recall -----

@app.route("/offer/<serial>")
def offer(serial):
    lead = db.get_trade_lead_by_serial(serial)
    if not lead:
        abort(404, description="Offer not found.")
    dealer = pdb.get_dealer(lead.get("dealer_id"))
    if not dealer:
        abort(404)

    vehicle_desc = " ".join(str(x) for x in (
        lead.get("vehicle_year"), lead.get("vehicle_make"),
        lead.get("vehicle_model"), lead.get("vehicle_trim")) if x)
    offer_url = url_for("offer", serial=serial, _external=True)
    barcode = barcode_svg(serial)

    addr = dealer_maps_url(dealer)
    addr_q = urllib.parse.quote_plus(addr)
    maps_link = f"https://www.google.com/maps/search/?api=1&query={addr_q}"
    maps_embed = f"https://www.google.com/maps?q={addr_q}&output=embed"

    sms = _sms_link(dealer, vehicle_desc, lead.get("value_low"),
                    lead.get("value_high"), serial, offer_url)
    return render_template(
        "offer.html", dealer=dealer, lead=lead, serial=serial,
        vehicle_desc=vehicle_desc, barcode_svg=barcode, offer_url=offer_url,
        maps_link=maps_link, maps_embed=maps_embed,
        sms=sms, email_post=url_for("email_offer", serial=serial),
        has_email=bool(lead.get("email")), emailed=request.args.get("emailed"))


@app.route("/offer/<serial>/email", methods=["POST"])
def email_offer(serial):
    lead = db.get_trade_lead_by_serial(serial)
    if not lead:
        abort(404)
    dealer = pdb.get_dealer(lead.get("dealer_id"))
    if not dealer or not lead.get("email"):
        return redirect(url_for("offer", serial=serial, emailed="no"))

    offer_url = url_for("offer", serial=serial, _external=True)
    addr_q = urllib.parse.quote_plus(dealer_maps_url(dealer))
    maps_link = f"https://www.google.com/maps/search/?api=1&query={addr_q}"
    html = _render_offer_email(dealer, lead, serial, offer_url, maps_link)
    subject = f"Your Trade-In Offer {serial} — {dealer.get('dealer_name','')}"
    status, _ = send_html_email(lead["email"], subject, html,
                                inline_png=barcode_png(serial), inline_cid="offerbarcode")
    return redirect(url_for("offer", serial=serial,
                            emailed=("1" if status == "sent" else "fail")))


# ----- vehicle Year/Make/Model/Trim lookup API -----

@app.route("/api/vehicle/years")
def api_years():
    try:
        return jsonify(vin_db.get_years() if vin_db.is_enabled() else [])
    except Exception:
        return jsonify([])


@app.route("/api/vehicle/makes")
def api_makes():
    try:
        return jsonify(vin_db.get_makes(request.args.get("year")) if vin_db.is_enabled() else [])
    except Exception:
        return jsonify([])


@app.route("/api/vehicle/models")
def api_models():
    try:
        return jsonify(vin_db.get_models(request.args.get("year"), request.args.get("make"))
                       if vin_db.is_enabled() else [])
    except Exception:
        return jsonify([])


@app.route("/api/vehicle/trims")
def api_trims():
    try:
        return jsonify(vin_db.get_trims(request.args.get("year"), request.args.get("make"),
                                        request.args.get("model")) if vin_db.is_enabled() else [])
    except Exception:
        return jsonify([])


if __name__ == "__main__":
    pdb.init_db()
    db.init_db()
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            debug=debug, threaded=True)
