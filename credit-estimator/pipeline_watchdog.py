#!/usr/bin/env python3
"""Credit Pipeline poller watchdog.

Runs every few minutes (launchd, com.dealerplatform.pipeline.watchdog). If the
poller hasn't stamped its heartbeat in > STALE_MINUTES, the scheduler has stalled
— reload + kick the poller LaunchAgent so leads keep flowing (unsent Credit
Pipeline leads expire after ~30 minutes, so a dead poller silently loses them).

Env: PIPELINE_STALE_MINUTES (default 12).
"""

import logging
import os
import subprocess
import sys
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

PLATFORM_DIR = os.environ.get(
    "PLATFORM_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "platform"),
)
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

import platform_db as pdb

LOG = logging.getLogger("pipeline_watchdog")
STALE_MINUTES = float(os.environ.get("PIPELINE_STALE_MINUTES", "12"))
POLLER_LABEL = "com.dealerplatform.pipeline"
POLLER_PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % POLLER_LABEL)


def _lc(*args):
    """Run a launchctl command; return (ok, combined output)."""
    try:
        r = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:                       # noqa: BLE001
        return False, str(e)


def reload_poller():
    """bootout (ignore errors) + bootstrap + kickstart the poller agent — this both
    forces a run now and re-arms the StartInterval timer."""
    dom = "gui/%d" % os.getuid()
    _lc("bootout", "%s/%s" % (dom, POLLER_LABEL))
    ok_bs, out_bs = _lc("bootstrap", dom, POLLER_PLIST)
    ok_ks, out_ks = _lc("kickstart", "-k", "%s/%s" % (dom, POLLER_LABEL))
    LOG.warning("reloaded poller: bootstrap ok=%s (%s); kickstart ok=%s (%s)",
                ok_bs, out_bs[:120], ok_ks, out_ks[:120])
    return ok_bs or ok_ks


def run():
    age = pdb.pipeline_heartbeat_age_minutes()
    pdb.set_setting("pipeline_watchdog_last_check", datetime.utcnow().isoformat())
    if age is None or age > STALE_MINUTES:
        shown = "never" if age is None else "%.1f min" % age
        LOG.warning("poller STALE (last run %s ago; threshold %s min) — reloading",
                    shown, STALE_MINUTES)
        reload_poller()
        pdb.set_setting("pipeline_watchdog_last_action",
                        "%s reloaded (poller age=%s)" % (datetime.utcnow().isoformat(), shown))
    else:
        LOG.info("poller OK (last run %.1f min ago)", age)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
