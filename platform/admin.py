"""Platform admin — manage dealers and their product access grants.

One place to configure the shared dealer master and entitlements used by both
the dealer-leads and trade-in apps. Gated by SETUP_PASSWORD.
"""

import os
from functools import wraps

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from flask import (
    Flask, abort, flash, redirect, render_template, request, session, url_for
)

import platform_db as pdb
import leads_view

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")
SETUP_PASSWORD = os.environ.get("SETUP_PASSWORD", "")

# Behind the Cloudflare Tunnel / reverse proxy: trust one hop of forwarded
# headers so url_for(_external=True) yields https://<public-host>.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# Where each product's customer-facing app lives, so the admin can link to a
# dealer's live form. Override per environment in .env.
_CREDIT_BASE = os.environ.get("CREDIT_EST_BASE_URL", "http://localhost:5003")
PRODUCT_BASE_URLS = {
    pdb.PRODUCT_LEAD_FORM: os.environ.get("LEAD_FORM_BASE_URL", "http://localhost:5000"),
    pdb.PRODUCT_TRADE_IN: os.environ.get("TRADE_IN_BASE_URL", "http://localhost:5001"),
    pdb.PRODUCT_CREDIT_EST: _CREDIT_BASE,
    pdb.PRODUCT_CREDIT_PIPELINE: _CREDIT_BASE,   # served by the same credit app
}
PRODUCT_PATHS = {pdb.PRODUCT_LEAD_FORM: "/d/", pdb.PRODUCT_TRADE_IN: "/t/",
                 pdb.PRODUCT_CREDIT_EST: "/c/", pdb.PRODUCT_CREDIT_PIPELINE: "/c/"}


def product_url(product_code, dealer_id):
    base = PRODUCT_BASE_URLS.get(product_code)
    path = PRODUCT_PATHS.get(product_code)
    if not base or not path:
        return None
    return base.rstrip("/") + path + dealer_id

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
]


@app.before_request
def _ensure_db():
    if not getattr(app, "_db_ready", False):
        pdb.init_db()
        app._db_ready = True


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if SETUP_PASSWORD and not session.get("admin_ok"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if not SETUP_PASSWORD:
        return redirect(url_for("index"))
    nxt = request.values.get("next") or "/"
    if not nxt.startswith("/"):   # avoid open redirect
        nxt = "/"
    error = False
    if request.method == "POST":
        if request.form.get("password") == SETUP_PASSWORD:
            session["admin_ok"] = True
            return redirect(nxt)
        error = True
    return render_template("login.html", error=error, next=nxt)


@app.route("/logout")
def logout():
    session.pop("admin_ok", None)
    return redirect(url_for("login"))


@app.route("/")
@require_login
def index():
    return render_template(
        "admin.html",
        dealers=pdb.list_dealers(),
        states=US_STATES,
    )


@app.route("/products")
@require_login
def products():
    return render_template("products.html", products=pdb.list_products())


@app.route("/product/source", methods=["POST"])
@require_login
def save_product_source():
    code = (request.form.get("product_code") or "").strip()
    source = (request.form.get("source") or "").strip()
    if not code:
        flash("Missing product.", "error")
    else:
        pdb.update_product_source(code, source)
        flash(f"Saved source for {code}.", "ok")
    return redirect(url_for("products"))


@app.route("/dealer", methods=["POST"])
@require_login
def save_dealer():
    data = {k: (request.form.get(k) or "").strip() for k in (
        "dealer_id", "dealer_name", "address", "city", "state", "zip",
        "phone", "lead_email_address", "banner_url",
    )}
    if not (data["dealer_id"] and data["dealer_name"] and data["lead_email_address"]):
        flash("DealerID, Dealer Name and Lead Email Address are required.", "error")
        return redirect(url_for("index"))
    pdb.upsert_dealer(data)
    flash(f"Saved dealer {data['dealer_id']}.", "ok")
    return redirect(url_for("dealer", dealer_id=data["dealer_id"]))


@app.route("/dealer/<dealer_id>")
@require_login
def dealer(dealer_id):
    d = pdb.get_dealer(dealer_id)
    if not d:
        abort(404)
    grants = pdb.list_grants(dealer_id)
    for g in grants:
        g["url"] = product_url(g["product_code"], dealer_id)
    return render_template(
        "dealer.html",
        dealer=d,
        states=US_STATES,
        products=pdb.list_products(),
        grants=grants,
        vs=pdb.get_valuation_settings(dealer_id),
        recommended=pdb.RECOMMENDED,
        cond_fields=pdb.CONDITION_ADJ_FIELDS,
        cs=pdb.get_credit_settings(dealer_id),
        recommended_credit=pdb.RECOMMENDED_CREDIT,
        credit_apr_fields=pdb.CREDIT_APR_FIELDS,
        leads=leads_view.all_leads(dealer_id, limit=100),
    )


@app.route("/leads")
@require_login
def leads():
    product = request.args.get("product")
    if product == "LEAD_FORM":
        rows = leads_view.lead_form_leads()
    elif product == "TRADE_IN":
        rows = leads_view.trade_leads()
    elif product == "CREDIT_EST":
        rows = leads_view.credit_leads()
    else:
        rows = leads_view.all_leads()
    return render_template("leads.html", leads=rows, product=product,
                           dealers={d["dealer_id"]: d["dealer_name"] for d in pdb.list_dealers()})


@app.route("/lead/<product>/<int:lead_id>")
@require_login
def lead_detail(product, lead_id):
    product = product.upper()
    row, groups, adf_xml = leads_view.get_lead_detail(product, lead_id)
    if not row:
        abort(404)
    dealer = pdb.get_dealer(row.get("dealer_id"))
    return render_template(
        "lead_detail.html",
        product=product, lead=row, groups=groups,
        labels=leads_view.FIELD_LABELS, adf_xml=adf_xml, dealer=dealer,
    )


@app.route("/grant", methods=["POST"])
@require_login
def save_grant():
    dealer_id = request.form.get("dealer_id")
    data = {
        "dealer_id": dealer_id,
        "product_code": request.form.get("product_code"),
        "valid_from": (request.form.get("valid_from") or "").strip(),
        "valid_to": (request.form.get("valid_to") or "").strip(),
        "monthly_price": (request.form.get("monthly_price") or "").strip(),
        "per_lead_price": (request.form.get("per_lead_price") or "").strip(),
    }
    if not (data["dealer_id"] and data["product_code"]):
        flash("Dealer and product are required.", "error")
    else:
        pdb.upsert_grant(data)
        flash(f"Saved {data['product_code']} grant.", "ok")
    return redirect(url_for("dealer", dealer_id=dealer_id))


@app.route("/valuation", methods=["POST"])
@require_login
def save_valuation():
    dealer_id = request.form.get("dealer_id")
    data = {"dealer_id": dealer_id,
            "base_source": request.form.get("base_source", "retail"),
            "adjustment_unit": request.form.get("adjustment_unit", "dollar")}
    # numeric fields
    for f in ("range_spread", "mileage_rate") + pdb.CONDITION_ADJ_FIELDS:
        raw = (request.form.get(f) or "").strip()
        try:
            data[f] = float(raw) if raw != "" else 0.0
        except ValueError:
            data[f] = 0.0
    pdb.upsert_valuation_settings(data)
    flash("Saved valuation settings.", "ok")
    return redirect(url_for("dealer", dealer_id=dealer_id))


@app.route("/credit-settings", methods=["POST"])
@require_login
def save_credit_settings():
    dealer_id = request.form.get("dealer_id")
    data = {"dealer_id": dealer_id}
    for f in pdb.CREDIT_SETTING_FIELDS:
        raw = (request.form.get(f) or "").strip()
        try:
            data[f] = float(raw) if raw != "" else 0.0
        except ValueError:
            data[f] = 0.0
    # max_term_months is a whole number of months.
    data["max_term_months"] = int(round(data.get("max_term_months") or 0))
    pdb.upsert_credit_settings(data)
    flash("Saved credit estimator settings.", "ok")
    return redirect(url_for("dealer", dealer_id=dealer_id))


@app.route("/grant/<int:grant_id>/delete", methods=["POST"])
@require_login
def remove_grant(grant_id):
    dealer_id = request.form.get("dealer_id")
    pdb.delete_grant(grant_id)
    flash("Removed grant.", "ok")
    return redirect(url_for("dealer", dealer_id=dealer_id))


if __name__ == "__main__":
    pdb.init_db()
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)),
            debug=debug, threaded=True)
