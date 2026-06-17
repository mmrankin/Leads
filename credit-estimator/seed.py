"""Seed a sample dealer + an active CREDIT_EST grant so you can try the app.

Usage:  python seed.py
Then visit /c/DEMO
"""

import os
import sys

PLATFORM_DIR = os.environ.get(
    "PLATFORM_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "platform"),
)
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

import platform_db as pdb

SAMPLE = {
    "dealer_id": "DEMO",
    "dealer_name": "Demo Motors",
    "address": "100 Main St",
    "city": "Springfield",
    "state": "IL",
    "zip": "62701",
    "phone": "(217) 555-0100",
    "lead_email_address": "sales@demomotors.example",
    "banner_url": "",
}

if __name__ == "__main__":
    pdb.init_db()
    pdb.upsert_dealer(SAMPLE)
    pdb.upsert_grant({
        "dealer_id": SAMPLE["dealer_id"],
        "product_code": pdb.PRODUCT_CREDIT_EST,
        "valid_from": "", "valid_to": "",
        "monthly_price": "", "per_lead_price": "",
    })
    print(f"Seeded dealer '{SAMPLE['dealer_id']}' with an active CREDIT_EST grant. "
          f"Visit /c/{SAMPLE['dealer_id']}")
