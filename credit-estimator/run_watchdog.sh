#!/bin/bash
# Credit Pipeline poller watchdog — one shot; fired every few minutes by launchd
# (com.dealerplatform.pipeline.watchdog.plist). Reloads the poller agent if its
# heartbeat has gone stale, so leads keep flowing.
cd /Users/markrankin/claude/credit-estimator || exit 1
exec /Users/markrankin/claude/dealer-leads/.venv/bin/python \
     /Users/markrankin/claude/credit-estimator/pipeline_watchdog.py
