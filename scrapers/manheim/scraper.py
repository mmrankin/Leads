"""Manheim saved-search exporter (see scrapers/README.md).

Reuses a persistent Chrome profile; for each URL in urls.txt it clicks
Share -> Export to download the file, then uploads to FTP /manheim/To_Process.

Run from a Terminal on the Mac Studio (so the window can appear):
    python scraper.py --login              # one-time login
    python scraper.py                      # export all URLs + upload
    python scraper.py --only 1 --no-upload # test the first URL, no upload
    python scraper.py --check-ftp          # test the FTP connection
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import scraper_common as sc  # noqa: E402
from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402


def login_recover(page):
    """Manheim's SSO session expires (~daily); it then redirects to the PingFederate
    login (auth.manheim.com) with the username autofilled but no password (Manheim's
    password isn't saved in the browser profile). Fill the credentials from
    MANHEIM_USER/MANHEIM_PASS (gitignored .env) and submit to re-establish the
    session — verified 2FA-free."""
    if not page.locator("#password").first.is_visible(timeout=4000):
        return
    user, pw = os.environ.get("MANHEIM_USER", ""), os.environ.get("MANHEIM_PASS", "")
    if not (user and pw):
        return
    try:
        if not (page.locator("#username").input_value() or "").strip():
            page.locator("#username").fill(user)
        page.locator("#password").fill(pw)
        sc.pace(page)
        page.locator("#password").press("Enter")
        page.wait_for_url("**search.manheim.com**", timeout=45000)
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass
    sc.pace(page)


def export_one(page, url, index, paths):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2500)  # let the results grid settle

    # The SSO session expires ~daily; re-login with the saved credentials, reload.
    if sc.looks_logged_out(page, CFG.login_hints):
        login_recover(page)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2500)
    if sc.looks_logged_out(page, CFG.login_hints):
        sc.dump_debug(page, paths, "loggedout_%02d" % index)
        raise sc.LoggedOut("session appears logged out — re-run with --login")

    # Open the Share menu (stable data-testid), then click Export inside it.
    if not sc.click_first(page, [("css", "[data-testid='share-menu-button']"),
                                 ("text", "Share")]):
        sc.dump_debug(page, paths, "no_share_%02d" % index)
        return None
    sc.pace(page)  # let the menu render (and don't move at bot speed)

    # The menu opens with Share Link / Export / Print. The visible Export item is
    # the only element with aria-label exactly "Export" (the hidden
    # export-data-button helper is aria-label "hidden-export-share").
    export = page.locator("[aria-label='Export']").first
    try:
        export.wait_for(state="visible", timeout=12000)
    except PWTimeout:
        sc.dump_debug(page, paths, "no_export_%02d" % index)
        return None
    try:
        with page.expect_download(timeout=60000) as dl:
            export.click()
        download = dl.value
    except PWTimeout:
        sc.dump_debug(page, paths, "no_download_%02d" % index)
        return None
    return sc.save_download(download, paths, "manheim", index)


CFG = sc.SiteCfg(
    site="manheim",
    start_url="https://search.manheim.com/",
    login_url="https://search.manheim.com/",
    remote_dir_env="MANHEIM_REMOTE_DIR",
    export_one=export_one,
    login_hints=sc.DEFAULT_LOGIN_HINTS,
    login_note=None,
    paths=sc.paths_for(__file__),
    login_recover=login_recover,
)

if __name__ == "__main__":
    sys.exit(sc.main(CFG))
