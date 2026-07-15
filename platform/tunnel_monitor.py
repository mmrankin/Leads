"""Monitor for the Cloudflare Tunnel (cloudflared).

Runs as a daemon thread inside the always-on admin web app (like stall_monitor) —
NOT a launchd job, so it keeps watching even when launchd timers wedge, and it
alerts over Twilio's REST API (a direct HTTPS call, NOT through the tunnel) so a
dead tunnel can't mute its own alarm. The admin app itself is local, so it stays
up when the tunnel dies.

Every TUNNEL_CHECK_SEC it checks that a cloudflared tunnel process is alive
(re-checked after a short pause to ignore a momentary gap while KeepAlive is
relaunching it). If it's really down it (1) restarts the tunnel LaunchAgent and
(2) texts the on-call number, throttled so a flapping tunnel can't spam.

Env (prod .env):
    TUNNEL_CHECK_SEC            seconds between checks (default 60)
    TUNNEL_ALERT_COOLDOWN_MIN   min minutes between texts (default 15)
    TUNNEL_LABEL                launchd label (default com.fyiauto.tunnel)
    TUNNEL_PROC_MATCH           pgrep pattern (default "cloudflared tunnel")
    TUNNEL_ALERT_TO             recipient E.164 (default +18508555707)
"""

import logging
import os
import subprocess
import threading
import time
from datetime import datetime

import platform_db as pdb
import sms_alert

LOG = logging.getLogger("tunnel_monitor")
CHECK_SEC = int(os.environ.get("TUNNEL_CHECK_SEC", "60"))
COOLDOWN_MIN = int(os.environ.get("TUNNEL_ALERT_COOLDOWN_MIN", "15"))
TUNNEL_LABEL = os.environ.get("TUNNEL_LABEL", "com.fyiauto.tunnel")
PROC_MATCH = os.environ.get("TUNNEL_PROC_MATCH", "cloudflared tunnel")
ALERT_TO = os.environ.get("TUNNEL_ALERT_TO", "+18508555707")
_ALERT_KEY = "tunnel_last_alert_at"

_started = False
_lock = threading.Lock()


def _running():
    """True if a cloudflared tunnel process is alive. On an inconclusive check
    (pgrep error) returns True so we never false-alarm or restart blindly."""
    try:
        r = subprocess.run(["pgrep", "-f", PROC_MATCH], timeout=10,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception as e:                           # noqa: BLE001
        LOG.warning("pgrep failed: %s", e)
        return True


def restart_tunnel():
    """Kick the tunnel LaunchAgent (forces a start even if launchd threw it into a
    not-running / throttled state, e.g. after a cloudflared self-update)."""
    try:
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{TUNNEL_LABEL}"],
                       timeout=25, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:                           # noqa: BLE001
        LOG.warning("tunnel restart failed: %s", e)
        return False


def _mins_since(iso):
    if not iso:
        return None
    try:
        return (datetime.now() - datetime.strptime(iso[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60.0
    except Exception:                                # noqa: BLE001
        return None


def check_once():
    """One health check + (restart, throttled alert) if the tunnel is down.
    Returns True when the tunnel is up."""
    if _running():
        return True
    time.sleep(3)
    if _running():                                   # momentary gap (KeepAlive relaunch) — ignore
        return True

    restarted = restart_tunnel()
    ago = _mins_since(pdb.get_setting(_ALERT_KEY))
    if ago is not None and ago < COOLDOWN_MIN:
        LOG.warning("tunnel DOWN — alert suppressed (%.0f<%d cooldown); restarted=%s",
                    ago, COOLDOWN_MIN, restarted)
        return False
    msg = ("[DLRPro] Cloudflare TUNNEL down — dlrpro.com / fyiAuto unreachable.%s"
           % (" Auto-restarted." if restarted else " Auto-restart FAILED."))
    ok, detail = sms_alert.send(msg, to=ALERT_TO)
    pdb.set_setting(_ALERT_KEY, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    LOG.warning("TUNNEL alert: sms=%s (%s) restarted=%s", ok, detail, restarted)
    return False


def _loop():
    while True:
        try:
            check_once()
            # Liveness heartbeat (also lets the admin surface "monitor alive").
            pdb.set_setting("tunnel_monitor_last_check",
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:                       # noqa: BLE001
            LOG.warning("monitor error: %s", e)
        time.sleep(CHECK_SEC)


def start():
    """Start the monitor thread once (idempotent)."""
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_loop, name="tunnel-monitor", daemon=True).start()
        _started = True
        LOG.info("tunnel monitor on: check=%ss cooldown=%sm label=%s to=%s sms=%s",
                 CHECK_SEC, COOLDOWN_MIN, TUNNEL_LABEL, ALERT_TO, sms_alert.is_configured())
