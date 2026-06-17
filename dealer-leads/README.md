# Dealer Lead Form

A self-contained lead-capture product for car dealerships. Each dealer gets a
clean, unbranded customer form. On submission, the lead is saved and delivered
to the dealer as **ADF/XML** (Auto-lead Data Format) by email via SendGrid.

## Features

- **No branding** at the top of the form by default. A per-dealer **banner**
  image can be added later through the `/setup` screen.
- **One `dealers` table** for every dealership using the product.
- **One `leads` table** for every captured lead.
- Customer **data-collection page** + **thank-you page**.
- Requires **email or phone** (enforced in the app and by a DB `CHECK`).
- Builds **ADF/XML** and emails it to the dealer's `leadEmailAddress` after a
  successful submission.
- **Email validation** of the customer's address via SendGrid's Email
  Validation API (rejects undeliverable addresses; fails open on any API error).
- **Cascading Year → Make → Model** dropdowns sourced live from SQL Server
  (`vin_decode..VIN_Data1_YMM` on 10.1.1.10), cached in-process. They can be
  pre-populated from the URL, e.g.
  `/d/DEMO?year=2024&make=Acura&model=Integra` (case-insensitive).

## Layout

| File | Purpose |
|------|---------|
| `app.py` | Flask routes: lead form, thank-you, setup |
| `db.py` | SQLite helpers |
| `schema.sql` | `dealers` + `leads` tables |
| `adf.py` | ADF/XML builder |
| `email_send.py` | SendGrid delivery (falls back to `./outbox/`) |
| `email_validate.py` | SendGrid Email Validation (fail-open) |
| `vin_db.py` | Year/Make/Model lookups from SQL Server (cached) |
| `templates/` | Form, thank-you, and setup pages |
| `static/style.css` | Styling |
| `seed.py` | Inserts a sample `DEMO` dealer |

## Run

```bash
cd dealer-leads
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in SendGrid + secrets (optional for local)
python seed.py              # creates a DEMO dealer
python app.py               # http://localhost:5000
```

- Customer form:  `http://localhost:5000/d/DEMO`
- Thank-you page: `http://localhost:5000/d/DEMO/thanks`
- Setup / dealers: `http://localhost:5000/setup`

## Email delivery

Set `SENDGRID_API_KEY` and a verified `LEAD_FROM_EMAIL` in `.env`. Each lead is
emailed to the dealer's `lead_email_address` with the ADF/XML both inline and as
an attachment. If SendGrid is not configured, leads are still saved and each ADF
is written to `./outbox/` so nothing is lost.

## Routing a dealer

The form URL embeds the `DealerID`: `/d/<DealerID>`. Add dealers at `/setup`
(protect it by setting `SETUP_PASSWORD`).

## Database notes

SQLite by default (`leads.db`). To move to SQL Server later, the schema in
`schema.sql` maps cleanly — swap the helpers in `db.py` for a `pyodbc`
connection.
