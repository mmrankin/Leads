"""Read auction-scraper run status for the admin Scraper Status page.

Each scraper (scrapers/<site>/) writes status.json after a run; this reads those,
adds a couple of filesystem signals (login session present), and returns a
per-site health summary. Sites with no folder yet are simply omitted.
"""
import json
import pathlib
from datetime import datetime

SCRAPERS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scrapers"
SITES = ["manheim", "adesa", "copart"]
STALE_HOURS = 24   # a last run older than this is flagged stale


def _age_seconds(iso):
    if not iso:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    except (ValueError, TypeError):
        return None


def _fmt_age(secs):
    if secs is None:
        return "—"
    secs = int(secs)
    if secs < 90:
        return "%ds ago" % secs
    if secs < 5400:
        return "%dm ago" % (secs // 60)
    if secs < 172800:
        return "%dh ago" % (secs // 3600)
    return "%dd ago" % (secs // 86400)


def site_status(site):
    d = SCRAPERS_DIR / site
    st = {"site": site, "configured": d.exists(), "session": (d / "profile").exists(),
          "status": None, "last_age_str": "—", "stale": False, "health": "never"}
    sf = d / "status.json"
    if sf.exists():
        try:
            data = json.loads(sf.read_text())
        except Exception:
            data = None
        st["status"] = data
        if data:
            secs = _age_seconds(data.get("written_at") or data.get("run_end"))
            st["last_age_str"] = _fmt_age(secs)
            st["stale"] = secs is not None and secs > STALE_HOURS * 3600
            if not data.get("logged_in", True):
                st["health"] = "logged_out"
            elif data.get("mode") == "login":
                st["health"] = "session_ready"
            elif data.get("failed"):
                st["health"] = "partial"
            elif data.get("downloaded"):
                st["health"] = "ok"
            else:
                st["health"] = "unknown"
    return st


def all_status():
    return [site_status(s) for s in SITES if (SCRAPERS_DIR / s).exists()]
