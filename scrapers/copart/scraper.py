"""Copart sales-data exporter (see scrapers/README.md).

Reuses a persistent Chrome profile; opens the sales-data page and clicks
"Download CSV file", then uploads the CSV to FTP /copart/runlist/To_Process.
There is a single URL for Copart.

Run from a Terminal on the Mac Studio (so the window can appear):
    python scraper.py --login              # one-time login
    python scraper.py                      # download CSV + upload
    python scraper.py --no-upload          # download only
    python scraper.py --check-ftp          # test the FTP connection
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import scraper_common as sc  # noqa: E402
from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402


def login_recover(page):
    """Copart's authenticated session rides on a session cookie (JSESSIONID) that
    doesn't survive a browser relaunch, so each run lands on the login form with
    the email + password already autofilled by Chrome from the saved profile. Click
    the exact "Sign in" submit (NOT the Google/Apple buttons) to re-establish the
    session. No credentials are stored here — they live in the profile's password
    manager, set during the one-time --login."""
    if not page.locator("#member-password").first.is_visible(timeout=4000):
        return
    sc.pace(page)  # let Chrome finish autofilling; don't move at bot speed
    btn = page.get_by_role("button", name="Sign in", exact=True).first
    btn.wait_for(state="visible", timeout=8000)
    btn.click()
    try:
        page.wait_for_url("**/downloadSalesData**", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass
    sc.pace(page)


def export_one(page, url, index, paths):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=45000)
    except PWTimeout:
        pass
    page.wait_for_timeout(3000)  # let the page fully render (and the login redirect settle)

    # Copart's session doesn't survive the relaunch, so we usually land here on the
    # login form — re-sign-in with the saved credentials, then reload the page.
    if sc.looks_logged_out(page, CFG.login_hints):
        login_recover(page)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=45000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)
    if sc.looks_logged_out(page, CFG.login_hints):
        sc.dump_debug(page, paths, "loggedout_%02d" % index)
        raise sc.LoggedOut("session appears logged out — re-run with --login")

    try:
        with page.expect_download(timeout=60000) as dl:
            if not sc.click_first(page, [
                    ("role", "Download CSV file"), ("text", "Download CSV file"),
                    ("link", "Download CSV"), ("text", "Download CSV"),
                    ("css", "a[href*='downloadSalesData' i]"),
                    ("css", "[href*='.csv' i]")]):
                sc.dump_debug(page, paths, "no_csv_%02d" % index)
                return None
        download = dl.value
    except PWTimeout:
        sc.dump_debug(page, paths, "no_download_%02d" % index)
        return None
    return sc.save_download(download, paths, "copart", index)


CFG = sc.SiteCfg(
    site="copart",
    start_url="https://www.copart.com/downloadSalesData/",
    login_url="https://www.copart.com/login",
    remote_dir_env="COPART_REMOTE_DIR",
    export_one=export_one,
    login_hints=sc.DEFAULT_LOGIN_HINTS,
    login_note=None,
    paths=sc.paths_for(__file__),
    login_recover=login_recover,
)

if __name__ == "__main__":
    sys.exit(sc.main(CFG))
