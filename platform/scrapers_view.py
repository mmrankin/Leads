"""Read auction-scraper run status for the admin Scraper Status page.

Each scraper (scrapers/<site>/) writes status.json after a run; this reads those,
adds a couple of filesystem signals (login session present), and returns a
per-site health summary. Sites with no folder yet are simply omitted.
"""
import json
import pathlib
from datetime import datetime
from ftplib import FTP

SCRAPERS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scrapers"
SITES = ["manheim", "adesa", "copart"]
STALE_HOURS = 24   # a last run older than this is flagged stale
FTP_TIMEOUT = 6    # seconds for the live FTP reachability probe


def _load_env(path):
    """Minimal KEY=VALUE reader for scrapers/.env (avoids a dotenv dependency in
    the admin venv)."""
    env = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def _annotate_ftp(sites, env):
    """Live FTP probe: one connection, then per-site check that the upload dir is
    reachable. Sets ftp_ok / ftp_msg / ftp_dir on each site."""
    host, user, pw = env.get("FTP_HOST"), env.get("FTP_USER"), env.get("FTP_PASS")
    try:
        port = int(env.get("FTP_PORT") or 21)
    except ValueError:
        port = 21
    ftp, conn_err = None, None
    if host and user and pw:
        try:
            ftp = FTP()
            ftp.connect(host, port, timeout=FTP_TIMEOUT)
            ftp.login(user, pw)
            ftp.set_pasv(True)
        except Exception as e:
            conn_err = "%s: %s" % (type(e).__name__, str(e)[:60])
            ftp = None
    else:
        conn_err = "FTP not configured"
    for s in sites:
        rdir = env.get(s["site"].upper() + "_REMOTE_DIR")
        s["ftp_dir"] = rdir
        if ftp is None:
            s["ftp_ok"], s["ftp_msg"] = False, conn_err
            continue
        try:
            if rdir:
                ftp.cwd(rdir)
            s["ftp_ok"] = True
            s["ftp_msg"] = "reachable" + (" · " + rdir if rdir else "")
        except Exception as e:
            s["ftp_ok"] = False
            s["ftp_msg"] = "dir unavailable: %s" % str(e)[:50]
    if ftp is not None:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass


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
    sites = [site_status(s) for s in SITES if (SCRAPERS_DIR / s).exists()]
    _annotate_ftp(sites, _load_env(SCRAPERS_DIR / ".env"))
    return sites
