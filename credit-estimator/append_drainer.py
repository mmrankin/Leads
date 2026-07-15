#!/usr/bin/env python3
"""Drain the append backlog.

Appends every vw_EquifaxConsumerRecordTriggers record not yet in
credit_append_log, in batches, honoring the append circuit breaker. Launched
detached by the admin "Append all" button. Sets platform_settings
`append_drain_running` = 1 while it runs and clears it on exit (so the button
won't double-launch it). Bounded to a max runtime as a safety net.
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
LS, DB = pipeline_enrich.LINKED_SERVER, pipeline_enrich.CP_DB


def _remaining():
    try:
        return dlr.one(
            "SELECT COUNT(*) c FROM [%s].[%s].[dbo].[vw_EquifaxConsumerRecordTriggers] v "
            "LEFT JOIN dlrPro.dbo.credit_append_log al ON al.result_id=v.result_id "
            "WHERE al.result_id IS NULL" % (LS, DB))["c"]
    except Exception:
        return 0


def main():
    pdb.set_setting(FLAG, "1")
    start = time.time()
    try:
        while _remaining() > 0 and time.time() - start < MAX_RUNTIME_SEC:
            if pdb.append_api_paused():          # API in cooldown — wait it out
                time.sleep(60)
                continue
            n = pipeline_enrich.append_from_view(limit=50, workers=5)
            time.sleep(1 if n else 15)
    finally:
        pdb.set_setting(FLAG, "0")


if __name__ == "__main__":
    main()
