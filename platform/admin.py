"""Platform admin — manage dealers and their product access grants.

One place to configure the shared dealer master and entitlements used by both
the dealer-leads and trade-in apps. Gated by SETUP_PASSWORD.
"""

import os
import subprocess
import sys
from functools import wraps

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
)

import platform_db as pdb
import leads_view
import stats_view
import scrapers_view
import health_view
import append_view
import map_view
import revenue_view
import stall_monitor
import tunnel_monitor

# The Credit Pipeline send path (build ADF -> email -> store -> record in `sent`)
# lives in the credit app and is shared with the poller. Import it so the admin's
# "Send Lead" button uses the exact same process (in-process; no public endpoint).
CREDIT_DIR = os.environ.get(
    "CREDIT_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "credit-estimator"))
if CREDIT_DIR not in sys.path:
    sys.path.insert(0, CREDIT_DIR)
try:
    import pipeline_source as _cp_source
    from pipeline_poller import send_lead as _cp_send_lead, eligible as _cp_eligible
    _CP_SEND_OK = True
except Exception as _cp_exc:  # keep the admin working even if the send path is unavailable
    _CP_SEND_OK = False

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")
SETUP_PASSWORD = os.environ.get("SETUP_PASSWORD", "")

# Behind the Cloudflare Tunnel / reverse proxy: trust one hop of forwarded
# headers so url_for(_external=True) yields https://<public-host>.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# Stall monitor: a daemon thread in this always-on web app watches poller health
# and texts / self-heals on a stall (the launchd scheduler itself is unreliable).
try:
    stall_monitor.start()
except Exception as _sm_exc:  # never let the monitor block the admin app
    pass

# Tunnel monitor: same pattern — watches the Cloudflare tunnel (cloudflared) and
# restarts it + texts the on-call number if it dies (e.g. a cloudflared
# self-update killed it). Alerts over Twilio directly, so a dead tunnel can't
# silence its own alarm.
try:
    tunnel_monitor.start()
except Exception as _tm_exc:
    pass


# Cache-bust static CSS by its mtime so browsers pick up style changes after a deploy.
_STYLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "style.css")


@app.context_processor
def _assets():
    try:
        return {"css_v": int(os.path.getmtime(_STYLE_PATH))}
    except OSError:
        return {"css_v": 0}

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
                 pdb.PRODUCT_CREDIT_EST: "/c/", pdb.PRODUCT_CREDIT_PIPELINE: "/p/"}


def product_url(product_code, dealer_id):
    base = PRODUCT_BASE_URLS.get(product_code)
    path = PRODUCT_PATHS.get(product_code)
    if not base or not path:
        return None
    return base.rstrip("/") + path + dealer_id


# Embeddable lead forms: the iframe install code a dealer drops into their own
# site. (iframe title, pixel height) per product — Credit Pipeline is a bureau
# feed with no customer-facing form, so it has no embed.
PRODUCT_EMBEDS = {
    pdb.PRODUCT_TRADE_IN: ("Value Your Trade", 1750),
    pdb.PRODUCT_CREDIT_EST: ("Estimate Your Credit", 1750),
    pdb.PRODUCT_LEAD_FORM: ("Get in Touch", 1750),
}


def embed_code(product_code, dealer_id):
    """The dealer-specific iframe install code for an embeddable product, or
    None. The wrapper div + lazy iframe match the markup dealers already use."""
    meta = PRODUCT_EMBEDS.get(product_code)
    url = product_url(product_code, dealer_id)
    if not meta or not url:
        return None
    title, height = meta
    return (
        '<div class="visible-xs">\n'
        '<iframe loading="lazy" src="%s" width="100%%" height="%d" frameborder="0" '
        'title="%s" id="%s"></iframe>\n'
        '</div>' % (url, height, title, dealer_id)
    )

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
    return redirect(url_for("dealers_list"))


@app.route("/dealers/add")
@require_login
def dealers_add():
    return render_template(
        "dealers_add.html",
        states=US_STATES,
        crm_types=pdb.list_crm_types(),
        lead_sources=pdb.list_lead_sources(),
        default_source=pdb.DEFAULT_LEAD_SOURCE,
        on_dealers_add=True,
    )


# Per-dealer product columns on the dealers list: (label, tooltip, product_code).
DEALER_PRODUCT_COLS = [
    ("CP", "Credit Pipeline product on", pdb.PRODUCT_CREDIT_PIPELINE),
    ("TR", "Trade-In product on", pdb.PRODUCT_TRADE_IN),
    ("CE", "Credit Estimator product on", pdb.PRODUCT_CREDIT_EST),
    ("DL", "Dealer Lead Form product on", pdb.PRODUCT_LEAD_FORM),
    ("VMS", "VMS system on", pdb.PRODUCT_CDP_VMS),
    ("CDP", "CDP system on", pdb.PRODUCT_CDP_CRM),
]


@app.route("/dealers/list")
@require_login
def dealers_list():
    return render_template(
        "dealers_list.html",
        dealers=pdb.list_dealers(),
        product_cols=DEALER_PRODUCT_COLS,
        grants=pdb.active_grants_by_dealer(),
        paused=pdb.paused_by_dealer(),
        on_dealers_list=True,
    )


@app.route("/stats/program")
@require_login
def stats_program():
    return render_template("stats_program.html",
                           s=stats_view.program_stats(), on_stats_program=True)


@app.route("/stats/leads")
@require_login
def stats_leads():
    scope = request.args.get("scope", "all")
    if scope not in ("all", "delivered"):
        scope = "all"
    return render_template("stats_leads.html",
                           l=stats_view.lead_stats(), c=stats_view.consumer_stats(),
                           scope=scope, on_stats_leads=True)


@app.route("/status")
@require_login
def status():
    """Dashboard: health of the automated Credit Pipeline send process."""
    return render_template("status.html", h=health_view.send_health(), on_status=True)


_SCRAPERS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scrapers")


@app.route("/auction/scrapers")
@require_login
def scraper_status():
    """Auction scraper management: status, enable/disable, ad-hoc run, reports."""
    return render_template("scraper_status.html",
                           sites=scrapers_view.all_status(), on_scrapers=True)


@app.route("/auction/scrapers/<site>/toggle", methods=["POST"])
@require_login
def scraper_toggle(site):
    if site not in scrapers_view.SITES:
        abort(404)
    want = request.form.get("enable") == "1"
    scrapers_view.set_enabled(site, want)
    flash("%s scraper %s." % (site.capitalize(), "enabled" if want else "disabled"), "ok")
    return redirect(url_for("scraper_status"))


@app.route("/auction/scrapers/<site>/run", methods=["POST"])
@require_login
def scraper_run(site):
    """Kick off an ad-hoc run now (bypasses the enabled flag via FORCE=1)."""
    if site not in scrapers_view.SITES:
        abort(404)
    run_sh = os.path.join(_SCRAPERS_DIR, "run.sh")
    log = os.path.join(os.path.dirname(_SCRAPERS_DIR), "deploy", "scraper.%s.out.log" % site)
    try:
        env = dict(os.environ, FORCE="1")
        lf = open(log, "a")
        subprocess.Popen(["/bin/bash", run_sh, site], stdout=lf,
                         stderr=subprocess.STDOUT, env=env, cwd=_SCRAPERS_DIR,
                         start_new_session=True)
        flash("Started an ad-hoc run for %s — refresh in a bit for results." % site.capitalize(), "ok")
    except Exception as e:
        flash("Couldn't start %s: %s" % (site, e), "error")
    return redirect(url_for("scraper_status"))


@app.route("/poller-restart", methods=["POST"])
@require_login
def poller_restart():
    """Manually restart (kickstart) the Credit Pipeline poller."""
    try:
        ok = stall_monitor.kick_poller()
    except Exception:
        ok = False
    flash("Poller restart requested — it should run within a few seconds." if ok
          else "Could not restart the poller.", "ok" if ok else "error")
    return redirect(url_for("status"))


@app.route("/revenue")
@require_login
def revenue_report():
    """Revenue report: leads x per-lead price, by month/day/dealer/lead type."""
    return render_template("revenue.html",
                           r=revenue_view.revenue_stats(request.args.get("month")),
                           on_revenue=True)


@app.route("/append")
@require_login
def append_report():
    """Phone/email append activity (imdatacenter) + sends, by day and month."""
    un_rows, un_total = append_view.unappended(limit=300)
    ap_rows, ap_total = append_view.appended(limit=300)
    eligible_total = 0
    if _CP_SEND_OK:
        try:
            import pipeline_enrich as _pe
            eligible_total = _pe.appendable_count(eligible=True)
        except Exception:
            eligible_total = 0
    return render_template("append.html",
                           a=append_view.append_stats(request.args.get("month")),
                           unappended=un_rows, unappended_total=un_total,
                           eligible_total=eligible_total,
                           appended=ap_rows, appended_total=ap_total,
                           drain_running=(pdb.get_setting("append_drain_running") == "1"),
                           on_append=True)


def _append_one(result_id):
    """Run the imdatacenter append for one trigger-view record (name/address from
    the view). Returns (email, phones)."""
    import pipeline_enrich as _pe
    ls, db = _cp_source.LINKED_SERVER, _cp_source.DB
    r = _cp_source.dlr.one(
        "SELECT FirstName, LastName, Address1, City, State, ZipCode "
        "FROM [%s].[%s].[dbo].[vw_EquifaxConsumerRecordTriggers] WHERE result_id=%%(r)s" % (ls, db),
        {"r": int(result_id)}) or {}
    lead = {"first_name": r.get("FirstName"), "last_name": r.get("LastName"),
            "address": r.get("Address1"), "city": r.get("City"),
            "state": r.get("State"), "zip": r.get("ZipCode")}
    return _pe._append_contact(int(result_id), lead)


@app.route("/append/one/<int:result_id>", methods=["POST"])
@require_login
def append_one(result_id):
    if not _CP_SEND_OK:
        flash("Append unavailable — credit modules failed to load.", "error")
        return redirect(url_for("append_report"))
    try:
        email, phones = _append_one(result_id)
        flash("Appended #%d — %d phone(s), email: %s." % (result_id, len(phones or []), email or "none"), "ok")
    except Exception as e:
        flash("Append failed for #%d: %s" % (result_id, str(e)[:140]), "error")
    return redirect(url_for("append_report"))


@app.route("/append/diag/<int:result_id>", methods=["POST"])
@require_login
def append_diag(result_id):
    """Run one append with full request/response capture (for the diagnostic
    modal), and record it on success. Returns the raw exchange as JSON."""
    if not _CP_SEND_OK:
        return jsonify({"error": "credit modules failed to load"}), 500
    from datetime import datetime as _dt
    import append_api as _aa
    ls, db = _cp_source.LINKED_SERVER, _cp_source.DB
    r = _cp_source.dlr.one(
        "SELECT FirstName, LastName, Address1, City, State, ZipCode "
        "FROM [%s].[%s].[dbo].[vw_EquifaxConsumerRecordTriggers] WHERE result_id=%%(r)s" % (ls, db),
        {"r": result_id}) or {}
    dbg = _aa.append_debug(r.get("FirstName"), r.get("LastName"), r.get("Address1"),
                           r.get("City"), r.get("State"), r.get("ZipCode"))
    dbg["result_id"] = result_id
    dbg["record"] = {k: r.get(k) for k in ("FirstName", "LastName", "Address1", "City", "State", "ZipCode")}
    dbg["timestamp"] = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    dbg["logged"] = False
    try:
        if dbg.get("ok") and dbg.get("parsed") and not pdb.get_append(result_id):
            p = dbg["parsed"]
            pdb.record_append(result_id, r.get("FirstName"), r.get("LastName"), r.get("Address1"),
                              r.get("City"), r.get("State"), r.get("ZipCode"),
                              p.get("email"), p.get("phones") or [], p.get("status"))
            dbg["logged"] = True
            pdb.resume_append_api()
        elif not dbg.get("ok"):
            pdb.pause_append_api()
    except Exception as e:
        dbg["log_error"] = str(e)[:200]
    return jsonify(dbg)


@app.route("/append/send/<int:result_id>", methods=["POST"])
@require_login
def append_send(result_id):
    """Send one appended lead to its dealer (from the Appended report)."""
    if not _CP_SEND_OK:
        flash("Send unavailable — credit modules failed to load.", "error")
        return redirect(url_for("append_report"))
    row = _cp_source.fetch_one(result_id)
    if not row:
        flash(f"Result #{result_id} can't be sent (already sent, or no matched dealer).", "error")
        return redirect(url_for("append_report"))
    ok, reason = _cp_eligible(row)
    if not ok:
        flash(f"Result #{result_id} not sent — {reason}.", "error")
        return redirect(url_for("append_report"))
    status, detail, lead_id = _cp_send_lead(row)
    flash(f"Result #{result_id}: {status} — {detail}",
          "ok" if status in ("sent", "pending") else "error")
    return redirect(url_for("append_report"))


def _launch_drainer(eligible=False):
    """Launch the detached append drainer (whole backlog, or the eligible subset).
    Returns (ok, message)."""
    if pdb.get_setting("append_drain_running") == "1":
        return False, "An append-all is already running in the background."
    try:
        import subprocess
        drainer = os.path.join(CREDIT_DIR, "append_drainer.py")
        # Use the shared venv python (has pymssql/requests) — the sibling of the
        # credit app dir — not necessarily this process's interpreter.
        venv_py = os.path.join(os.path.dirname(CREDIT_DIR), "dealer-leads", ".venv", "bin", "python")
        python = os.environ.get("APPEND_PYTHON",
                                venv_py if os.path.exists(venv_py) else sys.executable)
        args = [python, drainer] + (["eligible"] if eligible else [])
        subprocess.Popen(args, cwd=CREDIT_DIR,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        which = "Eligible append" if eligible else "Append-all"
        return True, ("%s started — draining in the background. "
                      "Refresh to watch the count drop." % which)
    except Exception as e:
        return False, "Could not start the drainer: %s" % str(e)[:140]


@app.route("/append/all", methods=["POST"])
@require_login
def append_all():
    ok, msg = _launch_drainer(eligible=False)
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("append_report"))


@app.route("/append/all-eligible", methods=["POST"])
@require_login
def append_all_eligible():
    """Append only the sendable subset: fresh (≤ send window) records whose dealer
    has an active Credit Pipeline grant."""
    ok, msg = _launch_drainer(eligible=True)
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("append_report"))


@app.route("/map")
@require_login
def lead_map():
    """Map of where leads originated (by consumer state), over a date range."""
    return render_template("map.html",
                           m=map_view.leads_map(request.args.get("start"), request.args.get("end"),
                                                request.args.get("mode")),
                           on_map=True)


@app.route("/pipeline-flow", methods=["POST"])
@require_login
def pipeline_flow():
    """Global on/off switch for automated Credit Pipeline lead delivery."""
    enable = request.form.get("enable") == "1"
    pdb.set_pipeline_flow(enable)
    flash(f"Credit Pipeline lead flow turned {'ON' if enable else 'OFF'}.", "ok")
    return redirect(url_for("trigger_leads"))


@app.route("/pipeline-interval", methods=["POST"])
@require_login
def pipeline_interval():
    """Set the send interval (minutes between automated sends to a dealer, 1–30)."""
    try:
        n = int(request.form.get("interval_min") or 5)
    except ValueError:
        n = 5
    pdb.set_pipeline_interval(n)
    flash(f"Send interval set to {pdb.get_pipeline_interval()} min between sends to a dealer.", "ok")
    return redirect(url_for("status"))


@app.route("/dealer/pause", methods=["POST"])
@require_login
def dealer_pause():
    """Pause/resume a dealer's grant (keeps the product; stops sends)."""
    dealer_id = request.form.get("dealer_id")
    product_code = request.form.get("product_code") or pdb.PRODUCT_CREDIT_PIPELINE
    paused = request.form.get("paused") == "1"
    pdb.set_dealer_paused(dealer_id, product_code, paused)
    flash(f"{product_code} {'paused' if paused else 'resumed'} for this dealer.", "ok")
    return redirect(url_for("dealer", dealer_id=dealer_id))


@app.route("/bcc")
@require_login
def bcc_settings():
    """Manage the BCC recipient list for Credit Pipeline lead emails."""
    return render_template("bcc.html", bcc=pdb.get_lead_bcc(), on_bcc=True)


@app.route("/bcc/add", methods=["POST"])
@require_login
def bcc_add():
    email = (request.form.get("email") or "").strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        flash("Enter a valid email address.", "error")
    else:
        cur = pdb.get_lead_bcc()
        if email.lower() in [e.lower() for e in cur]:
            flash(f"{email} is already on the BCC list.", "error")
        else:
            cur.append(email)
            pdb.set_lead_bcc(cur)
            flash(f"Added {email} to the BCC list.", "ok")
    return redirect(url_for("bcc_settings"))


@app.route("/bcc/remove", methods=["POST"])
@require_login
def bcc_remove():
    email = (request.form.get("email") or "").strip()
    pdb.set_lead_bcc([e for e in pdb.get_lead_bcc() if e.lower() != email.lower()])
    flash(f"Removed {email} from the BCC list.", "ok")
    return redirect(url_for("bcc_settings"))


@app.route("/products")
@require_login
def products():
    return render_template("products.html", products=pdb.list_products())


@app.route("/crm-types")
@require_login
def crm_types():
    return render_template("crm_types.html", crm_types=pdb.list_crm_types())


@app.route("/crm-type", methods=["POST"])
@require_login
def save_crm_type():
    crm_id = (request.form.get("id") or "").strip()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("CRM name is required.", "error")
    else:
        try:
            if crm_id:
                pdb.update_crm_type(int(crm_id), name)
                flash("Updated CRM type.", "ok")
            else:
                pdb.add_crm_type(name)
                flash(f'Added CRM type "{name}".', "ok")
        except Exception:
            flash(f'Could not save "{name}" — that CRM name may already exist.', "error")
    return redirect(url_for("crm_types"))


@app.route("/crm-type/<int:crm_id>/delete", methods=["POST"])
@require_login
def remove_crm_type(crm_id):
    pdb.delete_crm_type(crm_id)
    flash("Removed CRM type.", "ok")
    return redirect(url_for("crm_types"))


@app.route("/lead-sources")
@require_login
def lead_sources():
    return render_template("lead_sources.html", lead_sources=pdb.list_lead_sources(),
                           default_source=pdb.DEFAULT_LEAD_SOURCE)


@app.route("/lead-source", methods=["POST"])
@require_login
def save_lead_source():
    source_id = (request.form.get("id") or "").strip()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Lead source name is required.", "error")
    else:
        try:
            if source_id:
                pdb.update_lead_source(int(source_id), name)
                flash("Updated lead source.", "ok")
            else:
                pdb.add_lead_source(name)
                flash(f'Added lead source "{name}".', "ok")
        except Exception:
            flash(f'Could not save "{name}" — that name may already exist.', "error")
    return redirect(url_for("lead_sources"))


@app.route("/lead-source/<int:source_id>/delete", methods=["POST"])
@require_login
def remove_lead_source(source_id):
    pdb.delete_lead_source(source_id)
    flash("Removed lead source.", "ok")
    return redirect(url_for("lead_sources"))


@app.route("/subsources")
@require_login
def subsources():
    # Every bucketType seen in the trigger feed (with counts + current descriptor),
    # plus any mapped bucketType not currently in the feed.
    rows = leads_view.bucket_report()
    seen = {r["bucket_type"] for r in rows}
    for bt, desc in sorted(pdb.subsource_map().items()):
        if bt not in seen:
            rows.append({"bucket_type": bt, "descriptor": desc, "triggered": 0, "sent": 0})
    return render_template("subsources.html", rows=rows, on_subsources=True)


@app.route("/subsource", methods=["POST"])
@require_login
def save_subsource():
    bucket = (request.form.get("bucket_type") or "").strip()
    descriptor = (request.form.get("descriptor") or "").strip()
    if not bucket:
        flash("Bucket type is required.", "error")
    elif descriptor:
        pdb.upsert_subsource(bucket, descriptor)
        flash(f"Saved descriptor for {bucket}.", "ok")
    else:
        pdb.delete_subsource(bucket)   # cleared descriptor -> revert to the raw code
        flash(f"Cleared descriptor for {bucket}.", "ok")
    return redirect(url_for("subsources"))


@app.route("/subsource/delete", methods=["POST"])
@require_login
def remove_subsource():
    bucket = (request.form.get("bucket_type") or "").strip()
    if bucket:
        pdb.delete_subsource(bucket)
        flash(f"Removed descriptor for {bucket}.", "ok")
    return redirect(url_for("subsources"))


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
    crm = (request.form.get("crm_type_id") or "").strip()
    data["crm_type_id"] = int(crm) if crm else None
    src = (request.form.get("lead_source_id") or "").strip()
    data["lead_source_id"] = int(src) if src else None
    for f in ("max_leads_per_day", "send_start_time", "send_end_time"):
        data[f] = (request.form.get(f) or "").strip() or None
    if not (data["dealer_id"] and data["dealer_name"] and data["lead_email_address"]):
        flash("DealerID, Dealer Name and Lead Email Address are required.", "error")
        return redirect(url_for("dealers_add"))
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
        g["embed"] = embed_code(g["product_code"], dealer_id)
    # Existing grant values keyed by product_code, so the "Add / Update Grant"
    # form can pre-fill when an already-granted product is selected (avoids
    # overwriting a field with a blank on save). Dates -> 'YYYY-MM-DD'.
    grant_form_data = {
        g["product_code"]: {
            "valid_from": str(g["valid_from"])[:10] if g.get("valid_from") else "",
            "valid_to": str(g["valid_to"])[:10] if g.get("valid_to") else "",
            "monthly_price": "" if g.get("monthly_price") is None else str(g["monthly_price"]),
            "per_lead_price": "" if g.get("per_lead_price") is None else str(g["per_lead_price"]),
            "max_leads_per_month": "" if g.get("max_leads_per_month") is None else str(g["max_leads_per_month"]),
            "max_leads_per_day": "" if g.get("max_leads_per_day") is None else str(g["max_leads_per_day"]),
        }
        for g in grants
    }
    # "Send a test lead" link — for dealers with the credit app (either grant).
    credit_base = product_url(pdb.PRODUCT_CREDIT_PIPELINE, dealer_id)
    has_credit = pdb.dealer_has_any_product(
        dealer_id, (pdb.PRODUCT_CREDIT_PIPELINE, pdb.PRODUCT_CREDIT_EST))
    credit_test_url = (credit_base + "/test-lead") if (has_credit and credit_base) else None
    return render_template(
        "dealer.html",
        dealer=d,
        states=US_STATES,
        products=pdb.list_products(),
        crm_types=pdb.list_crm_types(),
        lead_sources=pdb.list_lead_sources(),
        default_source=pdb.DEFAULT_LEAD_SOURCE,
        grants=grants,
        grant_form_data=grant_form_data,
        credit_test_url=credit_test_url,
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
    date_from = (request.args.get("start") or "").strip()[:10]
    date_to = (request.args.get("end") or "").strip()[:10]
    # When date-filtering, pull a wider window before filtering (created_at is a
    # 'YYYY-MM-DD HH:MM:SS' string, so a lexical [:10] compare is a date compare).
    limit = 5000 if (date_from or date_to) else 300
    if product == "LEAD_FORM":
        rows = leads_view.lead_form_leads(limit=limit)
    elif product == "TRADE_IN":
        rows = leads_view.trade_leads(limit=limit)
    elif product == "CREDIT_EST":
        rows = leads_view.credit_leads(limit=limit)
    else:
        rows = leads_view.all_leads(limit=limit)
    if date_from:
        rows = [r for r in rows if (r.get("created_at") or "")[:10] >= date_from]
    if date_to:
        rows = [r for r in rows if (r.get("created_at") or "")[:10] <= date_to]
    return render_template("leads.html", leads=rows, product=product,
                           date_from=date_from, date_to=date_to,
                           dealers={d["dealer_id"]: d["dealer_name"] for d in pdb.list_dealers()})


@app.route("/leads/volume")
@require_login
def lead_volume():
    return render_template("volume.html", rows=leads_view.pipeline_volume())


@app.route("/leads/volume/<dealer_id>/<period>")
@require_login
def dealer_leads(dealer_id, period):
    dealer = pdb.get_dealer(dealer_id)
    return render_template("dealer_leads.html", dealer_id=dealer_id,
                           dealer_name=(dealer or {}).get("dealer_name") or dealer_id,
                           period=period,
                           period_label=leads_view.PERIOD_LABEL.get(period, period),
                           rows=leads_view.dealer_sent_leads(dealer_id, period),
                           on_volume=True)


@app.route("/leads/funnel")
@require_login
def trigger_funnel():
    # Fast shell — the slow funnel (vehicle enrichment) loads async with a spinner.
    window = request.args.get("window", "month")
    if window not in leads_view.FUNNEL_WINDOWS:
        window = "month"
    return render_template("trigger_funnel.html", window=window)


@app.route("/leads/funnel/data")
@require_login
def trigger_funnel_data():
    window = request.args.get("window", "month")
    if window not in leads_view.FUNNEL_WINDOWS:
        window = "month"
    days = leads_view.pipeline_by_day(window)
    html = render_template("_funnel_body.html",
                           funnel=leads_view.pipeline_funnel(window),
                           window=window,
                           chart_svg=leads_view.by_day_chart_svg(days))
    return jsonify(html=html)


@app.route("/leads/buckets")
@require_login
def lead_buckets():
    return render_template("buckets.html",
                           buckets=leads_view.bucket_report(), on_buckets=True)


@app.route("/leads/buckets/<bucket_type>")
@require_login
def bucket_leads(bucket_type):
    return render_template("bucket_leads.html",
                           bucket_type=bucket_type,
                           descriptor=pdb.subsource_map().get(bucket_type, ""),
                           rows=leads_view.bucket_leads(bucket_type), on_buckets=True)


def _trigger_filters():
    """(f_customer, f_dealer, f_phone, f_sent) from request.args. "applied" marks a
    real form submission; on a fresh visit (no applied) the phone filter defaults
    ON, but once applied we respect an unchecked box."""
    applied = request.args.get("applied") == "1"
    f_customer = request.args.get("customer") == "1"
    f_dealer = request.args.get("dealer") == "1"
    f_phone = (request.args.get("phone") == "1") if applied else True
    f_cp = (request.args.get("cp_setup") == "1") if applied else True
    f_sent = request.args.get("sent", "unsent")
    if f_sent not in ("unsent", "sent", "all"):
        f_sent = "unsent"
    return f_customer, f_dealer, f_phone, f_cp, f_sent


@app.route("/trigger-leads")
@require_login
def trigger_leads():
    # Fast shell — the slow results table and "available to send" count load async
    # (with a spinner) from /trigger-leads/data so the page paints immediately.
    f_customer, f_dealer, f_phone, f_cp, f_sent = _trigger_filters()
    return render_template("trigger_leads.html",
                           f_customer=f_customer, f_dealer=f_dealer, f_phone=f_phone,
                           f_cp=f_cp, f_sent=f_sent,
                           pipeline_flow=pdb.get_pipeline_flow(), can_send=_CP_SEND_OK,
                           sent_today=pdb.sent_today_total(),
                           no_phone_today=pdb.no_phone_today())


@app.route("/trigger-leads/data")
@require_login
def trigger_leads_data():
    """The slow half of the Trigger Leads page (enriched rows + available count),
    returned as JSON for the async load."""
    f_customer, f_dealer, f_phone, f_cp, f_sent = _trigger_filters()
    rows = leads_view.trigger_leads(matching_customer=f_customer, matching_dealer=f_dealer,
                                    matching_phone=f_phone, cp_setup=f_cp, sent_status=f_sent)
    results_html = render_template("_trigger_rows.html", rows=rows, can_send=_CP_SEND_OK,
                                   f_customer=f_customer, f_dealer=f_dealer,
                                   f_phone=f_phone, f_cp=f_cp, f_sent=f_sent)
    return jsonify(available=leads_view.available_to_send_count(), results_html=results_html)


@app.route("/trigger-send", methods=["POST"])
@require_login
def trigger_send():
    """Send one Credit Pipeline lead for a match result_id (same process as the
    poller: build ADF -> email -> store in credit_leads -> record in `sent`)."""
    result_id = (request.form.get("result_id") or "").strip()
    keep = {k: request.form.get(k) for k in ("applied", "customer", "dealer", "phone", "sent")
            if request.form.get(k)}
    if not _CP_SEND_OK:
        flash("Send is unavailable — the credit send modules failed to load.", "error")
        return redirect(url_for("trigger_leads", **keep))
    row = _cp_source.fetch_one(result_id) if result_id else None
    if not row:
        flash(f"Result #{result_id} can't be sent (no matched dealer/customer, or already sent).", "error")
        return redirect(url_for("trigger_leads", **keep))
    ok, reason = _cp_eligible(row)
    if not ok:
        flash(f"Result #{result_id} not sent — {reason}.", "error")
        return redirect(url_for("trigger_leads", **keep))
    status, detail, lead_id = _cp_send_lead(row)
    if status in ("sent", "pending"):
        flash(f"Lead sent to {row.get('dealer_name')} (result #{result_id}, {status}).", "ok")
    elif status and status.startswith("skipped"):
        flash(f"Result #{result_id} not sent — {detail}.", "error")
    else:
        flash(f"Send failed for result #{result_id}: {status} — {detail}", "error")
    return redirect(url_for("trigger_leads", **keep))


@app.route("/trigger/<int:result_id>")
@require_login
def trigger_detail(result_id):
    """Customer/trigger detail for a CreditPipeline match result_id — the link
    target for a customer name whose trigger never became a sent lead."""
    detail = leads_view.trigger_detail(result_id)
    if not detail:
        abort(404)
    dealer = pdb.get_dealer(detail.get("dealer_code")) if detail.get("dealer_code") else None
    return render_template("trigger_detail.html", d=detail, dealer=dealer)


@app.route("/lead-flow/<int:result_id>/append", methods=["POST"])
@require_login
def lead_flow_append(result_id):
    """Run the imdatacenter phone+email append for one lead (steps 4 & 5), and
    surface the run's timestamp + result/status back on the flow page."""
    if not _CP_SEND_OK:
        flash("Append unavailable — credit modules failed to load.", "error")
        return redirect(url_for("lead_flow", result_id=result_id))
    from datetime import datetime as _dt
    import append_api as _aa
    run = {}
    try:
        sent = _cp_source.dlr.one(
            "SELECT TOP 1 result_id FROM dlrPro.dbo.[sent] WHERE result_id=%(r)s",
            {"r": result_id})
        if sent:                                      # sent leads can't be (re)appended
            flash(f"Lead #{result_id} was already sent — append is disabled.", "error")
            return redirect(url_for("lead_flow", result_id=result_id))
        existing = pdb.get_append(result_id)
        if existing:                                  # already appended — show the logged result
            ph = [p for p in (existing.get("all_phones") or "").split("|") if p]
            run = {"run_ts": str(existing.get("created") or "")[:19], "run_ok": "1", "run_code": "logged",
                   "run_phone": ph[0] if ph else "", "run_email": existing.get("email_appended") or ""}
        else:
            ls, db = _cp_source.LINKED_SERVER, _cp_source.DB
            r = _cp_source.dlr.one(
                "SELECT FirstName, LastName, Address1, City, State, ZipCode "
                "FROM [%s].[%s].[dbo].[vw_EquifaxConsumerRecordTriggers] WHERE result_id=%%(r)s" % (ls, db),
                {"r": result_id}) or {}
            res = _aa.append_ex(r.get("FirstName"), r.get("LastName"), r.get("Address1"),
                                r.get("City"), r.get("State"), r.get("ZipCode"))
            ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
            if res["ok"]:
                pdb.record_append(result_id, r.get("FirstName"), r.get("LastName"), r.get("Address1"),
                                  r.get("City"), r.get("State"), r.get("ZipCode"),
                                  res["email"], res["phones"], res.get("status"))
                pdb.resume_append_api()
                run = {"run_ts": ts, "run_ok": "1", "run_code": str(res["status_code"] or ""),
                       "run_phone": (res["phones"][0] if res["phones"] else ""),
                       "run_email": res["email"] or ""}
            else:
                pdb.pause_append_api()
                run = {"run_ts": ts, "run_ok": "0", "run_code": str(res["status_code"] or "error"),
                       "run_err": (res["error"] or "")[:140]}
    except Exception as e:
        run = {"run_ts": "", "run_ok": "0", "run_code": "exception", "run_err": str(e)[:140]}
    return redirect(url_for("lead_flow", result_id=result_id, **run))


@app.route("/lead-flow/<int:result_id>/reject", methods=["POST"])
@require_login
def lead_flow_reject(result_id):
    """Reject (abandon) one lead so the poller won't send it (step 10)."""
    try:
        sent = _cp_source.dlr.one(
            "SELECT TOP 1 result_id FROM dlrPro.dbo.[sent] WHERE result_id=%(r)s",
            {"r": result_id})
        if sent:                                      # a sent lead can't be rejected
            flash(f"Lead #{result_id} was already sent — it can't be rejected.", "error")
            return redirect(url_for("lead_flow", result_id=result_id))
        _cp_source.reject_result(result_id)
        flash(f"Lead #{result_id} rejected.", "ok")
    except Exception as e:
        flash("Reject failed: %s" % str(e)[:140], "error")
    return redirect(url_for("lead_flow", result_id=result_id))


@app.route("/lead-flow/<int:result_id>/send", methods=["POST"])
@require_login
def lead_flow_send(result_id):
    """Send one lead to its dealer (step 11) — same process as the poller."""
    if not _CP_SEND_OK:
        flash("Send unavailable — credit modules failed to load.", "error")
        return redirect(url_for("lead_flow", result_id=result_id))
    row = _cp_source.fetch_one(result_id)
    if not row:
        flash(f"Result #{result_id} can't be sent (no matched dealer, or already sent).", "error")
        return redirect(url_for("lead_flow", result_id=result_id))
    ok, reason = _cp_eligible(row)
    if not ok:
        flash(f"Not sent — {reason}.", "error")
        return redirect(url_for("lead_flow", result_id=result_id))
    status, detail, lead_id = _cp_send_lead(row)
    ok_send = status in ("sent", "pending")
    flash(f"Result #{result_id}: {status} — {detail}", "ok" if ok_send else "error")
    return redirect(url_for("lead_flow", result_id=result_id))


@app.route("/lead-flow/<int:result_id>")
@require_login
def lead_flow(result_id):
    """Pipeline flowchart for one Credit Pipeline lead — where it sits in the
    process (received → matched → enriched → cleared → sent), colour-coded."""
    flow = leads_view.lead_flow(result_id)
    if not flow:
        abort(404)
    run = {k: request.args.get(k) for k in
           ("run_ts", "run_ok", "run_code", "run_phone", "run_email", "run_err")
           if request.args.get(k)}
    return render_template("lead_flow.html", flow=flow, run=run)


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
        "max_leads_per_month": (request.form.get("max_leads_per_month") or "").strip(),
        "max_leads_per_day": (request.form.get("max_leads_per_day") or "").strip(),
    }
    if not (data["dealer_id"] and data["product_code"]):
        flash("Dealer and product are required.", "error")
    else:
        pdb.upsert_grant(data)
        flash(f"Saved {data['product_code']} grant.", "ok")
    return redirect(url_for("dealer", dealer_id=dealer_id))


@app.route("/grant/sync-daily", methods=["POST"])
@require_login
def sync_grant_daily():
    """'Sync Dealer Maximum per Day' — copy the dealer's max/day onto one grant."""
    dealer_id = request.form.get("dealer_id")
    product_code = request.form.get("product_code")
    if not (dealer_id and product_code):
        abort(400)
    val = pdb.sync_grant_daily_max(dealer_id, product_code)
    if val is None:
        flash(f"{product_code}: dealer has no Max Leads / Day set — grant max/day cleared.", "ok")
    else:
        flash(f"{product_code}: max/day synced to the dealer maximum ({val}).", "ok")
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
