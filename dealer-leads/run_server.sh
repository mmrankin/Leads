#!/bin/bash
# Dealer Lead Form — persistent LaunchAgent (KeepAlive) on port 5002.
# Production server: waitress via serve_prod.py (not the Flask dev server).
cd /Users/markrankin/claude/dealer-leads || exit 1
export PORT=5002
exec /Users/markrankin/claude/dealer-leads/.venv/bin/python \
     /Users/markrankin/claude/dealer-leads/serve_prod.py
