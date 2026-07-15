"""Health snapshot of the automated Credit Pipeline send process.

Feeds the admin Status dashboard (/status) and is reused by the stall alerter.
Every metric is best-effort — a failing lookup degrades to None/[] rather than
breaking the page. The poller's liveness comes from its log's last 'Run complete'
timestamp (the poller is the parallel session's file, so we don't add a DB
heartbeat to it); log timestamps are the prod box's local time.
"""

import os
import re
from datetime import datetime, timedelta

import dlrpro_db as dlr
import platform_db as pdb

CP = "[{ls}].[{db}].[dbo]".format(
    ls=os.environ.get("CREDITPIPELINE_LINKED_SERVER", "10.1.4.8"),
    db=os.environ.get("CREDITPIPELINE_DB", "CreditPipeline"))

POLLER_LOG = os.environ.get(
    "PIPELINE_LOG", "/Users/markrankin/claude/deploy/pipeline.err.log")
STALL_MINUTES = int(os.environ.get("PIPELINE_STALL_MIN", "20"))

_RUN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Run complete")


def last_poller_run():
    """(datetime, minutes_ago) of the last 'Run complete' in the poller log, or
    (None, None). Reads only the tail of the log."""
    try:
        with open(POLLER_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            data = b""
            while size > 0 and b"Run complete" not in data:
                step = min(65536, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
    except OSError:
        return None, None
    ts = None
    for line in reversed(data.decode("utf-8", "replace").splitlines()):
        m = _RUN_RE.match(line)
        if m:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            break
    if ts is None:
        return None, None
    return ts, (datetime.now() - ts).total_seconds() / 60.0


def _q(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def send_health():
    """Dict of health metrics for the Status dashboard."""
    h = {"stall_threshold": STALL_MINUTES}

    # Poller liveness: prefer the DB heartbeat (stamped at each run start); fall
    # back to the log's last 'Run complete'.
    mins = _q(pdb.pipeline_heartbeat_age_minutes)
    if mins is None:
        last_run, mins = last_poller_run()
        h["last_run"] = last_run.strftime("%Y-%m-%d %H:%M") if last_run else None
    else:
        hb = _q(lambda: pdb.get_setting(pdb.PIPELINE_HEARTBEAT_KEY))
        h["last_run"] = (hb[:16].replace("T", " ") + " UTC") if hb else None
    h["mins_since_run"] = int(round(mins)) if mins is not None else None
    h["flow_on"] = bool(_q(pdb.get_pipeline_flow, False))
    h["interval_min"] = _q(pdb.get_pipeline_interval, 5)
    # Stalled = flow is ON but the poller hasn't checked in within the window.
    h["stalled"] = h["flow_on"] and (mins is None or mins > STALL_MINUTES)

    s = _q(lambda: dlr.one(
        "SELECT "
        "(SELECT COUNT(*) FROM dlrPro.dbo.[sent]) AS total, "
        "(SELECT COUNT(*) FROM dlrPro.dbo.[sent] WHERE CONVERT(date,created)=CONVERT(date,GETDATE())) AS today, "
        "(SELECT COUNT(*) FROM dlrPro.dbo.[sent] WHERE created>=DATEADD(hour,-1,GETDATE())) AS last_hour, "
        "(SELECT CONVERT(varchar(19),MAX(created),120) FROM dlrPro.dbo.[sent]) AS last_sent"), {}) or {}
    h["sent_total"] = s.get("total")
    h["sent_today"] = s.get("today")
    h["sent_last_hour"] = s.get("last_hour")
    h["last_sent"] = s.get("last_sent")

    b = _q(lambda: dlr.one(
        "SELECT "
        "SUM(CASE WHEN s.id IS NULL AND m.rejected=0 THEN 1 ELSE 0 END) AS backlog, "
        "MAX(CASE WHEN s.id IS NULL AND m.rejected=0 THEN DATEDIFF(minute,m.returned_at,GETUTCDATE()) END) AS oldest_min, "
        "SUM(CASE WHEN CONVERT(date,m.returned_at)=CONVERT(date,GETUTCDATE()) THEN 1 ELSE 0 END) AS feed_today "
        "FROM " + CP + ".[match_result] m "
        "LEFT JOIN dlrPro.dbo.[sent] s ON s.result_id=m.result_id"), {}) or {}
    h["backlog"] = b.get("backlog")
    h["backlog_oldest_min"] = b.get("oldest_min")
    h["feed_today"] = b.get("feed_today")

    a = _q(lambda: dlr.one(
        "SELECT COUNT(*) AS calls, "
        "SUM(CASE WHEN all_phones IS NOT NULL AND all_phones<>'' THEN 1 ELSE 0 END) AS with_phone, "
        "SUM(CASE WHEN email_appended IS NOT NULL AND email_appended<>'' THEN 1 ELSE 0 END) AS with_email "
        "FROM dlrPro.dbo.credit_append_log WHERE CONVERT(date,created)=CONVERT(date,GETDATE())"), {}) or {}
    h["append_calls_today"] = a.get("calls")
    h["append_phone_today"] = a.get("with_phone")
    h["append_email_today"] = a.get("with_email")
    # Sends per hour, last 24h (oldest -> newest), for the status chart.
    hr_rows = _q(lambda: dlr.query(
        "SELECT DATEADD(hour, DATEDIFF(hour,0,created),0) AS hr, COUNT(*) AS c "
        "FROM dbo.sent WHERE created >= DATEADD(hour,-24,GETDATE()) "
        "GROUP BY DATEADD(hour, DATEDIFF(hour,0,created),0)"), []) or []
    hr_map = {}
    for r in hr_rows:
        hh = r.get("hr")
        if hh is not None:
            hr_map[(hh.year, hh.month, hh.day, hh.hour)] = r.get("c") or 0
    cur_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    buckets, hmax = [], 0
    for i in range(23, -1, -1):
        b = cur_hour - timedelta(hours=i)
        cnt = hr_map.get((b.year, b.month, b.day, b.hour), 0)
        hmax = max(hmax, cnt)
        buckets.append({"label": b.strftime("%-I%p").lower(), "hour24": b.strftime("%H"),
                        "date": b.strftime("%Y-%m-%d"), "count": cnt})
    h["sent_by_hour"] = buckets
    h["sent_by_hour_max"] = hmax
    h["sent_24h"] = sum(b["count"] for b in buckets)

    h["append_paused"] = bool(_q(pdb.append_api_paused, False))
    h["append_configured"] = bool(os.environ.get("IMDC_API_KEY") and os.environ.get("IMDC_CLIENT_ID"))

    # SMS alerting config + last alert (written by the alerter into platform_settings)
    h["sms_configured"] = bool(os.environ.get("TWILIO_ACCOUNT_SID")
                               and os.environ.get("TWILIO_AUTH_TOKEN")
                               and os.environ.get("TWILIO_FROM")
                               and os.environ.get("STALL_ALERT_TO"))
    h["last_alert"] = _q(lambda: pdb.get_setting("stall_last_alert_at"))

    # Per-dealer freshness — granted (active) dealers, when each last received.
    h["dealers"] = _q(lambda: dlr.query(
        "SELECT d.dealer_id, d.dealer_name, "
        "CONVERT(varchar(19),MAX(s.created),120) AS last_received, "
        "DATEDIFF(minute,MAX(s.created),GETDATE()) AS mins_ago, "
        "SUM(CASE WHEN CONVERT(date,s.created)=CONVERT(date,GETDATE()) THEN 1 ELSE 0 END) AS today, "
        "dp.max_leads_per_month AS cap, dp.paused AS paused "
        "FROM dlrPro.dbo.dealer_products dp "
        "JOIN dlrPro.dbo.dealers d ON d.dealer_id=dp.dealer_id "
        "LEFT JOIN dlrPro.dbo.[sent] s ON s.dealer_id=d.id "
        "WHERE dp.product_code='CREDIT_PIPELINE' "
        "AND (dp.valid_from IS NULL OR dp.valid_from<=CONVERT(date,GETDATE())) "
        "AND (dp.valid_to IS NULL OR dp.valid_to>=CONVERT(date,GETDATE())) "
        "GROUP BY d.dealer_id, d.dealer_name, dp.max_leads_per_month, dp.paused "
        "ORDER BY d.dealer_name"), []) or []
    return h
