"""Stall monitor for the Credit Pipeline poller.

Runs as a daemon thread inside the always-on admin web app — deliberately NOT a
launchd interval job, because that scheduler is exactly what keeps wedging on
this box. Every STALL_CHECK_SEC it checks send health; if the flow is ON but the
poller hasn't run within the stall window it (1) kicks the poller to self-heal
and (2) texts the on-call number, throttled so a flapping poller can't spam.

Env (prod .env):
    STALL_CHECK_SEC             seconds between checks (default 120)
    STALL_ALERT_COOLDOWN_MIN    min minutes between texts (default 60)
    STALL_AUTO_KICK            "1" to auto-restart the poller (default on)
    POLLER_LABEL               launchd label (default com.dealerplatform.pipeline)
    PIPELINE_STALL_MIN         stall threshold, minutes (health_view, default 20)
"""

import logging
import os
import subprocess
import threading
import time
from datetime import datetime

import health_view
import platform_db as pdb
import sms_alert

LOG = logging.getLogger("stall_monitor")
CHECK_SEC = int(os.environ.get("STALL_CHECK_SEC", "120"))
COOLDOWN_MIN = int(os.environ.get("STALL_ALERT_COOLDOWN_MIN", "60"))
AUTO_KICK = os.environ.get("STALL_AUTO_KICK", "1") == "1"
POLLER_LABEL = os.environ.get("POLLER_LABEL", "com.dealerplatform.pipeline")
_ALERT_KEY = "stall_last_alert_at"

_started = False
_lock = threading.Lock()


def kick_poller():
    try:
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{POLLER_LABEL}"],
                       timeout=20, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        LOG.warning("poller kick failed: %s", e)
        return False


def _mins_since(iso):
    if not iso:
        return None
    try:
        return (datetime.now() - datetime.strptime(iso[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60.0
    except Exception:
        return None


def check_once():
    """One health check + (self-heal, throttled alert) if stalled."""
    h = health_view.send_health()
    if not h.get("stalled"):
        return h
    kicked = kick_poller() if AUTO_KICK else False
    ago = _mins_since(pdb.get_setting(_ALERT_KEY))
    if ago is not None and ago < COOLDOWN_MIN:
        LOG.info("stalled %s min — alert suppressed (%.0f<%d cooldown); kicked=%s",
                 h.get("mins_since_run"), ago, COOLDOWN_MIN, kicked)
        return h
    msg = ("[DLRPro] Credit Pipeline STALLED: poller idle %s min (flow ON, backlog %s).%s"
           % (h.get("mins_since_run"), h.get("backlog"),
              " Auto-restarted." if kicked else ""))
    ok, detail = sms_alert.send(msg)
    pdb.set_setting(_ALERT_KEY, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    LOG.warning("STALL alert: sms=%s (%s) kicked=%s", ok, detail, kicked)
    return h


def _loop():
    while True:
        try:
            check_once()
        except Exception as e:
            LOG.warning("monitor error: %s", e)
        time.sleep(CHECK_SEC)


def start():
    """Start the monitor thread once (idempotent)."""
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_loop, name="stall-monitor", daemon=True).start()
        _started = True
        LOG.info("stall monitor on: check=%ss cooldown=%sm autokick=%s sms=%s",
                 CHECK_SEC, COOLDOWN_MIN, AUTO_KICK, sms_alert.is_configured())
