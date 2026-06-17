#!/bin/bash
# Credit Estimator — persistent LaunchAgent (KeepAlive) on port 5003.
# Production server: waitress via serve_prod.py (not the Flask dev server).
cd /Users/markrankin/claude/credit-estimator || exit 1
export PORT=5003
exec /Users/markrankin/claude/dealer-leads/.venv/bin/python \
     /Users/markrankin/claude/credit-estimator/serve_prod.py
