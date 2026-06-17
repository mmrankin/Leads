"""Seed a demo dealer and grant it both products (active today).

Usage:  python seed.py
"""

from datetime import date

import platform_db as pdb

DEALER = {
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
    pdb.upsert_dealer(DEALER)
    today = date.today().isoformat()
    for code, monthly, per_lead in (
        (pdb.PRODUCT_LEAD_FORM, 199.0, 5.0),
        (pdb.PRODUCT_TRADE_IN, 149.0, 8.0),
    ):
        pdb.upsert_grant({
            "dealer_id": "DEMO", "product_code": code,
            "valid_from": today, "valid_to": None,
            "monthly_price": monthly, "per_lead_price": per_lead,
        })
    print(f"Seeded dealer DEMO with active {pdb.PRODUCT_LEAD_FORM} + "
          f"{pdb.PRODUCT_TRADE_IN} grants (valid_from {today}, open-ended).")
