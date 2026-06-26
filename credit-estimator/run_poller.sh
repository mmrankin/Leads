#!/bin/bash
# Credit Pipeline lead poller — one shot; intended to be fired every minute by
# launchd (com.dealerplatform.pipeline.plist) or cron. Honors the global flow
# switch in the platform admin; does nothing when the switch is OFF.
cd /Users/markrankin/claude/credit-estimator || exit 1
exec /Users/markrankin/claude/dealer-leads/.venv/bin/python \
     /Users/markrankin/claude/credit-estimator/pipeline_poller.py
