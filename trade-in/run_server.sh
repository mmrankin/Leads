#!/bin/bash
# Trade-In widget — persistent LaunchAgent (KeepAlive) on port 5001.
# Production server: waitress via serve_prod.py (not the Flask dev server).
cd /Users/markrankin/claude/trade-in || exit 1
export PORT=5001
exec /Users/markrankin/claude/dealer-leads/.venv/bin/python \
     /Users/markrankin/claude/trade-in/serve_prod.py
