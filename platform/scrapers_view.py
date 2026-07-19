"""Read auction-scraper state for the admin Auction pages: enabled flag, live
run status, per-run history and effectiveness, plus a live FTP reachability probe.

Each scraper (scrapers/<site>/) writes status.json (last run) and appends a record
to history.jsonl per run; scrapers/control.json holds the per-site enabled flag;
a scrapers/<site>/.running lockfile marks a run in progress.
"""
import json
import pathlib
import time
from datetime import datetime
from ftplib import FTP

SCRAPERS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scrapers"
SITES = ["manheim", "adesa", "copart"]
STALE_HOURS = 50          # runs are every 48h; older than this is stale
FTP_TIMEOUT = 6           # seconds for the live FTP probe
RUNNING_MAX_AGE = 3 * 3600  # a .running lockfile older than this is treated as stale
HISTORY_LIMIT = 15        # runs shown in the detail table


def _load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def control():
    """{site: {'enabled': bool}} from control.json (default all enabled)."""
    return _load_json(SCRAPERS_DIR / "control.json", {})


def set_enabled(site, enabled):
    """Flip a site's enabled flag in control.json. Returns the new value."""
    path = SCRAPERS_DIR / "control.json"
    data = _load_json(path, {})
    data.setdefault(site, {})["enabled"] = bool(enabled)
    path.write_text(json.dumps(data, indent=2))
    return bool(enabled)


def _read_history(site_dir, limit=HISTORY_LIMIT):
    """Newest-first list of past scrape-run records from history.jsonl."""
    path = site_dir / "history.jsonl"
    if not path.exists():
        return []
    runs = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("mode") == "scrape":   # login records aren't runs
                runs.append(rec)
    except Exception:
        return []
    runs.reverse()
    return runs[:limit]


def _effectiveness(runs):
    """Summary stats over the run records (already newest-first)."""
    scrapes = [r for r in runs]
    n = len(scrapes)
    if not n:
        return {"runs": 0, "ok_runs": 0, "success_rate": None,
                "avg_downloaded": None, "avg_failed": None, "total_uploaded": 0}
    ok = sum(1 for r in scrapes if not r.get("failed") and (r.get("downloaded") or 0) > 0)
    return {
        "runs": n,
        "ok_runs": ok,
        "success_rate": round(100 * ok / n),
        "avg_downloaded": round(sum((r.get("downloaded") or 0) for r in scrapes) / n, 1),
        "avg_failed": round(sum((r.get("failed") or 0) for r in scrapes) / n, 1),
        "total_uploaded": sum((r.get("uploaded") or 0) for r in scrapes),
    }


def _per_url(runs):
    """Per-URL ok/fail tallies across the run history, worst first."""
    tally = {}
    for r in runs:
        for res in (r.get("results") or []):
            u = res.get("url")
            if not u:
                continue
            t = tally.setdefault(u, {"url": u, "ok": 0, "fail": 0})
            if res.get("ok"):
                t["ok"] += 1
            else:
                t["fail"] += 1
    rows = sorted(tally.values(), key=lambda t: (-t["fail"], t["url"]))
    return rows


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


def _running(site_dir):
    lock = site_dir / ".running"
    try:
        if lock.exists() and (time.time() - lock.stat().st_mtime) < RUNNING_MAX_AGE:
            return True
    except Exception:
        pass
    return False


def site_status(site):
    d = SCRAPERS_DIR / site
    ctrl = control().get(site, {})
    enabled = ctrl.get("enabled", True)
    running = _running(d)
    status = _load_json(d / "status.json", None)
    history = _read_history(d)
    st = {
        "site": site, "configured": d.exists(), "session": (d / "profile").exists(),
        "enabled": enabled, "running": running, "status": status,
        "history": history, "eff": _effectiveness(history), "per_url": _per_url(history),
        "last_age_str": "—", "stale": False, "health": "never",
    }
    if status:
        secs = _age_seconds(status.get("written_at") or status.get("run_end"))
        st["last_age_str"] = _fmt_age(secs)
        st["stale"] = secs is not None and secs > STALE_HOURS * 3600
    # Health precedence.
    if running:
        st["health"] = "running"
    elif not enabled:
        st["health"] = "disabled"
    elif not status:
        st["health"] = "session_ready" if st["session"] else "never"
    elif not status.get("logged_in", True):
        st["health"] = "logged_out"
    elif status.get("failed"):
        st["health"] = "partial"
    elif status.get("downloaded"):
        st["health"] = "ok"
    else:
        st["health"] = "unknown"
    return st


# ----- live FTP reachability probe -----

def _load_env(path):
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
            s["ftp_ok"], s["ftp_msg"] = True, "reachable" + (" · " + rdir if rdir else "")
        except Exception as e:
            s["ftp_ok"], s["ftp_msg"] = False, "dir unavailable: %s" % str(e)[:50]
    if ftp is not None:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass


def all_status():
    sites = [site_status(s) for s in SITES if (SCRAPERS_DIR / s).exists()]
    _annotate_ftp(sites, _load_env(SCRAPERS_DIR / ".env"))
    return sites
