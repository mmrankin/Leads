#!/bin/bash
# Platform admin — persistent LaunchAgent (KeepAlive) on port 5050.
# Production server: waitress via serve_prod.py (not the Flask dev server).
cd /Users/markrankin/claude/platform || exit 1
export PORT=5050
exec /Users/markrankin/claude/dealer-leads/.venv/bin/python \
     /Users/markrankin/claude/platform/serve_prod.py
