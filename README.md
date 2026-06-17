# Dealer Lead Platform

A small platform of customer-facing lead-capture products for car dealerships,
plus a shared admin. Each product is an isolated Flask app with its own URL, all
sharing one dealer/entitlement database. Leads are delivered to dealers as
**ADF/XML** email (via SendGrid).

## Systems

| Directory | What it is | Port | Customer URL |
|-----------|-----------|------|--------------|
| [`dealer-leads/`](dealer-leads/) | **Lead Form** — single-page customer lead capture | 5002 | `/d/<DealerID>` |
| [`trade-in/`](trade-in/) | **Trade-In Widget** — mobile 3-step wizard with live valuation, serialized offer + barcode | 5001 | `/t/<DealerID>` |
| [`credit-estimator/`](credit-estimator/) | **Credit Estimator** — self-reported credit quiz + payment/affordability estimate | 5003 | `/c/<DealerID>` |
| [`platform/`](platform/) | **Platform Admin** — manage dealers, product access grants, valuation/credit settings; browse leads | 5050 | `/` (password-gated) |
| [`deploy/`](deploy/) | Cloudflare Tunnel deploy kit (config template, README, LaunchAgent) | — | — |

## Architecture
- **Shared platform DB** (`platform/`) holds the dealer master, the product
  catalog, per-dealer access grants (with validity windows + pricing), and the
  valuation/credit settings. Both customer apps import it via `sys.path` and
  enforce an active grant before serving (a dealer without a grant gets a 403
  "inactive" page).
- Each customer app stores its own **leads** in a local SQLite DB.
- **ADF/XML** is built per lead and emailed to the dealer's `lead_email_address`
  through SendGrid; if SendGrid isn't configured, the ADF is written to the
  app's `outbox/` so nothing is lost.
- Vehicle Year/Make/Model(/Trim) dropdowns and the trade-in valuation read a
  SQL Server VIN/inventory/auction environment (not included here).

## Running locally
Each app is self-contained:
```bash
cd <app>
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in secrets (see below)
python app.py               # platform admin: python admin.py
```
The customer apps need the shared platform on the import path; set `PLATFORM_DIR`
/ `PLATFORM_DB_PATH` in their `.env` (see each app's `.env.example`).

## Configuration / secrets
Secrets live only in each app's `.env` (git-ignored). Copy the provided
`.env.example` in each directory and fill in:
- `SENDGRID_API_KEY` + `LEAD_FROM_EMAIL` — ADF/XML email delivery
- `SENDGRID_VALIDATION_API_KEY` — optional customer-email validation
- `VIN_DB_*` — SQL Server connection for VIN/valuation lookups
- `FLASK_SECRET_KEY` — session signing (use a strong random value)
- `SETUP_PASSWORD` (platform admin) — admin login

**Never commit `.env` files, `*.db` files (customer PII), or `outbox/`.**

## Production deployment
See [`deploy/DEPLOY.md`](deploy/DEPLOY.md) — Cloudflare Tunnel + per-app
subdomains, running under `waitress` with `ProxyFix`.
