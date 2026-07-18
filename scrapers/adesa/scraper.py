"""Adesa saved-search exporter (see scrapers/README.md).

Reuses a persistent Chrome profile; for each results URL in urls.txt it waits for
the page to fully render, then clicks "Open with Excel" to download the .xls, and
uploads the files to FTP /adesa/To_Process.

Run from a Terminal on the Mac Studio (so the window can appear):
    python scraper.py --login              # one-time login (check "save this browser for 30 days")
    python scraper.py                      # export all URLs + upload
    python scraper.py --only 1 --no-upload # test the first URL, no upload
    python scraper.py --check-ftp          # test the FTP connection
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import scraper_common as sc  # noqa: E402
from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402


def export_one(page, url, index, paths):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=45000)
    except PWTimeout:
        pass
    page.wait_for_timeout(3000)  # wait for the results to fully display

    if sc.looks_logged_out(page, CFG.login_hints):
        sc.dump_debug(page, paths, "loggedout_%02d" % index)
        raise sc.LoggedOut("session appears logged out — re-run with --login")

    # "Open with Excel" downloads the results spreadsheet. Try a range of forms
    # (link/button/image/title) since the exact control isn't known yet.
    try:
        with page.expect_download(timeout=60000) as dl:
            if not sc.click_first(page, [
                    ("link", "Open with Excel"), ("text", "Open with Excel"),
                    ("link", "Open in Excel"), ("text", "Open in Excel"),
                    ("text", "Excel"),
                    ("css", "a[href*='excel' i]"), ("css", "a[href*='xls' i]"),
                    ("css", "[title*='Excel' i]"), ("css", "img[alt*='Excel' i]")]):
                sc.dump_debug(page, paths, "no_excel_%02d" % index)
                return None
        download = dl.value
    except PWTimeout:
        sc.dump_debug(page, paths, "no_download_%02d" % index)
        return None
    return sc.save_download(download, paths, "adesa", index)


CFG = sc.SiteCfg(
    site="adesa",
    start_url="https://buy.adesa.com/",
    login_url="https://buy.adesa.com/",
    remote_dir_env="ADESA_REMOTE_DIR",
    export_one=export_one,
    login_hints=sc.DEFAULT_LOGIN_HINTS + ("adesa.com/u/login", "marketplace.adesa"),
    login_note="On the 2FA step, check 'save this browser for 30 days' before finishing.",
    paths=sc.paths_for(__file__),
)

if __name__ == "__main__":
    sys.exit(sc.main(CFG))
