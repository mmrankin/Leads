#!/bin/bash
# Run one auction scraper by name:  run.sh <site>   (site = manheim | adesa | copart)
# Scheduled runs (from the LaunchAgents) honor the per-site enabled flag in
# control.json; an ad-hoc run from the admin sets FORCE=1 to bypass it. A .running
# lockfile in the site dir marks a run in progress (read by the status page).
# Runs headful (real Chrome, in the logged-in Aqua session).
set -u
SITE="${1:?usage: run.sh <site>}"
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"

if [ "${FORCE:-0}" != "1" ]; then
  EN=$("$PY" -c "import json;print(json.load(open('$DIR/control.json')).get('$SITE',{}).get('enabled',True))" 2>/dev/null || echo True)
  if [ "$EN" != "True" ]; then echo "$SITE is disabled — skipping scheduled run."; exit 0; fi
fi

LOCK="$DIR/$SITE/.running"
touch "$LOCK"
trap 'rm -f "$LOCK"' EXIT
cd "$DIR/$SITE"
"$PY" scraper.py
