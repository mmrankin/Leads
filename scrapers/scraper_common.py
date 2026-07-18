"""Shared engine for the auction scrapers (Manheim / Adesa / Copart).

Each site provides a small config (`SiteCfg`) plus an `export_one(page, url, index,
paths)` function that performs the site-specific export/download for one results
URL. This module handles the persistent logged-in Chrome profile, the per-URL run
loop, status.json recording, and the FTP upload.
"""
import argparse
import collections
import datetime
import json
import os
import pathlib
import random
import sys

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SCRAPERS = pathlib.Path(__file__).resolve().parent
load_dotenv(SCRAPERS / ".env")
sys.path.insert(0, str(SCRAPERS))
import ftp_upload  # noqa: E402

# Chrome args that reduce the most obvious automation fingerprint.
CHROME_ARGS = ["--disable-blink-features=AutomationControlled", "--start-maximized"]
# Hosts/paths that indicate a logged-out (login/SSO) page.
DEFAULT_LOGIN_HINTS = ("login", "signin", "sign-in", "oauth", "authorize", "identity", "auth.")

Paths = collections.namedtuple("Paths", "site_dir profile download debug urls status")
SiteCfg = collections.namedtuple(
    "SiteCfg", "site start_url login_url remote_dir_env export_one login_hints login_note paths")


def paths_for(site_file):
    base = pathlib.Path(site_file).resolve().parent
    return Paths(base, base / "profile", base / "downloads",
                base / "debug", base / "urls.txt", base / "status.json")


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def load_urls(paths):
    return [ln.strip() for ln in paths.urls.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def write_status(paths, payload):
    payload["written_at"] = now()
    try:
        paths.status.write_text(json.dumps(payload, indent=2))
    except Exception:
        pass


def looks_logged_out(page, hints):
    url = (page.url or "").lower()
    if any(h in url for h in hints):
        return True
    try:
        if page.locator("input[type='password']").first.is_visible(timeout=1500):
            return True
    except PWTimeout:
        pass
    return False


def dump_debug(page, paths, tag):
    paths.debug.mkdir(exist_ok=True)
    stamp = tag.replace("/", "_")
    try:
        page.screenshot(path=str(paths.debug / ("%s.png" % stamp)), full_page=True)
    except Exception:
        pass
    try:
        (paths.debug / ("%s.html" % stamp)).write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def pace(page, lo_ms=2500, hi_ms=6000):
    """A randomized, human-like pause. These sites watch for bot-speed activity and
    can invalidate the session if pages are hit too fast, so we never move at
    machine speed — every wait is randomized rather than uniform. Runs happen only
    every ~48h, so there is no reason to hurry."""
    page.wait_for_timeout(random.randint(lo_ms, hi_ms))


def click_first(page, strategies, timeout=8000):
    """Try each (kind, value) locator strategy in order; click the first visible
    match. kinds: 'role' (button by accessible name), 'link' (link by name),
    'text' (visible text), 'css' (raw selector)."""
    for kind, value in strategies:
        try:
            if kind == "role":
                loc = page.get_by_role("button", name=value, exact=False).first
            elif kind == "link":
                loc = page.get_by_role("link", name=value, exact=False).first
            elif kind == "text":
                loc = page.get_by_text(value, exact=False).first
            else:
                loc = page.locator(value).first
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()
            return True
        except Exception:
            continue
    return False


def open_context(pw, paths, headless):
    paths.profile.mkdir(parents=True, exist_ok=True)
    paths.download.mkdir(parents=True, exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        str(paths.profile), channel="chrome", headless=headless,
        accept_downloads=True, args=CHROME_ARGS, no_viewport=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def save_download(download, paths, site, index):
    paths.download.mkdir(parents=True, exist_ok=True)
    suggested = download.suggested_filename or ("%s_%02d" % (site, index))
    out = paths.download / ("%s_%02d_%s" % (site, index, suggested))
    download.save_as(str(out))
    return str(out)


def do_login(cfg, headless=False):
    with sync_playwright() as pw:
        ctx, page = open_context(pw, cfg.paths, headless=headless)
        page.goto(cfg.login_url, wait_until="domcontentloaded")
        print("\nA Chrome window is open for %s. Log in (complete any 2FA)." % cfg.site)
        if cfg.login_note:
            print(">> %s" % cfg.login_note)
        print("When you're logged in and on the site, come back here and press Enter to save the session.")
        try:
            input()
        except EOFError:
            page.wait_for_timeout(120000)
        logged_in = not looks_logged_out(page, cfg.login_hints)
        ctx.close()
        print("Session saved to %s" % cfg.paths.profile)
        write_status(cfg.paths, {"site": cfg.site, "mode": "login", "logged_in": logged_in,
                                 "run_start": None, "run_end": now(), "results": [],
                                 "downloaded": 0, "failed": 0, "uploaded": 0, "error": None})


def do_scrape(cfg, headless, do_upload, only):
    urls = load_urls(cfg.paths)
    if only:
        urls = urls[:only]
    run_start = now()
    results, files, error, logged_in = [], [], None, True
    with sync_playwright() as pw:
        ctx, page = open_context(pw, cfg.paths, headless=headless)
        try:
            page.goto(cfg.start_url, wait_until="domcontentloaded", timeout=60000)
            if looks_logged_out(page, cfg.login_hints):
                logged_in = False
                error = "not logged in — run: python scraper.py --login"
                print("Not logged in. Run:  python scraper.py --login")
            else:
                for i, url in enumerate(urls, 1):
                    print("[%d/%d] %s" % (i, len(urls), url))
                    try:
                        saved = cfg.export_one(page, url, i, cfg.paths)
                    except _LoggedOut as e:
                        logged_in = False
                        error = str(e)
                        print("   ! %s" % e)
                        results.append({"url": url, "ok": False, "file": None, "error": str(e)})
                        break
                    if saved:
                        print("   downloaded %s" % os.path.basename(saved))
                        files.append(saved)
                        results.append({"url": url, "ok": True,
                                        "file": os.path.basename(saved), "error": None})
                    else:
                        print("   ! export failed (see ./debug)")
                        results.append({"url": url, "ok": False, "file": None,
                                        "error": "export failed (see debug/)"})
                    # Space out requests so the run doesn't look like a bot sweep.
                    # Generous by design — these sites tolerate 30-min spacing, and
                    # runs are only every ~48h, so ~30-75s between pages is plenty.
                    if i < len(urls):
                        page.wait_for_timeout(random.randint(30000, 75000))
        finally:
            ctx.close()

    failed = sum(1 for r in results if not r["ok"])
    print("\nDownloaded %d/%d files." % (len(files), len(urls)))
    if failed:
        print("Failed: %d (debug artifacts in %s)" % (failed, cfg.paths.debug))
    uploaded = []
    remote_dir = os.environ.get(cfg.remote_dir_env)
    if files and do_upload:
        print("Uploading %d file(s) to FTP %s ..." % (len(files), remote_dir))
        try:
            uploaded = ftp_upload.upload_files(files, remote_dir=remote_dir)
            for w in uploaded:
                print("   uploaded %s" % w)
        except Exception as e:
            error = error or ("upload failed: %s" % e)
            print("   ! upload failed: %s" % e)

    write_status(cfg.paths, {"site": cfg.site, "mode": "scrape", "logged_in": logged_in,
                             "run_start": run_start, "run_end": now(),
                             "urls_total": len(urls), "downloaded": len(files),
                             "failed": failed, "uploaded": len(uploaded),
                             "results": results, "error": error})
    return 0 if files and not failed and logged_in else 1


class _LoggedOut(RuntimeError):
    """Raised by an export_one when the session has dropped mid-run."""


# Exposed so a site's export_one can signal a lost session.
LoggedOut = _LoggedOut


def main(cfg):
    ap = argparse.ArgumentParser(description="%s saved-search exporter" % cfg.site)
    ap.add_argument("--login", action="store_true", help="one-time interactive login")
    ap.add_argument("--no-upload", action="store_true", help="scrape only, skip FTP upload")
    ap.add_argument("--headless", action="store_true", help="run without a visible window")
    ap.add_argument("--only", type=int, default=0, help="scrape only the first N URLs")
    ap.add_argument("--check-ftp", action="store_true", help="test FTP connectivity and exit")
    args = ap.parse_args()

    if args.check_ftp:
        ok, msg = ftp_upload.check_connection(os.environ.get(cfg.remote_dir_env))
        print(("FTP OK: " if ok else "FTP FAIL: ") + msg)
        return 0 if ok else 1
    if args.login:
        do_login(cfg)
        return 0
    return do_scrape(cfg, headless=args.headless, do_upload=not args.no_upload, only=args.only)
