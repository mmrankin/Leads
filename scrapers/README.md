# Auction scrapers

Log into an auction site, export saved-search result files, and upload them to the
`ftp.vin10.net` FTP host. One folder per site; Manheim is implemented first
(Adesa and Copart to follow, sharing `ftp_upload.py`).

Runs on the Mac Studio (10.1.1.117) with real Chrome via Playwright, reusing a
persistent, already-logged-in browser profile (no stored site password, and far
more resistant to Manheim's bot detection).

## Setup (one time)

```bash
cd ~/claude/scrapers
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Secrets live in `scrapers/.env` (gitignored) — FTP host/user/password and each
site's remote upload directory.

## Manheim

**1. Log in once** (run from a Terminal *on the Mac Studio* so the window appears):

```bash
cd ~/claude/scrapers/manheim
../.venv/bin/python scraper.py --login
```

A Chrome window opens on manheim.com. Log in (complete any 2FA), reach the search
page, then press Enter in the Terminal. The session is saved under `manheim/profile/`.

**2. Run the export + upload:**

```bash
../.venv/bin/python scraper.py                # all URLs -> download -> FTP upload
../.venv/bin/python scraper.py --only 1        # just the first URL (test)
../.venv/bin/python scraper.py --no-upload     # download only, no upload
../.venv/bin/python scraper.py --check-ftp    # test FTP connection only
```

The saved-search URLs to export are in `manheim/urls.txt` (one per line).
Downloads land in `manheim/downloads/`; on any Share/Export failure, a screenshot
and page HTML are written to `manheim/debug/` for troubleshooting.

If a run reports "logged out", re-run step 1 to refresh the session.

## Scheduling (after the one-time login)

Each site has a ready-to-load LaunchAgent in `deploy/` that runs
`scrapers/run.sh <site>` on a schedule (Copart every 15 min; Manheim and Adesa
hourly — change `StartInterval` to adjust). They run **headful** in the logged-in
Aqua session, so the Mac Studio must stay logged in. Load one after its session
is saved:

```bash
cp ~/claude/deploy/com.dealerplatform.scraper.copart.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dealerplatform.scraper.copart.plist
# (repeat with manheim / adesa)
```

Stop / reload one:

```bash
launchctl bootout gui/$(id -u)/com.dealerplatform.scraper.copart
```

Logs: `deploy/scraper.<site>.out.log` / `.err.log`. Run status also shows on the
admin **Auction → Scraper Status** page.

