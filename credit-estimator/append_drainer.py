#!/usr/bin/env python3
"""Drain the append backlog.

Appends vw_EquifaxConsumerRecordTriggers records not yet in credit_append_log, in
batches, honoring the append circuit breaker. Launched detached by the admin
"Append all" buttons. With the CLI arg `eligible`, only the sendable subset is
appended — fresh (≤ the send window) records whose dealer has an active
CREDIT_PIPELINE grant (see pipeline_enrich._APPEND_ELIGIBLE_WHERE); otherwise the
whole backlog. Sets platform_settings `append_drain_running` = 1 while it runs and
clears it on exit (so the button won't double-launch it). Bounded to a max
runtime as a safety net.
"""

import os
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

PLATFORM_DIR = os.environ.get(
    "PLATFORM_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "platform"))
for p in (PLATFORM_DIR, os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import platform_db as pdb
import dlrpro_db as dlr
import pipeline_enrich

FLAG = "append_drain_running"
MAX_RUNTIME_SEC = int(os.environ.get("APPEND_DRAIN_MAX_SEC", str(6 * 3600)))
# Only the eligible (fresh + active-CP-dealer) subset when launched with `eligible`.
ELIGIBLE = len(sys.argv) > 1 and sys.argv[1].lower() == "eligible"


def main():
    pdb.set_setting(FLAG, "1")
    start = time.time()
    try:
        while pipeline_enrich.appendable_count(eligible=ELIGIBLE) > 0 \
                and time.time() - start < MAX_RUNTIME_SEC:
            if pdb.append_api_paused():          # API in cooldown — wait it out
                time.sleep(60)
                continue
            n = pipeline_enrich.append_from_view(limit=50, workers=5, eligible=ELIGIBLE)
            time.sleep(1 if n else 15)
    finally:
        pdb.set_setting(FLAG, "0")


if __name__ == "__main__":
    main()
