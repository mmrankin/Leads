"""Pre-populate the local squish-VIN cache for every year/make/model.

Runs one grouped scan of vin_data1 (~30-90s) so valuation never hits the slow
scan at request time. Safe to re-run (refreshes the cache). Schedule periodically
(e.g. weekly) to pick up new model years.

Usage:  python build_squish_cache.py
"""

import os

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
        raise SystemExit("VIN_DB_SERVER not configured; cannot build cache.")
    print("Scanning vin_data1 and building squish cache (this can take a minute)…")
    n = valuation.build_squish_cache()
    print(f"Cached {n} year/make/model squish patterns. Total in cache: {db.squish_count()}")
