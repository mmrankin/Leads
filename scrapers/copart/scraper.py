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


def export_one(page, url, index, paths):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=45000)
    except PWTimeout:
        pass
    page.wait_for_timeout(3000)  # let the page fully render

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
)

if __name__ == "__main__":
    sys.exit(sc.main(CFG))
