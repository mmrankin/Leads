"""Shared platform data access: dealers + product access grants + settings.

Backed by the SQL Server `dlrPro` database (see dlrpro_db.py). Imported by both
the dealer-leads and trade-in/credit apps (each adds this directory to sys.path).
Public function names/signatures are unchanged from the old SQLite version.
"""

from datetime import date, datetime, timedelta

import dlrpro_db as dlr
from dlrpro_db import NOW

# Canonical product codes.
PRODUCT_LEAD_FORM = "LEAD_FORM"
PRODUCT_TRADE_IN = "TRADE_IN"
PRODUCT_CREDIT_EST = "CREDIT_EST"
PRODUCT_CREDIT_PIPELINE = "CREDIT_PIPELINE"
# CDP (cdp.dlrpro.com) modules — granted per dealer here, enforced by the CDP app.
PRODUCT_CDP_VMS = "VMS"
PRODUCT_CDP_CRM = "CDP"
PRODUCT_CDP_PROSPECTING = "PROSPECTING"

# The 4th tuple element is the product's default ADF "source" — the primary
# (sequence=1) source name emitted on every outbound ADF/XML <id> for leads of
# this product. It is admin-editable per product (Products page); init_db only
# seeds it and backfills NULLs, so edits persist across restarts.
DEFAULT_ADF_SOURCE = "RMA Data Plus"
DEFAULT_PRODUCTS = [
    (PRODUCT_LEAD_FORM, "Dealer Lead Form", "Customer lead-capture form with ADF/XML delivery.", DEFAULT_ADF_SOURCE),
    (PRODUCT_TRADE_IN, "Trade-In Widget", "Multi-step trade-in appraisal capture with ADF/XML delivery.", DEFAULT_ADF_SOURCE),
    (PRODUCT_CREDIT_EST, "Credit Estimator", "Lead form + FICO-range estimate, APR/payment, and affordability.", DEFAULT_ADF_SOURCE),
    (PRODUCT_CREDIT_PIPELINE, "Credit Pipeline", "Credit lead pipeline with ADF/XML delivery and source lineage.", "Credit Pipeline"),
    (PRODUCT_CDP_VMS, "CDP · Vehicle Management", "Appraisal, VIN normalization, valuation, photos & inventory.", DEFAULT_ADF_SOURCE),
    (PRODUCT_CDP_CRM, "CDP · CRM", "Customer relationship management (coming soon).", DEFAULT_ADF_SOURCE),
    (PRODUCT_CDP_PROSPECTING, "CDP · Prospecting", "Data mining & opportunity lists (coming soon).", DEFAULT_ADF_SOURCE),
]


def init_db():
    """Ensure the default products exist (tables themselves are created by the
    one-time migrate_to_dlrpro.py). Idempotent."""
    _ensure_pipeline_tables()
    _ensure_crm_tables()
    _ensure_leadsource_tables()
    _ensure_subsource_tables()
    # Self-healing: the `source` column was added after the initial migration.
    dlr.execute("IF COL_LENGTH('products', 'source') IS NULL "
                "ALTER TABLE products ADD source NVARCHAR(200) NULL")
    # Per-grant monthly lead cap (Credit Pipeline); NULL -> DEFAULT_MAX_LEADS_PER_MONTH.
    dlr.execute("IF COL_LENGTH('dealer_products', 'max_leads_per_month') IS NULL "
                "ALTER TABLE dealer_products ADD max_leads_per_month INT NULL")
    # Pause a grant without removing it: paused dealers keep the product but get
    # no automated (or manual) sends, and show 'P' on CP reports.
    dlr.execute("IF COL_LENGTH('dealer_products', 'paused') IS NULL "
                "ALTER TABLE dealer_products ADD paused BIT NOT NULL DEFAULT 0")
    for code, name, desc, source in DEFAULT_PRODUCTS:
        p = {"c": code, "n": name, "d": desc, "s": source}
        if dlr.one("SELECT 1 AS x FROM products WHERE product_code=%(c)s", p):
            # Keep name/desc in sync with code, but never clobber an admin-edited
            # source — only backfill it when it's still NULL.
            dlr.execute("UPDATE products SET product_name=%(n)s, description=%(d)s, "
                        "source=COALESCE(source, %(s)s) WHERE product_code=%(c)s", p)
        else:
            dlr.execute("INSERT INTO products (product_code, product_name, description, source) "
                        "VALUES (%(c)s, %(n)s, %(d)s, %(s)s)", p)


# ----- CRM types -----
#
# Master list of dealer CRM systems; a dealer references one via dealers.crm_type_id.

def _ensure_crm_tables():
    """Create the crm_types table + the dealers.crm_type_id column if missing."""
    dlr.execute(
        "IF OBJECT_ID(N'dbo.crm_types','U') IS NULL "
        "CREATE TABLE dbo.crm_types ("
        " id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,"
        " name NVARCHAR(120) NOT NULL,"
        " created_at NVARCHAR(32) NULL,"
        " CONSTRAINT UQ_crm_types_name UNIQUE (name))")
    dlr.execute(
        "IF COL_LENGTH('dbo.dealers','crm_type_id') IS NULL "
        "ALTER TABLE dbo.dealers ADD crm_type_id INT NULL")


def list_crm_types():
    return dlr.query("SELECT * FROM crm_types ORDER BY name")


def get_crm_type(crm_id):
    return dlr.one("SELECT * FROM crm_types WHERE id=%(i)s", {"i": crm_id})


def add_crm_type(name):
    dlr.execute(f"INSERT INTO crm_types (name, created_at) VALUES (%(n)s, {NOW})",
                {"n": name})


def update_crm_type(crm_id, name):
    dlr.execute("UPDATE crm_types SET name=%(n)s WHERE id=%(i)s",
                {"n": name, "i": crm_id})


def delete_crm_type(crm_id):
    dlr.execute("DELETE FROM crm_types WHERE id=%(i)s", {"i": crm_id})


def crm_name_for(dealer):
    """CRM system name for a dealer dict (resolves crm_type_id), or None.
    Never raises — returns None on any error (used during ADF generation)."""
    try:
        cid = (dealer or {}).get("crm_type_id")
        if not cid:
            return None
        row = get_crm_type(cid)
        return row["name"] if row else None
    except Exception:
        return None


# ----- lead sources -----
#
# Master list of lead sources; a dealer references one via dealers.lead_source_id.
# The dealer's lead source is the primary (sequence 1) ADF <id> source. Dealers
# with no selection fall back to DEFAULT_LEAD_SOURCE.

DEFAULT_LEAD_SOURCE = "Credit Pipeline"
DEFAULT_LEAD_SOURCES = ("Credit Pipeline", "G4 Media")


def _ensure_leadsource_tables():
    """Create lead_sources + the dealers.lead_source_id column, and seed the
    default sources. Idempotent."""
    dlr.execute(
        "IF OBJECT_ID(N'dbo.lead_sources','U') IS NULL "
        "CREATE TABLE dbo.lead_sources ("
        " id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,"
        " name NVARCHAR(120) NOT NULL,"
        " created_at NVARCHAR(32) NULL,"
        " CONSTRAINT UQ_lead_sources_name UNIQUE (name))")
    dlr.execute(
        "IF COL_LENGTH('dbo.dealers','lead_source_id') IS NULL "
        "ALTER TABLE dbo.dealers ADD lead_source_id INT NULL")
    for name in DEFAULT_LEAD_SOURCES:
        dlr.execute(
            "IF NOT EXISTS (SELECT 1 FROM lead_sources WHERE name=%(n)s) "
            f"INSERT INTO lead_sources (name, created_at) VALUES (%(n)s, {NOW})",
            {"n": name})


def list_lead_sources():
    return dlr.query("SELECT * FROM lead_sources ORDER BY name")


def get_lead_source(source_id):
    return dlr.one("SELECT * FROM lead_sources WHERE id=%(i)s", {"i": source_id})


def add_lead_source(name):
    dlr.execute(f"INSERT INTO lead_sources (name, created_at) VALUES (%(n)s, {NOW})",
                {"n": name})


def update_lead_source(source_id, name):
    dlr.execute("UPDATE lead_sources SET name=%(n)s WHERE id=%(i)s",
                {"n": name, "i": source_id})


def delete_lead_source(source_id):
    dlr.execute("DELETE FROM lead_sources WHERE id=%(i)s", {"i": source_id})


def lead_source_for(dealer):
    """Primary ADF source for a dealer (resolves lead_source_id), defaulting to
    Credit Pipeline. Never raises (used during ADF generation)."""
    try:
        sid = (dealer or {}).get("lead_source_id")
        if sid:
            row = get_lead_source(sid)
            if row and row.get("name"):
                return row["name"]
    except Exception:
        pass
    return DEFAULT_LEAD_SOURCE


# ----- Credit Pipeline sub-sources (bucketType -> descriptor) -----
#
# A trigger's secondary source is its `bucketType` in the Equifax trigger view;
# this table maps each bucketType to an admin-editable human descriptor (the
# Subsources admin page). Unmapped bucketTypes fall back to the raw code.

def _ensure_subsource_tables():
    dlr.execute(
        "IF OBJECT_ID(N'dbo.pipeline_subsource','U') IS NULL "
        "CREATE TABLE dbo.pipeline_subsource ("
        " id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,"
        " bucket_type NVARCHAR(100) NOT NULL,"
        " descriptor NVARCHAR(200) NOT NULL,"
        " created_at NVARCHAR(32) NULL,"
        " CONSTRAINT UQ_pipeline_subsource UNIQUE (bucket_type))")
    # Seed the first known bucketType.
    dlr.execute(
        "IF NOT EXISTS (SELECT 1 FROM pipeline_subsource WHERE bucket_type=%(b)s) "
        f"INSERT INTO pipeline_subsource (bucket_type, descriptor, created_at) "
        f"VALUES (%(b)s, %(d)s, {NOW})",
        {"b": "CONQUEST_PROP", "d": "Conquest Soft Pull"})


def list_subsources():
    return dlr.query("SELECT * FROM pipeline_subsource ORDER BY bucket_type")


def subsource_map():
    """{bucket_type: descriptor} used to label trigger buckets / lead sub-sources."""
    return {r["bucket_type"]: r["descriptor"]
            for r in dlr.query("SELECT bucket_type, descriptor FROM pipeline_subsource")}


def upsert_subsource(bucket_type, descriptor):
    """Set the descriptor for a bucketType (insert or update by bucket_type)."""
    dlr.execute(
        "IF EXISTS (SELECT 1 FROM pipeline_subsource WHERE bucket_type=%(b)s) "
        "UPDATE pipeline_subsource SET descriptor=%(d)s WHERE bucket_type=%(b)s "
        "ELSE INSERT INTO pipeline_subsource (bucket_type, descriptor, created_at) "
        f"VALUES (%(b)s, %(d)s, {NOW})", {"b": bucket_type, "d": descriptor})


def delete_subsource(bucket_type):
    dlr.execute("DELETE FROM pipeline_subsource WHERE bucket_type=%(b)s", {"b": bucket_type})


# ----- dealers -----

def get_dealer(dealer_id):
    return dlr.one("SELECT * FROM dealers WHERE dealer_id=%(d)s", {"d": dealer_id})


def list_dealers():
    return dlr.query("SELECT * FROM dealers ORDER BY dealer_name")


def upsert_dealer(data):
    fields = ("dealer_id", "dealer_name", "address", "city", "state",
              "zip", "phone", "lead_email_address", "banner_url",
              "crm_type_id", "lead_source_id")
    v = {k: (data.get(k) if data.get(k) not in ("", None) else None) for k in fields}
    if dlr.one("SELECT 1 AS x FROM dealers WHERE dealer_id=%(dealer_id)s", v):
        dlr.execute(
            "UPDATE dealers SET dealer_name=%(dealer_name)s, address=%(address)s, "
            "city=%(city)s, state=%(state)s, zip=%(zip)s, phone=%(phone)s, "
            "lead_email_address=%(lead_email_address)s, banner_url=%(banner_url)s, "
            "crm_type_id=%(crm_type_id)s, lead_source_id=%(lead_source_id)s, "
            f"updated_at={NOW} WHERE dealer_id=%(dealer_id)s", v)
    else:
        dlr.execute(
            "INSERT INTO dealers (dealer_id, dealer_name, address, city, state, zip, "
            "phone, lead_email_address, banner_url, crm_type_id, lead_source_id) "
            "VALUES (%(dealer_id)s, %(dealer_name)s, %(address)s, %(city)s, %(state)s, "
            "%(zip)s, %(phone)s, %(lead_email_address)s, %(banner_url)s, "
            "%(crm_type_id)s, %(lead_source_id)s)", v)


# ----- products -----

def list_products():
    return dlr.query("SELECT * FROM products ORDER BY product_name")


def get_product(product_code):
    """One product row (incl. its ADF `source`), or None."""
    return dlr.one("SELECT * FROM products WHERE product_code=%(c)s", {"c": product_code})


def update_product_source(product_code, source):
    """Set a product's ADF primary source (sequence=1 source on outbound <id>)."""
    dlr.execute("UPDATE products SET source=%(s)s WHERE product_code=%(c)s",
                {"s": (source or None), "c": product_code})


# ----- grants -----

def list_grants(dealer_id=None):
    if dealer_id:
        return dlr.query("SELECT * FROM dealer_products WHERE dealer_id=%(d)s "
                         "ORDER BY product_code", {"d": dealer_id})
    return dlr.query("SELECT * FROM dealer_products ORDER BY dealer_id, product_code")


def upsert_grant(data):
    fields = ("dealer_id", "product_code", "valid_from", "valid_to",
              "monthly_price", "per_lead_price", "max_leads_per_month")
    v = {k: (data.get(k) if data.get(k) not in ("",) else None) for k in fields}
    if dlr.one("SELECT 1 AS x FROM dealer_products WHERE dealer_id=%(dealer_id)s "
               "AND product_code=%(product_code)s", v):
        dlr.execute(
            "UPDATE dealer_products SET valid_from=%(valid_from)s, valid_to=%(valid_to)s, "
            "monthly_price=%(monthly_price)s, per_lead_price=%(per_lead_price)s, "
            "max_leads_per_month=%(max_leads_per_month)s, "
            f"updated_at={NOW} WHERE dealer_id=%(dealer_id)s AND product_code=%(product_code)s", v)
    else:
        dlr.execute(
            "INSERT INTO dealer_products (dealer_id, product_code, valid_from, valid_to, "
            "monthly_price, per_lead_price, max_leads_per_month) VALUES (%(dealer_id)s, "
            "%(product_code)s, %(valid_from)s, %(valid_to)s, %(monthly_price)s, "
            "%(per_lead_price)s, %(max_leads_per_month)s)", v)


def delete_grant(grant_id):
    dlr.execute("DELETE FROM dealer_products WHERE id=%(id)s", {"id": grant_id})


def get_active_grant(dealer_id, product_code, on_date=None):
    """Grant row if active on the date (default today), else None. NULL bounds =
    unbounded; ISO 'YYYY-MM-DD' strings compare correctly."""
    today = on_date or date.today().isoformat()
    return dlr.one(
        "SELECT * FROM dealer_products WHERE dealer_id=%(d)s AND product_code=%(p)s "
        "AND (valid_from IS NULL OR valid_from<=%(t)s) "
        "AND (valid_to IS NULL OR valid_to>=%(t)s)",
        {"d": dealer_id, "p": product_code, "t": today})


def dealer_has_product(dealer_id, product_code, on_date=None):
    return get_active_grant(dealer_id, product_code, on_date) is not None


def dealer_has_any_product(dealer_id, product_codes, on_date=None):
    """True if the dealer has an active grant for ANY of the given product codes.
    Used where one app serves more than one product (e.g. the credit app serves
    both Credit Estimator and Credit Pipeline)."""
    return any(dealer_has_product(dealer_id, c, on_date) for c in product_codes)


def count_active_grants(product_code, on_date=None):
    """Number of dealers with an active grant for the product (default today)."""
    today = on_date or date.today().isoformat()
    row = dlr.one(
        "SELECT COUNT(DISTINCT dealer_id) AS c FROM dealer_products "
        "WHERE product_code=%(p)s "
        "AND (valid_from IS NULL OR valid_from<=%(t)s) "
        "AND (valid_to IS NULL OR valid_to>=%(t)s)",
        {"p": product_code, "t": today})
    return row["c"] if row else 0


def active_grants_by_dealer(on_date=None):
    """{dealer_id: set(product_code)} of grants active on the date (default today).
    One query — used to render per-dealer product columns on the dealers list."""
    today = on_date or date.today().isoformat()
    rows = dlr.query(
        "SELECT dealer_id, product_code FROM dealer_products "
        "WHERE (valid_from IS NULL OR valid_from<=%(t)s) "
        "AND (valid_to IS NULL OR valid_to>=%(t)s)", {"t": today})
    out = {}
    for r in rows:
        out.setdefault(r["dealer_id"], set()).add(r["product_code"])
    return out


# ----- valuation settings -----

CONDITION_ADJ_FIELDS = (
    "adj_keys_1", "adj_keys_3plus", "adj_unrepaired_damage", "adj_engine_light",
    "adj_airbag_light", "adj_brake_light", "adj_aftermarket_exhaust",
    "adj_aftermarket_engine", "adj_aftermarket_stereo",
)

RECOMMENDED = {
    "dollar": {
        "range_spread": 500, "mileage_rate": 0.12,
        "adj_keys_1": -250, "adj_keys_3plus": 0, "adj_unrepaired_damage": -1000,
        "adj_engine_light": -1500, "adj_airbag_light": -600, "adj_brake_light": -400,
        "adj_aftermarket_exhaust": -300, "adj_aftermarket_engine": -750,
        "adj_aftermarket_stereo": -200,
    },
    "percent": {
        "range_spread": 3, "mileage_rate": 0.12,
        "adj_keys_1": -1, "adj_keys_3plus": 0, "adj_unrepaired_damage": -5,
        "adj_engine_light": -7, "adj_airbag_light": -3, "adj_brake_light": -2,
        "adj_aftermarket_exhaust": -2, "adj_aftermarket_engine": -4,
        "adj_aftermarket_stereo": -1.5,
    },
}

ALL_SETTING_FIELDS = ("base_source", "adjustment_unit", "range_spread",
                      "mileage_rate") + CONDITION_ADJ_FIELDS


def recommended_settings(unit):
    return dict(RECOMMENDED.get(unit, RECOMMENDED["dollar"]))


def get_valuation_settings(dealer_id):
    row = dlr.one("SELECT * FROM dealer_valuation_settings WHERE dealer_id=%(d)s",
                  {"d": dealer_id})
    if row:
        return row
    defaults = {"dealer_id": dealer_id, "base_source": "retail",
                "adjustment_unit": "dollar"}
    defaults.update(RECOMMENDED["dollar"])
    return defaults


def _upsert_settings(table, fields, data):
    v = {"dealer_id": data["dealer_id"]}
    for f in fields:
        v[f] = data.get(f)
    if dlr.one(f"SELECT 1 AS x FROM {table} WHERE dealer_id=%(dealer_id)s", v):
        sets = ", ".join(f"{f}=%({f})s" for f in fields)
        dlr.execute(f"UPDATE {table} SET {sets}, updated_at={NOW} "
                    "WHERE dealer_id=%(dealer_id)s", v)
    else:
        cols = ", ".join(("dealer_id",) + tuple(fields))
        ph = ", ".join(f"%({c})s" for c in ("dealer_id",) + tuple(fields))
        dlr.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", v)


def upsert_valuation_settings(data):
    _upsert_settings("dealer_valuation_settings", ALL_SETTING_FIELDS, data)


# ----- credit estimator settings -----

CREDIT_APR_FIELDS = (
    "apr_exceptional", "apr_very_good", "apr_good", "apr_fair", "apr_poor",
)
CREDIT_SETTING_FIELDS = CREDIT_APR_FIELDS + (
    "new_apr_delta", "apr_spread", "max_term_months", "max_payment_pct",
)
RECOMMENDED_CREDIT = {
    "apr_exceptional": 6.49, "apr_very_good": 7.49, "apr_good": 9.99,
    "apr_fair": 14.49, "apr_poor": 19.99,
    "new_apr_delta": -1.0, "apr_spread": 1.0,
    "max_term_months": 72, "max_payment_pct": 15.0,
}


def recommended_credit_settings():
    return dict(RECOMMENDED_CREDIT)


def get_credit_settings(dealer_id):
    row = dlr.one("SELECT * FROM dealer_credit_settings WHERE dealer_id=%(d)s",
                  {"d": dealer_id})
    if row:
        return row
    defaults = {"dealer_id": dealer_id}
    defaults.update(RECOMMENDED_CREDIT)
    return defaults


def upsert_credit_settings(data):
    _upsert_settings("dealer_credit_settings", CREDIT_SETTING_FIELDS, data)


# ----- platform settings + Credit Pipeline lead flow -----
#
# platform_settings (key/value) holds the global Credit Pipeline on/off switch.
# The `sent` table is the send-once ledger — one row per (dealer, match result):
# id, result_id, dealer_id (= dealers.id), created. Kept lean; it grows large.

def _ensure_pipeline_tables():
    """Create the settings table + the `sent` ledger if missing. Idempotent."""
    dlr.execute(
        "IF OBJECT_ID(N'dbo.platform_settings','U') IS NULL "
        "CREATE TABLE dbo.platform_settings ("
        " setting_key NVARCHAR(64) NOT NULL PRIMARY KEY,"
        " setting_value NVARCHAR(MAX) NULL,"
        " updated_at NVARCHAR(32) NULL)")
    dlr.execute(
        "IF OBJECT_ID(N'dbo.sent','U') IS NULL "
        "CREATE TABLE dbo.sent ("
        " id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,"
        " result_id BIGINT NULL,"
        " dealer_id BIGINT NULL,"
        " created SMALLDATETIME NULL)")
    # No-contact retry counter (dealer, match result) -> attempts; capped so a
    # record with no phone/email is only retried a few times.
    dlr.execute(
        "IF OBJECT_ID(N'dbo.pipeline_skips','U') IS NULL "
        "CREATE TABLE dbo.pipeline_skips ("
        " dealer_id BIGINT NOT NULL,"
        " result_id BIGINT NOT NULL,"
        " attempts INT NOT NULL,"
        " last_at SMALLDATETIME NULL,"
        " CONSTRAINT PK_pipeline_skips PRIMARY KEY (dealer_id, result_id))")
    # Phone/email append API call log (imdatacenter fp+fe2) — one row per record,
    # for billing reconciliation. result_id is unique (called once per record).
    dlr.execute(
        "IF OBJECT_ID(N'dbo.credit_append_log','U') IS NULL "
        "CREATE TABLE dbo.credit_append_log ("
        " id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,"
        " result_id BIGINT NOT NULL,"
        " first_name NVARCHAR(100) NULL,"
        " last_name NVARCHAR(100) NULL,"
        " address NVARCHAR(200) NULL,"
        " city NVARCHAR(100) NULL,"
        " state NVARCHAR(20) NULL,"
        " zip VARCHAR(15) NULL,"
        " email_appended NVARCHAR(200) NULL,"
        " phone_appended VARCHAR(20) NULL,"
        " all_phones NVARCHAR(400) NULL,"
        " status NVARCHAR(40) NULL,"
        " created DATETIME NULL,"
        " CONSTRAINT UQ_credit_append_log_result UNIQUE (result_id))")


def get_setting(key, default=None):
    row = dlr.one("SELECT setting_value AS v FROM platform_settings WHERE setting_key=%(k)s",
                  {"k": key})
    return row["v"] if row else default


def set_setting(key, value):
    v = {"k": key, "v": value}
    if dlr.one("SELECT 1 AS x FROM platform_settings WHERE setting_key=%(k)s", v):
        dlr.execute(f"UPDATE platform_settings SET setting_value=%(v)s, updated_at={NOW} "
                    "WHERE setting_key=%(k)s", v)
    else:
        dlr.execute("INSERT INTO platform_settings (setting_key, setting_value, updated_at) "
                    f"VALUES (%(k)s, %(v)s, {NOW})", v)


PIPELINE_FLOW_KEY = "credit_pipeline_flow_enabled"


def get_pipeline_flow():
    """Global Credit Pipeline lead-flow switch (default OFF)."""
    return get_setting(PIPELINE_FLOW_KEY, "0") == "1"


def set_pipeline_flow(enabled):
    set_setting(PIPELINE_FLOW_KEY, "1" if enabled else "0")


# ----- Lead BCC list (admin-editable; used by the lead email sender) -----
LEAD_BCC_KEY = "lead_bcc"
DEFAULT_LEAD_BCC = "justinstull@rmadataplus.com,robbazaren@rmadataplus.com,mark@rmadataplus.com"


def get_lead_bcc():
    """BCC recipients for Credit Pipeline lead emails, as a list. Seeds the
    setting with the built-in default the first time it's read so the list is
    editable and the default addresses are 'in it'."""
    v = get_setting(LEAD_BCC_KEY)
    if v is None:
        v = DEFAULT_LEAD_BCC
        try:
            set_setting(LEAD_BCC_KEY, v)
        except Exception:
            pass
    return [e.strip() for e in (v or "").split(",") if e.strip()]


def set_lead_bcc(emails):
    """emails: list/tuple or comma-separated string."""
    if isinstance(emails, (list, tuple)):
        emails = ",".join(e.strip() for e in emails if e.strip())
    set_setting(LEAD_BCC_KEY, emails or "")


# ----- Send interval (per-dealer spacing) -----
PIPELINE_INTERVAL_KEY = "pipeline_interval_min"


def get_pipeline_interval():
    """Minutes the poller waits between automated sends to the SAME dealer
    (spacing), clamped 1–30. Default 5."""
    try:
        n = int(get_setting(PIPELINE_INTERVAL_KEY, "5"))
    except (TypeError, ValueError):
        n = 5
    return max(1, min(30, n))


def set_pipeline_interval(minutes):
    set_setting(PIPELINE_INTERVAL_KEY, str(max(1, min(30, int(minutes)))))


def dealer_last_sent_min(dealers_id):
    """Minutes since the last Credit Pipeline send to this dealer (dealers.id),
    or None if never — for the per-dealer send-spacing interval."""
    row = dlr.one("SELECT DATEDIFF(minute, MAX(created), GETDATE()) AS m "
                  "FROM dbo.sent WHERE dealer_id=%(d)s", {"d": int(dealers_id)})
    return int(row["m"]) if row and row.get("m") is not None else None


# ----- Pause a dealer's grant (keep the product, stop sends) -----
def set_dealer_paused(dealer_id, product_code, paused):
    dlr.execute(f"UPDATE dealer_products SET paused=%(p)s, updated_at={NOW} "
                "WHERE dealer_id=%(d)s AND product_code=%(c)s",
                {"p": 1 if paused else 0, "d": dealer_id, "c": product_code})


def dealer_cp_paused(dealer_id):
    """True if the dealer's Credit Pipeline grant is paused (grant kept, sends off)."""
    row = dlr.one("SELECT paused FROM dealer_products "
                  "WHERE dealer_id=%(d)s AND product_code=%(c)s",
                  {"d": dealer_id, "c": PRODUCT_CREDIT_PIPELINE})
    return bool(row and row.get("paused"))


def paused_by_dealer():
    """{dealer_id: set(product_code)} of PAUSED grants — renders 'P' on reports."""
    out = {}
    for r in dlr.query("SELECT dealer_id, product_code FROM dealer_products WHERE paused=1"):
        out.setdefault(r["dealer_id"], set()).add(r["product_code"])
    return out


# ----- Poller heartbeat (watchdog) -----
# The poller stamps this at the start of every run; the watchdog reloads the
# poller agent if it goes stale (unsent leads expire after ~4h, so a stalled
# scheduler silently loses them).
PIPELINE_HEARTBEAT_KEY = "pipeline_last_run"


def record_pipeline_heartbeat():
    set_setting(PIPELINE_HEARTBEAT_KEY, datetime.utcnow().isoformat())


def pipeline_heartbeat_age_minutes():
    """Minutes since the poller last ran, or None if it never has / is unparseable."""
    v = get_setting(PIPELINE_HEARTBEAT_KEY)
    if not v:
        return None
    try:
        return (datetime.utcnow() - datetime.fromisoformat(v)).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


# ----- Credit Pipeline sent ledger (dbo.sent) -----

def is_sent(dealers_id, result_id):
    """True if this (dealer, match result) is already in the sent ledger."""
    return dlr.one("SELECT TOP 1 id FROM dbo.sent WHERE dealer_id=%(d)s AND result_id=%(r)s",
                   {"d": int(dealers_id), "r": int(result_id)}) is not None


def record_sent(dealers_id, result_id):
    """Mark a trigger lead as sent — one row in dbo.sent (dealers.id, result_id,
    timestamp). Idempotent: won't duplicate an existing (dealer, result)."""
    dlr.execute(
        "IF NOT EXISTS (SELECT 1 FROM dbo.sent WHERE dealer_id=%(d)s AND result_id=%(r)s) "
        "INSERT INTO dbo.sent (result_id, dealer_id, created) VALUES (%(r)s, %(d)s, GETDATE())",
        {"d": int(dealers_id), "r": int(result_id)})


# ----- Credit Pipeline no-contact retry counter (dbo.pipeline_skips) -----

MAX_NO_CONTACT_ATTEMPTS = 3


def bump_no_contact(dealers_id, result_id):
    """Increment the no-contact attempt count for (dealer, match result); returns
    the new count. Once it reaches MAX_NO_CONTACT_ATTEMPTS the poller stops
    fetching the record (see pipeline_source)."""
    v = {"d": int(dealers_id), "r": int(result_id)}
    dlr.execute(
        "IF EXISTS (SELECT 1 FROM dbo.pipeline_skips WHERE dealer_id=%(d)s AND result_id=%(r)s) "
        "UPDATE dbo.pipeline_skips SET attempts = attempts + 1, last_at = GETDATE() "
        "WHERE dealer_id=%(d)s AND result_id=%(r)s "
        "ELSE INSERT INTO dbo.pipeline_skips (dealer_id, result_id, attempts, last_at) "
        "VALUES (%(d)s, %(r)s, 1, GETDATE())", v)
    row = dlr.one("SELECT attempts FROM dbo.pipeline_skips WHERE dealer_id=%(d)s AND result_id=%(r)s", v)
    return int(row["attempts"]) if row else 1


# ----- Phone/email append API log (dbo.credit_append_log) -----

def get_append(result_id):
    """The append-API log row for this match result_id, or None. Presence means
    the API was already called once for this record — don't call it again."""
    return dlr.one("SELECT * FROM dbo.credit_append_log WHERE result_id=%(r)s",
                   {"r": int(result_id)})


# Circuit breaker: when the append API fails (e.g. 429 throttling), pause calls
# for a cooldown so lead processing is never held up. Persisted in
# platform_settings so it holds across the poller's fresh-process runs.
APPEND_PAUSE_KEY = "append_api_paused_until"
APPEND_COOLDOWN_MIN = 15


def append_api_paused():
    """True while the append API is in its post-failure cooldown."""
    v = get_setting(APPEND_PAUSE_KEY)
    if not v:
        return False
    try:
        return datetime.fromisoformat(v) > datetime.utcnow()
    except (ValueError, TypeError):
        return False


def pause_append_api(minutes=APPEND_COOLDOWN_MIN):
    """Back off from the append API for `minutes` after a failure."""
    set_setting(APPEND_PAUSE_KEY, (datetime.utcnow() + timedelta(minutes=minutes)).isoformat())


def resume_append_api():
    """Clear the append-API cooldown (call after a successful append)."""
    if get_setting(APPEND_PAUSE_KEY):
        set_setting(APPEND_PAUSE_KEY, "")


def record_append(result_id, first_name, last_name, address, city, state, zip_code,
                  email, phones, status=None):
    """Log one append-API call (for billing reconciliation). `phones` is the list
    of returned phone strings; the first is the primary. Idempotent on result_id."""
    v = {"r": int(result_id), "f": first_name, "l": last_name, "a": address, "c": city,
         "s": state, "z": zip_code, "em": (email or None),
         "ph": (phones[0] if phones else None),
         "all": ("|".join(phones) if phones else None), "st": status}
    dlr.execute(
        "IF NOT EXISTS (SELECT 1 FROM dbo.credit_append_log WHERE result_id=%(r)s) "
        "INSERT INTO dbo.credit_append_log (result_id, first_name, last_name, address, city, "
        "state, zip, email_appended, phone_appended, all_phones, status, created) VALUES "
        "(%(r)s, %(f)s, %(l)s, %(a)s, %(c)s, %(s)s, %(z)s, %(em)s, %(ph)s, %(all)s, %(st)s, GETDATE())", v)


# ----- Credit Pipeline monthly lead cap -----

DEFAULT_MAX_LEADS_PER_MONTH = 10


def pipeline_max_leads(dealer_id, on_date=None):
    """Max Credit Pipeline leads/month for a dealer — the active CREDIT_PIPELINE
    grant's max_leads_per_month, or DEFAULT_MAX_LEADS_PER_MONTH when unset."""
    g = get_active_grant(dealer_id, PRODUCT_CREDIT_PIPELINE, on_date)
    if g and g.get("max_leads_per_month") is not None:
        try:
            return int(g["max_leads_per_month"])
        except (TypeError, ValueError):
            pass
    return DEFAULT_MAX_LEADS_PER_MONTH


def sent_this_month(dealers_id):
    """Count of Credit Pipeline leads recorded to a dealer (dealers.id) since the
    first of the current calendar month."""
    row = dlr.one(
        "SELECT COUNT_BIG(*) AS c FROM dbo.sent WHERE dealer_id=%(d)s "
        "AND created >= DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)",
        {"d": int(dealers_id)})
    return int(row["c"]) if row else 0


def sent_today(dealers_id):
    """Count of Credit Pipeline leads recorded to a dealer (dealers.id) since
    midnight today (server local time)."""
    row = dlr.one(
        "SELECT COUNT_BIG(*) AS c FROM dbo.sent WHERE dealer_id=%(d)s "
        "AND created >= CAST(GETDATE() AS date)",
        {"d": int(dealers_id)})
    return int(row["c"]) if row else 0


def sent_today_total():
    """Count of Credit Pipeline leads sent across all dealers since midnight today."""
    row = dlr.one(
        "SELECT COUNT_BIG(*) AS c FROM dbo.sent WHERE created >= CAST(GETDATE() AS date)")
    return int(row["c"]) if row else 0


def no_phone_today():
    """Count of leads the poller skipped for no phone today — one row per
    (dealer, result_id) in dbo.pipeline_skips whose last attempt was today."""
    row = dlr.one(
        "SELECT COUNT_BIG(*) AS c FROM dbo.pipeline_skips WHERE last_at >= CAST(GETDATE() AS date)")
    return int(row["c"]) if row else 0


def recently_sent(dealers_id, minutes):
    """True if this dealer (dealers.id) has a Credit Pipeline send within the last
    `minutes` minutes (by the sent ledger's `created`, DB clock) — used to space
    out the poller's automated sends."""
    row = dlr.one(
        "SELECT TOP 1 id FROM dbo.sent WHERE dealer_id=%(d)s "
        "AND created >= DATEADD(minute, %(m)s, GETDATE())",
        {"d": int(dealers_id), "m": -int(minutes)})
    return row is not None
