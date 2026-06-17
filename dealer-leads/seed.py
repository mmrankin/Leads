"""Seed a sample dealer so you can try the form immediately.

Usage:  python seed.py
Then visit /d/DEMO
"""

import db

SAMPLE = {
    "dealer_id": "DEMO",
    "dealer_name": "Demo Motors",
    "address": "100 Main St",
    "city": "Springfield",
    "state": "IL",
    "zip": "62701",
    "phone": "(217) 555-0100",
    "lead_email_address": "sales@demomotors.example",
    "banner_url": "",  # add a URL via /setup to show a banner
}

if __name__ == "__main__":
    db.init_db()
    db.upsert_dealer(SAMPLE)
    print(f"Seeded dealer '{SAMPLE['dealer_id']}'. Visit /d/{SAMPLE['dealer_id']}")
