"""Manheim saved-search exporter.

Reuses a persistent Chrome profile (you log in once with `--login`), then for
each saved-search URL in urls.txt it opens the results, clicks Share -> Export to
download the file, and finally uploads every downloaded file to the FTP host.

Usage (run from a Terminal on the Mac Studio so the browser window can appear):
    python scraper.py --login       # one-time: log into Manheim, then press Enter
    python scraper.py               # scrape all URLs, then FTP-upload
    python scraper.py --no-upload   # scrape only (leave files in ./downloads)
    python scraper.py --only 1      # scrape just the first URL (for testing)
    python scraper.py --headless    # try without a visible window (anti-bot may block)

The Share/Export selectors are best-effort; on any failure the page HTML and a
screenshot are written to ./debug so the exact controls can be pinned down.
"""
import argparse
import datetime
import json
import os
import pathlib
import sys

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = pathlib.Path(__file__).resolve().parent
SCRAPERS = BASE.parent
PROFILE_DIR = BASE / "profile"
DOWNLOAD_DIR = BASE / "downloads"
DEBUG_DIR = BASE / "debug"
URLS_FILE = BASE / "urls.txt"
STATUS_FILE = BASE / "status.json"   # last-run summary, read by the admin status page


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def write_status(payload):
    """Persist the last-run summary for the admin Scraper Status page."""
    payload["written_at"] = _now()
    try:
        STATUS_FILE.write_text(json.dumps(payload, indent=2))
    except Exception:
        pass

# Load shared secrets and make the shared uploader importable.
load_dotenv(SCRAPERS / ".env")
sys.path.insert(0, str(SCRAPERS))
import ftp_upload  # noqa: E402

START_URL = "https://search.manheim.com/"
# A logged-out session lands on one of these (Cox/Manheim SSO).
LOGIN_HOST_HINTS = ("login", "signin", "sign-in", "oauth", "authorize", "identity", "auth.")

# Chrome launch args that reduce the most obvious automation fingerprint.
CHROME_ARGS = ["--disable-blink-features=AutomationControlled", "--start-maximized"]


def load_urls():
    return [ln.strip() for ln in URLS_FILE.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def looks_logged_out(page):
    url = (page.url or "").lower()
    if any(h in url for h in LOGIN_HOST_HINTS):
        return True
    # A visible password field is a strong sign we're on a login screen.
    try:
        if page.locator("input[type='password']").first.is_visible(timeout=1500):
            return True
    except PWTimeout:
        pass
    return False


def _dump_debug(page, tag):
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = tag.replace("/", "_")
    try:
        page.screenshot(path=str(DEBUG_DIR / ("%s.png" % stamp)), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_DIR / ("%s.html" % stamp)).write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def _click_first(page, strategies, timeout=8000):
    """Try each (kind, value) locator strategy in order; click the first visible
    match. Returns True on success. Strategies: ('role', name) button by role,
    ('text', str) by visible text, ('css', selector)."""
    for kind, value in strategies:
        try:
            if kind == "role":
                loc = page.get_by_role("button", name=value, exact=False).first
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


def export_one(page, url, index):
    """Open a results URL and click Share -> Export, capturing the download.
    Returns the saved file path, or None on failure (debug artifacts written)."""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2500)  # let the results grid settle

    if looks_logged_out(page):
        _dump_debug(page, "loggedout_%02d" % index)
        raise RuntimeError("session appears logged out — re-run with --login")

    # Open Share.
    if not _click_first(page, [("role", "Share"), ("text", "Share"),
                               ("css", "[aria-label*='Share' i]"),
                               ("css", "[data-testid*='share' i]")]):
        _dump_debug(page, "no_share_%02d" % index)
        return None
    page.wait_for_timeout(1200)

    # Click Export and capture the resulting download.
    try:
        with page.expect_download(timeout=60000) as dl_info:
            if not _click_first(page, [("role", "Export"), ("text", "Export"),
                                       ("css", "[aria-label*='Export' i]"),
                                       ("css", "[data-testid*='export' i]")]):
                _dump_debug(page, "no_export_%02d" % index)
                return None
        download = dl_info.value
    except PWTimeout:
        _dump_debug(page, "no_download_%02d" % index)
        return None

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    suggested = download.suggested_filename or ("manheim_%02d.csv" % index)
    out = DOWNLOAD_DIR / ("manheim_%02d_%s" % (index, suggested))
    download.save_as(str(out))
    return str(out)


def open_context(pw, headless):
    PROFILE_DIR.mkdir(exist_ok=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        str(PROFILE_DIR), channel="chrome", headless=headless,
        accept_downloads=True, args=CHROME_ARGS, no_viewport=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def do_login():
    with sync_playwright() as pw:
        ctx, page = open_context(pw, headless=False)
        page.goto(START_URL, wait_until="domcontentloaded")
        print("\nA Chrome window is open. Log into Manheim (complete any 2FA/prompts),")
        print("get to the search page, then come back here and press Enter to save the session.")
        try:
            input()
        except EOFError:
            page.wait_for_timeout(120000)
        logged_in = not looks_logged_out(page)
        ctx.close()
        print("Session saved to %s" % PROFILE_DIR)
        write_status({"site": "manheim", "mode": "login", "logged_in": logged_in,
                      "run_start": None, "run_end": _now(), "results": [],
                      "downloaded": 0, "failed": 0, "uploaded": 0, "error": None})


def do_scrape(headless, do_upload, only):
    urls = load_urls()
    if only:
        urls = urls[:only]
    run_start = _now()
    results = []   # per-URL: {url, ok, file, error}
    files = []
    error = None
    logged_in = True
    with sync_playwright() as pw:
        ctx, page = open_context(pw, headless=headless)
        try:
            page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
            if looks_logged_out(page):
                logged_in = False
                error = "not logged in — run: python scraper.py --login"
                print("Not logged in. Run:  python scraper.py --login")
            else:
                for i, url in enumerate(urls, 1):
                    print("[%d/%d] %s" % (i, len(urls), url))
                    try:
                        saved = export_one(page, url, i)
                    except RuntimeError as e:   # session dropped mid-run
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
        finally:
            ctx.close()

    failed = sum(1 for r in results if not r["ok"])
    print("\nDownloaded %d/%d files." % (len(files), len(urls)))
    if failed:
        print("Failed URLs: %d (debug artifacts in %s)" % (failed, DEBUG_DIR))
    uploaded = []
    if files and do_upload:
        print("Uploading %d file(s) to FTP %s ..." % (len(files), os.environ.get("MANHEIM_REMOTE_DIR")))
        try:
            uploaded = ftp_upload.upload_files(files, remote_dir=os.environ.get("MANHEIM_REMOTE_DIR"))
            for w in uploaded:
                print("   uploaded %s" % w)
        except Exception as e:
            error = error or ("upload failed: %s" % e)
            print("   ! upload failed: %s" % e)

    write_status({"site": "manheim", "mode": "scrape", "logged_in": logged_in,
                  "run_start": run_start, "run_end": _now(),
                  "urls_total": len(urls), "downloaded": len(files),
                  "failed": failed, "uploaded": len(uploaded),
                  "results": results, "error": error})
    return 0 if files and not failed and logged_in else 1


def main():
    ap = argparse.ArgumentParser(description="Manheim saved-search exporter")
    ap.add_argument("--login", action="store_true", help="one-time interactive login")
    ap.add_argument("--no-upload", action="store_true", help="scrape only, skip FTP upload")
    ap.add_argument("--headless", action="store_true", help="run without a visible window")
    ap.add_argument("--only", type=int, default=0, help="scrape only the first N URLs")
    ap.add_argument("--check-ftp", action="store_true", help="test FTP connectivity and exit")
    args = ap.parse_args()

    if args.check_ftp:
        ok, msg = ftp_upload.check_connection(os.environ.get("MANHEIM_REMOTE_DIR"))
        print(("FTP OK: " if ok else "FTP FAIL: ") + msg)
        return 0 if ok else 1
    if args.login:
        do_login()
        return 0
    return do_scrape(headless=args.headless, do_upload=not args.no_upload, only=args.only)


if __name__ == "__main__":
    sys.exit(main())
