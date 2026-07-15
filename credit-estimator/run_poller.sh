#!/bin/bash
# Credit Pipeline lead poller — persistent, supervised loop.
#
# Runs the poller once per POLL_INTERVAL seconds (a FRESH python process each
# tick, so code edits apply next tick and DB connections stay fresh), forever.
# launchd runs this under KeepAlive=true (com.dealerplatform.pipeline.plist),
# which relaunches it if it ever exits — so the schedule no longer depends on
# launchd's StartInterval firing, which stalled unreliably on this host and
# silently killed the send (the "dies every hour" symptom).
#
# Each tick is hard-capped at POLL_MAX seconds, so one stuck run can't freeze the
# loop. The poller honors the global flow switch and exits fast when it is OFF,
# so an idle loop is cheap.
cd /Users/markrankin/claude/credit-estimator || exit 1
PY=/Users/markrankin/claude/dealer-leads/.venv/bin/python
SCRIPT=/Users/markrankin/claude/credit-estimator/pipeline_poller.py
INTERVAL="${POLL_INTERVAL:-60}"
MAX="${POLL_MAX:-180}"

while true; do
    "$PY" "$SCRIPT" &
    pid=$!
    ( sleep "$MAX"; kill -9 "$pid" 2>/dev/null ) &   # cap a single tick
    killer=$!
    wait "$pid" 2>/dev/null
    kill "$killer" 2>/dev/null; wait "$killer" 2>/dev/null
    sleep "$INTERVAL"
done
