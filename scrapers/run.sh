#!/bin/bash
# Run one auction scraper by name:  run.sh <site>   (site = manheim | adesa | copart)
# Invoked by the com.dealerplatform.scraper.<site> LaunchAgents. Runs headful (real
# Chrome, in the logged-in Aqua session) so the saved session resists bot detection;
# add --headless below if you'd rather it not open a window (higher block risk).
set -eu
SITE="${1:?usage: run.sh <site>}"
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/$SITE"
exec "$DIR/.venv/bin/python" scraper.py
