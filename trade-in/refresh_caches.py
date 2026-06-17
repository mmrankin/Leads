"""Refresh the local lookup caches the valuation uses, so requests are fast.

  - vin8_map        : make/model -> top-N first-8 VIN prefixes (from vin_data1)
  - inv_prefix_count: first-8 VIN prefix -> # for sale (from tbl_inventory)

vin8_map changes rarely (new model years); inv_prefix_count tracks live
inventory, so schedule this (e.g. nightly) to keep the "Similar Vehicles for
Sale" metric current. Usage:  python refresh_caches.py [--vin8]
"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import db
import valuation

if __name__ == "__main__":
    db.init_db()
    if not valuation.is_enabled():
        raise SystemExit("VIN_DB_SERVER not configured; cannot refresh caches.")

    # vin8_map is slow + rarely changes — only rebuild when asked or empty.
    if "--vin8" in sys.argv or db.vin8_count() == 0:
        print("Building vin8_map (make/model -> first-8 prefixes)…")
        print("  vin8_map rows:", valuation.build_vin8_map())

    print("Building inv_prefix_count (inventory counts per first-8 prefix)…")
    print("  inv_prefix_count rows:", valuation.build_inv_count_cache())
    print("Done.")
